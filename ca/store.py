"""ca/store.py — SQLite 持久化存储层 (v5.10, turn_stream only)

设计决策: S-001~S-005 (SQLite 存储)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-存储层
  - WAL + threading.local() + busy_timeout 3000ms
  - 写入重试指数退避
  - Schema v5.0 (turn_stream 表, PK=(session_id, turn, seq))

v5.0 清理（2026-07-25）：
  - 删除 turn_cache + turn_plan 表（被 turn_stream 完全取代）
  - 删除 _pack_f32 / _unpack_f32 二进制嵌入序列化
  - 删除 SQLiteStore 全部旧方法（write_turn, read_session, turn_plan 等）
  - 删除 _infer_legacy_state（旧状态推断，不再调用）
  - 删除 checkpoint daemon / session_cache / readonly 分支
  - 保留 SQLiteStore 最小壳（仅 __init__ / _get_conn / conn / close）
  - 保留全部 v5.0 模块级函数
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)


class SQLiteStore:
    """v5.0: turn_stream-only store — no turn_cache, no readonly, no checkpoint daemon."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.session_id: str = self._db_path.stem

    def _get_conn(self) -> sqlite3.Connection:
        """线程本地连接，WAL 模式 + turn_stream schema。"""
        now = time.monotonic()
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            if now - getattr(self._local, 'last_used', now) > Config.DB_IDLE_TIMEOUT_SECONDS:
                try:
                    self._local.conn.close()
                except Exception:
                    pass
                self._local.conn = None

        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=10, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute(f"PRAGMA busy_timeout={Config.DB_BUSY_TIMEOUT_MS}")
            self._local.conn.executescript(_SCHEMA_SQL_V50)

        self._local.last_used = now
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def close(self) -> None:
        if hasattr(self._local, 'conn') and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
def format_previous_summary_for_prompt(l1_text_from_db: str) -> str:
    """将 DB 中历史 Fct 统一转换为新提示词期望的 Markdown 格式。

    - None/空/"无" → "无"
    - JSON (旧 5 类) → 调用 _json_to_v1_markdown() 转换
    - 纯文本 → 原样返回
    """
    if not l1_text_from_db or l1_text_from_db.strip() in ("无", "null"):
        return "无"

    stripped = l1_text_from_db.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            from .post_process import _json_to_v1_markdown as _legacy_json_to_v1_markdown
            result = _legacy_json_to_v1_markdown(data)
            if result:
                return result
            # 转换结果为空，原样返回
            return stripped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 非 JSON 纯文本，原样返回
    return stripped


# ═══════════════════════════════════════════════════════════
# v5.0 — turn_stream 新表 + 模块级函数
# (不绑定 SQLiteStore 类，调用时传 store 实例)
# ═══════════════════════════════════════════════════════════

_SCHEMA_SQL_V50 = """
CREATE TABLE IF NOT EXISTS turn_stream (
    session_id   TEXT    NOT NULL,
    turn         INTEGER NOT NULL,
    seq          INTEGER NOT NULL,

    -- 原始数据核
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL DEFAULT '',

    -- tool 行专用
    tool_name     TEXT,
    tool_call_id  TEXT,
    args_json     TEXT,
    status        TEXT,
    duration_ms   INTEGER,

    -- thought / assistant 行专用
    tool_calls_json TEXT,
    finish_reason  TEXT,
    usage_prompt_tokens     INTEGER,
    usage_completion_tokens INTEGER,

    -- 标记
    biz_category  TEXT,
    written_at    REAL,

    -- 摘要（C-stage 填）
    Fct       TEXT,
    Hdl       TEXT,

    PRIMARY KEY (session_id, turn, seq)
);
"""


def write_turn_v5(store, session_id: str, turn: int, seq: int, *,
                  role: str = 'user', content: str = '',
                  tool_name: Optional[str] = None,
                  tool_call_id: Optional[str] = None,
                  args_json: Optional[str] = None,
                  status: Optional[str] = None,
                  duration_ms: Optional[int] = None,
                  tool_calls_json: Optional[str] = None,
                  finish_reason: Optional[str] = None,
                  usage_prompt_tokens: Optional[int] = None,
                  usage_completion_tokens: Optional[int] = None,
                  biz_category: Optional[str] = None,
                  written_at: Optional[float] = None,
                  fct_text: Optional[str] = None,
                  hdl_text: Optional[str] = None) -> bool:
    """v5.0 INSERT OR REPLACE — 简化参数，无 v4 兼容映射。"""
    if written_at is None:
        written_at = time.time()
    for attempt in range(Config.DB_MAX_RETRY):
        try:
            store.conn.execute(
                """INSERT OR REPLACE INTO turn_stream
                   (session_id, turn, seq, role, content,
                    tool_name, tool_call_id, args_json, status, duration_ms,
                    tool_calls_json, finish_reason,
                    usage_prompt_tokens, usage_completion_tokens,
                    biz_category, written_at,
                    Fct, Hdl)
                   VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?)""",
                (session_id, turn, seq, role, content,
                 tool_name, tool_call_id, args_json, status, duration_ms,
                 tool_calls_json, finish_reason,
                 usage_prompt_tokens, usage_completion_tokens,
                 biz_category, written_at,
                 fct_text, hdl_text),
            )
            store.conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            if attempt < Config.DB_MAX_RETRY - 1:
                time.sleep(0.1 * (2 ** attempt))
            else:
                logger.error("write_turn_v5 failed after %d retries: %s", Config.DB_MAX_RETRY, exc)
                return False
        except sqlite3.Error as exc:
            logger.error("write_turn_v5 error: %s", exc)
            return False
    return False


def read_fct_v5(store, session_id: str, turn: int, seq: int) -> str:
    """点查：返回 (turn, seq) 的 fct_text（Fct），无则空字符串。"""
    try:
        cur = store.conn.execute(
            "SELECT Fct FROM turn_stream WHERE session_id=? AND turn=? AND seq=?",
            (session_id, turn, seq),
        )
        row = cur.fetchone()
        return row[0] or "" if row else ""
    except sqlite3.Error:
        return ""


def get_turn_ca_rows(store, session_id: str, turn: int) -> list:
    """返回该 turn 在 turn_stream 中的所有 (seq, role, finish_reason, tool_calls_json, content, Fct, Hdl)。
    包含原始 content（Elm）用于 ELM 等级回退。按 seq 升序。
    空列表 = 该 turn 无 CA 数据。"""
    try:
        cur = store.conn.execute(
            "SELECT seq, role, finish_reason, tool_calls_json, content, Fct, Hdl FROM turn_stream "
            "WHERE session_id=? AND turn=? ORDER BY seq",
            (session_id, turn),
        )
        return cur.fetchall()
    except sqlite3.Error:
        return []


def read_turn_elm_rows(store, session_id: str, turn: int) -> list:
    """Read all Elm rows for a turn from turn_stream.

    Returns list of (seq, role, content, tool_name, tool_call_id).
    """
    try:
        cur = store.conn.execute(
            "SELECT seq, role, content, tool_name, tool_call_id "
            "FROM turn_stream WHERE session_id=? AND turn=? ORDER BY seq",
            (session_id, turn),
        )
        return cur.fetchall()
    except sqlite3.Error:
        return []


def read_prev_fct(store, session_id: str, turn: int) -> str:
    """Read previous turn's Fct from fin row (role='assistant', finish_reason='stop')."""
    if turn <= 0:
        return ""
    try:
        cur = store.conn.execute(
            "SELECT Fct FROM turn_stream "
            "WHERE session_id=? AND turn=? AND role='assistant' AND finish_reason='stop'",
            (session_id, turn - 1),
        )
        row = cur.fetchone()
        return row[0] or "" if row else ""
    except sqlite3.Error:
        return ""


def update_fin_fct_v5(store, session_id: str, turn: int,
                      fct_text: str, hdl_text: str) -> bool:
    """写 assistant_fin 行的 Fct/Hdl（最末 seq, role='assistant', finish_reason='stop'）。"""
    try:
        store.conn.execute(
            "UPDATE turn_stream SET Fct=?, Hdl=? "
            "WHERE session_id=? AND turn=? AND seq=("
            "  SELECT MAX(seq) FROM turn_stream "
            "  WHERE session_id=? AND turn=? AND role='assistant' AND finish_reason='stop'"
            ")",
            (fct_text, hdl_text, session_id, turn, session_id, turn),
        )
        store.conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("update_fin_fct_v5 failed: %s", exc)
        return False


def read_turn_stream_all(store, session_id: str) -> list:
    """Read all turn_stream rows for a session, ordered by turn, seq.

    Returns list of dicts with keys:
      turn, seq, role, content, tool_name, tool_call_id, args_json, status, duration_ms,
      tool_calls_json, finish_reason, usage_prompt_tokens, usage_completion_tokens,
      biz_category, written_at, Fct, Hdl
    Used by CacheBuilder.build to warm cache from DB.
    """
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, content, tool_name, tool_call_id, args_json, "
            "       status, duration_ms, tool_calls_json, finish_reason, "
            "       usage_prompt_tokens, usage_completion_tokens, "
            "       biz_category, written_at, Fct, Hdl "
            "FROM turn_stream WHERE session_id=? ORDER BY turn, seq",
            (session_id,),
        )
        cols = ["turn", "seq", "role", "content", "tool_name", "tool_call_id", "args_json",
                "status", "duration_ms", "tool_calls_json", "finish_reason",
                "usage_prompt_tokens", "usage_completion_tokens",
                "biz_category", "written_at", "Fct", "Hdl"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def max_turn_v5(store, session_id: str) -> int:
    """Return the highest turn number for session from turn_stream, or 0 if empty."""
    try:
        cur = store.conn.execute(
            "SELECT COALESCE(MAX(turn), 0) FROM turn_stream WHERE session_id=?",
            (session_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0
