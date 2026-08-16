"""ca/store.py — SQLite 持久化存储层 (v5.10, turn_stream only)

设计决策: S-001~S-005 (SQLite 存储)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-存储层
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
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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

        # 迁移兼容层：旧列 content → Elm（v5.x → v5.10+）
        try:
            cur = self._local.conn.execute("PRAGMA table_info(turn_stream)")
            tbl_cols = {row[1] for row in cur.fetchall()}
            if "content" in tbl_cols and "Elm" not in tbl_cols:
                self._local.conn.execute("ALTER TABLE turn_stream RENAME COLUMN content TO Elm")
                self._local.conn.commit()
        except Exception:
            pass

        # 决策 44：v7 细颗粒度列增量迁移（缺列才 ALTER）
        try:
            _migrate_turn_stream_v7(self._local.conn)
        except Exception:
            pass

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
def _format_affairs_for_previous_summary(data: dict) -> str:
    """决策 45：多事务 Fct 的历史摘要直接按事务 hdl + OODA 阶段渲染。

    OODA 阶段项即变更记录；不渲染 stage_tag/【已完成】等状态标签
    （v2 旧记录携带的 changes 在历史摘要中不重复输出，避免双轨）。
    """
    affairs = data.get("affairs")
    if not isinstance(affairs, list) or not affairs:
        return ""
    lines: list[str] = []
    for idx, affair in enumerate(affairs, start=1):
        if not isinstance(affair, dict):
            continue
        ooda = affair.get("ooda") if isinstance(affair.get("ooda"), dict) else {}
        body: list[str] = []
        for label in ("现象与问题", "背景与约束", "决策与方案", "后续行动"):
            items = ooda.get(label)
            if not isinstance(items, list):
                continue
            for item in items:
                item_text = str(item).strip()
                if item_text:
                    body.append(f"- {label}：{item_text}")
        if not body:
            # 空事务不构成历史摘要，避免把「事务1：事务1」空壳喂给 4B
            continue
        hdl = str(affair.get("hdl") or f"事务{idx}").strip()
        lines.append(f"## 事务{idx}：{hdl}")
        lines.extend(body)
    return "\n".join(lines)


def format_previous_summary_for_prompt(l1_text_from_db: str) -> str:
    """将 DB 中历史 Fct 统一转换为新提示词期望的 Markdown 格式。

    - None/空/"无" → "无"
    - 多事务 Fct（含 affairs）→ 事务 hdl + OODA 阶段记录（决策 45）
    - JSON (旧 5 类) → 调用 _json_to_v1_markdown() 转换
    - 纯文本 → 原样返回
    """
    if not l1_text_from_db or l1_text_from_db.strip() in ("无", "null"):
        # 决策 45：不再点名旧 stage_tag/core_change 输出格式——
        # 多事务模式无状态标签，具体输出格式由各自 FCT prompt 约束。
        return "【无历史回顾】——本轮所有内容相对空历史均为首次出现，必须全部提取为新增内容"

    stripped = l1_text_from_db.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            affairs_summary = _format_affairs_for_previous_summary(data)
            if affairs_summary:
                return affairs_summary
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
    Elm       TEXT    NOT NULL DEFAULT '',

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

    -- 摘要（E-stage 填）
    Fct       TEXT,
    Hdl       TEXT,

    -- 决策 44：细颗粒度块模型 + 近源元数据（旧库经 _migrate_turn_stream_v7 补列）
    block_type  TEXT,
    ooda_stage  TEXT,
    request_id  TEXT,
    provider    TEXT,
    model       TEXT,
    reasoning_chars INTEGER,
    text_chars       INTEGER,
    result_chars     INTEGER,
    error_text       TEXT,
    is_fin           INTEGER DEFAULT 0,
    metadata_incomplete INTEGER DEFAULT 0,

    PRIMARY KEY (session_id, turn, seq)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_seq INTEGER,
    turn INTEGER,
    step INTEGER,
    seq INTEGER,
    provider TEXT,
    model TEXT,
    purpose TEXT,
    reasoning_effort TEXT,
    base_url TEXT,
    api_mode TEXT,
    messages_count INTEGER DEFAULT 0,
    input_chars INTEGER DEFAULT 0,
    reasoning_chars INTEGER DEFAULT 0,
    text_chars INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    tool_calls_json TEXT,
    usage_json TEXT,
    finish_kind TEXT,
    duration_ms INTEGER,
    failure_json TEXT,
    status TEXT NOT NULL DEFAULT 'streaming',
    created_at REAL,
    UNIQUE(session_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_request_id ON llm_calls(request_id);

CREATE TABLE IF NOT EXISTS think_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn INTEGER,
    step INTEGER,
    seq INTEGER,
    txn_id INTEGER,
    topic_id INTEGER,
    source_kind TEXT NOT NULL DEFAULT 'cloud_think',
    card_kind TEXT,
    call_id TEXT,
    tool_name TEXT,
    question_text TEXT NOT NULL DEFAULT '',
    l0_abstract TEXT,
    l1_json TEXT,
    entities_json TEXT,
    embedding_json TEXT,
    raw_len INTEGER NOT NULL DEFAULT 0,
    preview TEXT,
    status TEXT NOT NULL DEFAULT 'raw',
    created_at REAL,
    updated_at REAL,
    UNIQUE(session_id, turn, seq)
);
"""

# 决策 44：turn_stream v7 增量列（幂等 ALTER TABLE 迁移）
_TURN_STREAM_V7_COLUMNS = [
    ("block_type", "TEXT"),
    ("ooda_stage", "TEXT"),
    ("request_id", "TEXT"),
    ("provider", "TEXT"),
    ("model", "TEXT"),
    ("reasoning_chars", "INTEGER"),
    ("text_chars", "INTEGER"),
    ("result_chars", "INTEGER"),
    ("error_text", "TEXT"),
    ("is_fin", "INTEGER DEFAULT 0"),
    ("metadata_incomplete", "INTEGER DEFAULT 0"),
]


def _migrate_turn_stream_v7(conn: sqlite3.Connection) -> None:
    """旧库增量补列（缺列才 ALTER；PRAGMA user_version 门控幂等）。"""
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 1:
            return
        cols = {row[1] for row in conn.execute("PRAGMA table_info(turn_stream)")}
        missing = [name for name, _ in _TURN_STREAM_V7_COLUMNS if name not in cols]
        changed = False
        for name, decl in _TURN_STREAM_V7_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE turn_stream ADD COLUMN {name} {decl}")
                changed = True
        conn.execute("PRAGMA user_version = 1")
        if changed:
            conn.commit()
            logger.info("[CA_v7] turn_stream migrated: added columns %s", missing)
    except sqlite3.Error as exc:
        logger.warning("[CA_v7] turn_stream v7 migration failed: %s", exc)


def write_turn_v5(store, session_id: str, turn: int, seq: int, *,
                  role: str = 'user', elm_text: str = '',
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
                  hdl_text: Optional[str] = None,
                  block_type: Optional[str] = None,
                  ooda_stage: Optional[str] = None,
                  request_id: Optional[str] = None,
                  provider: Optional[str] = None,
                  model: Optional[str] = None,
                  reasoning_chars: Optional[int] = None,
                  text_chars: Optional[int] = None,
                  result_chars: Optional[int] = None,
                  error_text: Optional[str] = None,
                  is_fin: Optional[int] = None,
                  metadata_incomplete: Optional[int] = None) -> bool:
    """v5.0 INSERT OR REPLACE + 同内容重放跳过（02-store 行不可变 / BUG-09 泛化）。

    - 同 (session_id, turn, seq) 且「核心列」完全一致 → 跳过，不覆盖已回填的
      Fct/Hdl（重放 post_llm_call 不会把异步 F-stage 已写好的摘要抹掉）。
    - Fct/Hdl 允许被本次参数覆盖（回填路径仍走本函数时生效）。
    - 核心列不同 → 保持既有 INSERT OR REPLACE 覆盖语义（引擎恢复/内容变更）。
    - written_at 不参与比较（时间戳天然不同，否则重放永不命中）。
    - 决策 44：核心列扩展到 v7 新列；旧行（迁移前写入）以 NULL 补齐比较。
    """
    if written_at is None:
        written_at = time.time()
    if is_fin is None:
        is_fin = None
    else:
        is_fin = 1 if is_fin else 0
    core_values = (
        role, elm_text,
        tool_name, tool_call_id, args_json, status, duration_ms,
        tool_calls_json, finish_reason,
        usage_prompt_tokens, usage_completion_tokens,
        biz_category,
        block_type, ooda_stage, request_id, provider, model,
        reasoning_chars, text_chars, result_chars, error_text,
        is_fin, metadata_incomplete,
    )
    _CORE_LEN = len(core_values)
    for attempt in range(Config.DB_MAX_RETRY):
        try:
            conn = store.conn
            try:
                old = conn.execute(
                    """SELECT role, Elm, tool_name, tool_call_id, args_json,
                              status, duration_ms, tool_calls_json, finish_reason,
                              usage_prompt_tokens, usage_completion_tokens,
                              biz_category, block_type, ooda_stage, request_id,
                              provider, model, reasoning_chars, text_chars,
                              result_chars, error_text, is_fin, metadata_incomplete,
                              Fct, Hdl
                       FROM turn_stream
                       WHERE session_id=? AND turn=? AND seq=?""",
                    (session_id, turn, seq),
                ).fetchone()
            except sqlite3.Error:
                old = None
            if old is not None:
                old_core = tuple(old[:_CORE_LEN])
                old_fct, old_hdl = old[_CORE_LEN], old[_CORE_LEN + 1]
                if old_core == core_values and (
                        (fct_text is None or fct_text == old_fct)
                        and (hdl_text is None or hdl_text == old_hdl)):
                    return True
            conn.execute(
                """INSERT OR REPLACE INTO turn_stream
                   (session_id, turn, seq, role, Elm,
                    tool_name, tool_call_id, args_json, status, duration_ms,
                    tool_calls_json, finish_reason,
                    usage_prompt_tokens, usage_completion_tokens,
                    biz_category, written_at,
                    Fct, Hdl,
                    block_type, ooda_stage, request_id, provider, model,
                    reasoning_chars, text_chars, result_chars, error_text,
                    is_fin, metadata_incomplete)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                           ?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, turn, seq, role, elm_text,
                 tool_name, tool_call_id, args_json, status, duration_ms,
                 tool_calls_json, finish_reason,
                 usage_prompt_tokens, usage_completion_tokens,
                 biz_category, written_at,
                 fct_text, hdl_text,
                 block_type, ooda_stage, request_id, provider, model,
                 reasoning_chars, text_chars, result_chars, error_text,
                 is_fin, metadata_incomplete),
            )
            conn.commit()
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
    """返回该 turn 在 turn_stream 中的所有 (seq, role, finish_reason, tool_calls_json, Elm, Fct, Hdl)。
    包含原始 Elm 用于 ELM 等级回退。按 seq 升序。
    空列表 = 该 turn 无 CA 数据。"""
    try:
        cur = store.conn.execute(
            "SELECT seq, role, finish_reason, tool_calls_json, Elm, Fct, Hdl FROM turn_stream "
            "WHERE session_id=? AND turn=? ORDER BY seq",
            (session_id, turn),
        )
        return cur.fetchall()
    except sqlite3.Error:
        return []


def read_turn_elm_rows(store, session_id: str, turn: int) -> list:
    """Read all Elm rows for a turn from turn_stream.

    Returns list of (seq, role, Elm, tool_name, tool_call_id).
    """
    try:
        cur = store.conn.execute(
            "SELECT seq, role, Elm, tool_name, tool_call_id "
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
            "WHERE session_id=? AND turn=? AND role='assistant' AND finish_reason='stop' "
            "ORDER BY seq DESC LIMIT 1",
            (session_id, turn - 1),
        )
        row = cur.fetchone()
        return row[0] or "" if row else ""
    except sqlite3.Error:
        return ""


def update_fin_fct_v5(store, session_id: str, turn: int, seq: int,
                      fct_text: str, hdl_text: str) -> bool:
    """写指定 fin 行 (turn, seq) 的 Fct/Hdl。

    不再使用 MAX(seq) 自动定位——调用方必须传入正确的 fin seq。
    每条 fin 行独立触发 F-stage，独立写入 Fct/Hdl。
    """
    try:
        store.conn.execute(
            "UPDATE turn_stream SET Fct=?, Hdl=? "
            "WHERE session_id=? AND turn=? AND seq=?",
            (fct_text, hdl_text, session_id, turn, seq),
        )
        store.conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("update_fin_fct_v5 failed: %s", exc)
        return False


def read_incremental_elm(store, session_id: str, turn: int, fin_seq: int) -> list:
    """读取从上次 fin 之后到 fin_seq 之间的 Elm 行 + user 行，用于增量 F-stage。

    返回 [(seq, role, Elm, tool_name, tool_call_id), ...]，
    包含 user 行 + 上次 fin_seq+1 到本次 fin_seq 之间的所有行。
    空列表 = 无可用内容。
    """
    try:
        rows = read_turn_elm_rows(store, session_id, turn)
        if not rows:
            return []

        # 一次查出所有 fin 行的 seq（效率优化：避免逐行查 SQL）
        fin_seqs = {
            r[0] for r in store.conn.execute(
                "SELECT seq FROM turn_stream "
                "WHERE session_id=? AND turn=? AND role='assistant' AND finish_reason='stop'",
                (session_id, turn),
            ).fetchall()
        }

        # 找到上次 fin（小于 fin_seq 的最大 fin seq）
        last_fin_seq = -1
        for seq, role, _, _, _ in rows:
            if seq < fin_seq and seq in fin_seqs:
                last_fin_seq = seq

        # 收集 user 行 + 上次 fin_seq+1 到 fin_seq 之间的行
        result: list = []
        for row in rows:
            seq, role = row[0], row[1]
            if role == "user":
                result.append(row)
            elif last_fin_seq < seq <= fin_seq:
                result.append(row)
        return result
    except sqlite3.Error:
        return []


def read_turn_stream_all(store, session_id: str) -> list:
    """Read all turn_stream rows for a session, ordered by turn, seq.

    Returns list of dicts with keys:
      turn, seq, role, Elm, tool_name, tool_call_id, args_json, status, duration_ms,
      tool_calls_json, finish_reason, usage_prompt_tokens, usage_completion_tokens,
      biz_category, written_at, Fct, Hdl,
      block_type, ooda_stage, request_id, provider, model,
      reasoning_chars, text_chars, result_chars, error_text, is_fin,
      metadata_incomplete
    Used by CacheBuilder.build to warm cache from DB.
    """
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, Elm, tool_name, tool_call_id, args_json, "
            "       status, duration_ms, tool_calls_json, finish_reason, "
            "       usage_prompt_tokens, usage_completion_tokens, "
            "       biz_category, written_at, Fct, Hdl, "
            "       block_type, ooda_stage, request_id, provider, model, "
            "       reasoning_chars, text_chars, result_chars, error_text, "
            "       is_fin, metadata_incomplete "
            "FROM turn_stream WHERE session_id=? ORDER BY turn, seq",
            (session_id,),
        )
        cols = ["turn", "seq", "role", "Elm", "tool_name", "tool_call_id", "args_json",
                "status", "duration_ms", "tool_calls_json", "finish_reason",
                "usage_prompt_tokens", "usage_completion_tokens",
                "biz_category", "written_at", "Fct", "Hdl",
                "block_type", "ooda_stage", "request_id", "provider", "model",
                "reasoning_chars", "text_chars", "result_chars", "error_text",
                "is_fin", "metadata_incomplete"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


# ═══════════════════════════════════════════════════════════
# 决策 44 — v7 细颗粒度 reader / llm_calls / think_trace
# ═══════════════════════════════════════════════════════════

def read_incremental_elm_detailed(store, session_id: str, turn: int, fin_seq: int) -> list:
    """F-stage v7 输入：读增量行并附带决策 44 元数据（dict 行）。

    与 read_incremental_elm 相同的窗口语义：
    user 行 + 上次 fin 之后到 fin_seq 之间的所有行。
    返回每行 dict：seq/role/Elm/tool_name/tool_call_id/block_type/ooda_stage/
    request_id/provider/model/finish_reason/tool_calls_json/result_chars/
    error_text/is_fin/Fct/Hdl。
    """
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, Elm, tool_name, tool_call_id, "
            "       block_type, ooda_stage, request_id, provider, model, "
            "       finish_reason, tool_calls_json, result_chars, error_text, "
            "       is_fin, Fct, Hdl "
            "FROM turn_stream WHERE session_id=? AND turn=? ORDER BY seq",
            (session_id, turn),
        )
        cols = ["turn", "seq", "role", "Elm", "tool_name", "tool_call_id",
                "block_type", "ooda_stage", "request_id", "provider", "model",
                "finish_reason", "tool_calls_json", "result_chars", "error_text",
                "is_fin", "Fct", "Hdl"]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        fin_seqs = {
            r["seq"] for r in rows
            if r["role"] == "assistant" and r["finish_reason"] == "stop"
        }
        last_fin_seq = -1
        for r in rows:
            if r["seq"] < fin_seq and r["seq"] in fin_seqs:
                last_fin_seq = r["seq"]
        result = []
        for r in rows:
            if r["role"] == "user":
                result.append(r)
            elif last_fin_seq < r["seq"] <= fin_seq:
                result.append(r)
        return result
    except sqlite3.Error:
        return []


def write_llm_call_v1(store, session_id: str, request_id: str, *,
                      request_seq: Optional[int] = None,
                      turn: Optional[int] = None,
                      step: Optional[int] = None,
                      seq: Optional[int] = None,
                      provider: Optional[str] = None,
                      model: Optional[str] = None,
                      purpose: Optional[str] = None,
                      reasoning_effort: Optional[str] = None,
                      base_url: Optional[str] = None,
                      api_mode: Optional[str] = None,
                      messages_count: Optional[int] = None,
                      input_chars: Optional[int] = None,
                      reasoning_chars: Optional[int] = None,
                      text_chars: Optional[int] = None,
                      chunk_count: Optional[int] = None,
                      tool_calls_json: Optional[str] = None,
                      usage_json: Optional[str] = None,
                      finish_kind: Optional[str] = None,
                      duration_ms: Optional[int] = None,
                      failure_json: Optional[str] = None,
                      status: str = "completed") -> bool:
    """post_api_request 主写 llm_calls（request_id UPSERT，latest-wins）。

    stream 计数由调用方合并 pending 后传入；本函数保留权威列。
    """
    try:
        store.conn.execute(
            """INSERT INTO llm_calls
               (session_id, request_id, request_seq, turn, step, seq,
                provider, model, purpose, reasoning_effort, base_url, api_mode,
                messages_count, input_chars, reasoning_chars, text_chars,
                chunk_count, tool_calls_json, usage_json, finish_kind,
                duration_ms, failure_json, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id, request_id) DO UPDATE SET
                 request_seq=COALESCE(excluded.request_seq, llm_calls.request_seq),
                 turn=COALESCE(excluded.turn, llm_calls.turn),
                 step=COALESCE(excluded.step, llm_calls.step),
                 seq=COALESCE(excluded.seq, llm_calls.seq),
                 provider=COALESCE(excluded.provider, llm_calls.provider),
                 model=COALESCE(excluded.model, llm_calls.model),
                 purpose=COALESCE(excluded.purpose, llm_calls.purpose),
                 reasoning_effort=COALESCE(excluded.reasoning_effort, llm_calls.reasoning_effort),
                 base_url=COALESCE(excluded.base_url, llm_calls.base_url),
                 api_mode=COALESCE(excluded.api_mode, llm_calls.api_mode),
                 messages_count=COALESCE(excluded.messages_count, llm_calls.messages_count),
                 input_chars=COALESCE(excluded.input_chars, llm_calls.input_chars),
                 tool_calls_json=COALESCE(excluded.tool_calls_json, llm_calls.tool_calls_json),
                 usage_json=COALESCE(excluded.usage_json, llm_calls.usage_json),
                 finish_kind=COALESCE(excluded.finish_kind, llm_calls.finish_kind),
                 duration_ms=COALESCE(excluded.duration_ms, llm_calls.duration_ms),
                 failure_json=COALESCE(excluded.failure_json, llm_calls.failure_json),
                 status=excluded.status""",
            (session_id, request_id, request_seq, turn, step, seq,
             provider, model, purpose, reasoning_effort, base_url, api_mode,
             messages_count or 0, input_chars or 0, reasoning_chars or 0,
             text_chars or 0, chunk_count or 0,
             tool_calls_json, usage_json, finish_kind,
             duration_ms, failure_json, status, time.time()),
        )
        store.conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("write_llm_call_v1 failed: %s", exc)
        return False


def patch_llm_call_stream_v1(store, session_id: str, request_id: str, *,
                             turn: Optional[int] = None,
                             provider: Optional[str] = None,
                             model: Optional[str] = None,
                             reasoning_chars: int = 0,
                             text_chars: int = 0,
                             chunk_count: int = 0,
                             finished: Optional[bool] = None,
                             error: Optional[str] = None) -> bool:
    """on_stream_* 补丁：只动流式计数，不覆盖 post_api 权威列（乱序安全）。

    行不存在时插入最小 streaming 行；统计列取 MAX 合并。
    """
    try:
        store.conn.execute(
            """INSERT INTO llm_calls
               (session_id, request_id, turn, provider, model,
                reasoning_chars, text_chars, chunk_count, status, created_at)
               VALUES (?,?,?,?,?, ?,?,?, 'streaming', ?)
               ON CONFLICT(session_id, request_id) DO UPDATE SET
                 turn=COALESCE(excluded.turn, llm_calls.turn),
                 provider=COALESCE(excluded.provider, llm_calls.provider),
                 model=COALESCE(excluded.model, llm_calls.model),
                 reasoning_chars=MAX(COALESCE(llm_calls.reasoning_chars,0), excluded.reasoning_chars),
                 text_chars=MAX(COALESCE(llm_calls.text_chars,0), excluded.text_chars),
                 chunk_count=MAX(COALESCE(llm_calls.chunk_count,0), excluded.chunk_count)""",
            (session_id, request_id, turn, provider, model,
             reasoning_chars, text_chars, chunk_count, time.time()),
        )
        if finished is not None:
            store.conn.execute(
                "UPDATE llm_calls SET status=? WHERE session_id=? AND request_id=?",
                ("completed" if finished else "failed", session_id, request_id))
        if error:
            store.conn.execute(
                "UPDATE llm_calls SET failure_json=?, status='failed' "
                "WHERE session_id=? AND request_id=?",
                (json.dumps({"error": error}, ensure_ascii=False), session_id, request_id))
        store.conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("patch_llm_call_stream_v1 failed: %s", exc)
        return False


def read_llm_call_v1(store, session_id: str, request_id: str) -> Optional[Dict]:
    try:
        cur = store.conn.execute(
            "SELECT * FROM llm_calls WHERE session_id=? AND request_id=?",
            (session_id, request_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))
    except sqlite3.Error:
        return None


def write_think_card_v1(store, card: Dict[str, Any]) -> bool:
    """think_trace UPSERT（UNIQUE(session_id,turn,seq) latest-wins）。"""
    try:
        now = time.time()
        store.conn.execute(
            """INSERT INTO think_trace
               (session_id, turn, step, seq, txn_id, topic_id, source_kind,
                card_kind, call_id, tool_name, question_text, l0_abstract,
                l1_json, entities_json, embedding_json, raw_len, preview,
                status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?)
               ON CONFLICT(session_id, turn, seq) DO UPDATE SET
                 card_kind=excluded.card_kind,
                 call_id=excluded.call_id,
                 tool_name=excluded.tool_name,
                 question_text=excluded.question_text,
                 raw_len=excluded.raw_len,
                 preview=excluded.preview,
                 updated_at=excluded.updated_at""",
            (card.get("session_id"), card.get("turn"), card.get("step"),
             card.get("seq"), card.get("txn_id"), card.get("topic_id"),
             card.get("source_kind", "cloud_think"),
             card.get("card_kind"), card.get("call_id"), card.get("tool_name"),
             card.get("question_text", ""), card.get("l0_abstract"),
             card.get("l1_json"), card.get("entities_json"),
             card.get("embedding_json"), card.get("raw_len", 0),
             card.get("preview"), card.get("status", "raw"), now, now),
        )
        store.conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("write_think_card_v1 failed: %s", exc)
        return False


def read_think_cards_v1(store, session_id: str) -> list:
    try:
        cur = store.conn.execute(
            "SELECT * FROM think_trace WHERE session_id=? ORDER BY turn, seq",
            (session_id,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def read_turn_user_question_v1(store, session_id: str, turn: int) -> str:
    """取该 turn 的 user 行文本（思考卡 question_text）。"""
    try:
        cur = store.conn.execute(
            "SELECT Elm FROM turn_stream "
            "WHERE session_id=? AND turn=? AND role='user' ORDER BY seq LIMIT 1",
            (session_id, turn),
        )
        row = cur.fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def read_turn_thinking_rows_v1(store, session_id: str, turn: int) -> list:
    """该 turn 的 THINKING/thought 行（dict，供 conclusion 卡判定）。"""
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, Elm, block_type, ooda_stage, finish_reason, "
            "       reasoning_chars, request_id FROM turn_stream "
            "WHERE session_id=? AND turn=? AND block_type='thinking' ORDER BY seq",
            (session_id, turn),
        )
        cols = ["turn", "seq", "role", "Elm", "block_type", "ooda_stage",
                "finish_reason", "reasoning_chars", "request_id"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def read_turn_tool_rows_v1(store, session_id: str, turn: int) -> list:
    """该 turn 工具行（dict），供 conclusion 卡工具错误信号判定。"""
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, tool_call_id, tool_name, status, error_text, "
            "       result_chars FROM turn_stream "
            "WHERE session_id=? AND turn=? AND role='tool' ORDER BY seq",
            (session_id, turn),
        )
        cols = ["turn", "seq", "role", "tool_call_id", "tool_name", "status",
                "error_text", "result_chars"]
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


# ═══════════════════════════════════════════════════════════
# v5.10 — topic_summaries 共享 DB（取代 OV Memory Provider）
# ═══════════════════════════════════════════════════════════

_TOPIC_STORE_TLS = threading.local()


def _get_topic_store_path() -> Path:
    """返回共享 ca_topics.db 的路径。

    解析顺序：hermes_constants.get_hermes_home()（Hermes 运行时）→
    HERMES_HOME env（独立脚本/reprocess 环境）→ ~/.hermes fallback。
    """
    try:
        from hermes_constants import get_hermes_home
        hermes_home = Path(get_hermes_home())
    except ImportError:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        hermes_home = Path(env_home) if env_home else Path.home() / ".hermes"
    path = hermes_home / "ca_cache" / "ca_topics.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _get_topic_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """获取共享 topic DB 的连接（线程本地缓存，per-thread per-path）。

    不能用进程级单连接：话题摘要 / reality merge / 精炼轮等多个后台线程
    会并发写 ca_topics.db，共用同一 sqlite3.Connection 会触发
    "cannot start a transaction within a transaction" 并静默丢写。
    """
    if isinstance(db_path, sqlite3.Connection):
        # BUG-11 防御：调用方误传有效连接时直接复用（绕过路径缓存）。
        return db_path
    p = db_path or _get_topic_store_path()
    key = str(p.resolve())
    conns = getattr(_TOPIC_STORE_TLS, "conns", None)
    if conns is None:
        conns = {}
        _TOPIC_STORE_TLS.conns = conns
    conn = conns.get(key)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            pass  # 连接已断开，重建
    conn = sqlite3.connect(str(p), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={Config.DB_BUSY_TIMEOUT_MS}")
    conn.executescript(_SCHEMA_SQL_STRANDS)
    _migrate_wiki_associations_column(conn)
    _migrate_ov_roots_seed(conn)
    conns[key] = conn
    return conn


def _migrate_wiki_associations_column(conn: sqlite3.Connection) -> None:
    """v6.5.4 迁移：wiki_associations.entry_id → theme_id（旧列名兼容）。

    CREATE TABLE IF NOT EXISTS 不会改已有表，旧库（v6.5.3 及以前）仍为 entry_id 列。
    表当前为 0 行（build_wiki_associations 从未成功），RENAME 无数据风险。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_associations)")}
        if "entry_id" in cols and "theme_id" not in cols:
            conn.execute(
                "ALTER TABLE wiki_associations RENAME COLUMN entry_id TO theme_id")
            conn.commit()
    except sqlite3.Error:
        pass  # 表不存在或已迁移，静默跳过


_SCHEMA_SQL_STRANDS = """
-- v6.4 strand 重构（不向前兼容）：topic_summaries/wiki_topic_map 旧表不再创建，
-- 旧数据随 DB 重建丢弃。摘要单元 = strand（事务级），wiki 关联 = strand 粒度。

CREATE TABLE IF NOT EXISTS strand_summaries (
    strand_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    topic_id      INTEGER NOT NULL,      -- 所属话题块（detect 窗口标识）
    profile       TEXT    NOT NULL DEFAULT '',
    hdl           TEXT,                  -- strand 名称（4B 生成，不用 title）
    turns         TEXT    NOT NULL DEFAULT '[]',  -- JSON array [7,8,9]
    ooda_json     TEXT    DEFAULT '{}',  -- {"现象与问题":[...],...}
    changes_json  TEXT    DEFAULT '[]',  -- 扁平聚合（各 ooda 组之和）
    key_facts_json TEXT   DEFAULT '[]',
    centroid_json TEXT,                  -- embed(hdl + ooda 内容) 均值
    status        TEXT    NOT NULL DEFAULT 'pending',  -- completed | skip
    created_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_strand_unmerged
    ON strand_summaries (profile, status, created_at);

CREATE TABLE IF NOT EXISTS session_meta (
    session_id    TEXT    NOT NULL,
    profile       TEXT    NOT NULL DEFAULT '',
    last_turn     INTEGER,
    last_topic_id INTEGER,
    created_at    REAL,
    updated_at    REAL,
    PRIMARY KEY (session_id, profile)
);

-- 话题 wiki（v6.5 theme 重构）：多个相关 strand 归并后的主题条目
-- 一级信息（切换话题时直接注入云端大模型）：
--   title / overview（摘要）/ ooda_json（当前详细状态 OODA 四组）/ key_facts / open_items
-- 二级信息（查 wiki 全文获取）：timeline_json（演变序列，仅 overview 条目）
CREATE TABLE IF NOT EXISTS themes (
    theme_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL DEFAULT '',      -- 一级：锚点（4B 生成，重大转向才更新）
    overview      TEXT    NOT NULL DEFAULT '',      -- 一级：当前摘要（4B，= timeline 末条）
    ooda_json     TEXT    DEFAULT '{}',             -- 一级：当前详细状态（OODA 四组，覆盖式更新）★
    key_facts_json TEXT   DEFAULT '[]',             -- 一级：已确认结论
    open_items_json TEXT  DEFAULT '[]',             -- 一级：待办
    timeline_json TEXT    DEFAULT '[]',             -- 二级：演变时间线（按主题块追加，仅 overview，不设上限）
    changes_json  TEXT    DEFAULT '[]',             -- 二级：全量去重 changes
    centroid_json TEXT,                              -- 召回锚点：embed(overview + ooda + key_facts)
    source_strands TEXT   DEFAULT '{}',             -- {"session_id": [strand_id, ...]}（历史追溯指针）
    profile       TEXT    NOT NULL DEFAULT '',
    -- 精炼轮元数据列（v5.14 兼容，适配 themes）
    health_score        REAL DEFAULT 1.0,
    flagged_for_review  INTEGER DEFAULT 0,
    topic_count         INTEGER DEFAULT 0,
    last_reviewed_turn  INTEGER DEFAULT 0,
    reviewed_at         REAL,
    open_items_resolved INTEGER DEFAULT 0,
    updated_at    REAL,
    created_at    REAL
);

-- v6.5: strand → theme 归并映射（替代 wiki_strand_map；method 一行记录归并方式）
CREATE TABLE IF NOT EXISTS theme_strand_map (
    session_id    TEXT    NOT NULL,
    strand_id     INTEGER NOT NULL,
    theme_id      INTEGER NOT NULL,
    method        TEXT    DEFAULT 'vector',         -- vector | llm | fallback
    PRIMARY KEY (session_id, strand_id)
);

-- v7 (决策 38): reality/theme 共现边（图模型数据源）
-- 块级明细：同 (session_id, topic_id) 块内归并到不同 reality 的 pair 记一次。
-- 聚合键铁律 (2026-08-03 实证): topic_id 是 session 内编号（topic_manager reset 置 1），
-- 跨 session 同名 topic_id 是不同块 → 必须 (session_id, topic_id) 复合键。
-- UNIQUE 复合键保证同块幂等（重复调用不重复计数）。
-- 边权 = COUNT(*)，支持按 created_at 时间衰减（查询时 WHERE created_at >= cutoff）。
CREATE TABLE IF NOT EXISTS cooccurrence_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    topic_id    INTEGER NOT NULL,
    profile     TEXT    NOT NULL DEFAULT '',
    reality_a   INTEGER NOT NULL,        -- 小者（无向边规范化 a < b）
    reality_b   INTEGER NOT NULL,
    created_at  REAL,
    UNIQUE(session_id, topic_id, profile, reality_a, reality_b)
);
CREATE INDEX IF NOT EXISTS idx_cooc_pair ON cooccurrence_events (reality_a, reality_b);
CREATE INDEX IF NOT EXISTS idx_cooc_time ON cooccurrence_events (created_at);
CREATE INDEX IF NOT EXISTS idx_cooc_prof ON cooccurrence_events (profile);

-- v7 (决策 41, 2026-08-07): realities 表（theme 层退役后的现实工作对象）
-- name=固定标识 / hdl=状态锚点(可改) / current_status={"goals":[],"current_state":[],"key_facts":[],"context":[]}
-- timeline=演进序列（seq 递增，含 changes 条目）/ source_strands={"session_id":[strand_id]}
-- query_centroid_json/query_count: 提问云形心（成员 strand 块首提问向量均值，注入拣选用）
CREATE TABLE IF NOT EXISTS realities (
    reality_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    hdl             TEXT,
    current_status  TEXT    DEFAULT '{}',
    timeline        TEXT    DEFAULT '[]',
    source_strands  TEXT    DEFAULT '{}',
    profile         TEXT    NOT NULL DEFAULT '',
    centroid_json   TEXT,
    query_centroid_json TEXT,
    query_count     INTEGER DEFAULT 0,
    health_score    REAL DEFAULT 1.0,
    flagged_for_review INTEGER DEFAULT 0,
    topic_count     INTEGER DEFAULT 0,
    reviewed_at     REAL,
    last_reviewed_turn INTEGER DEFAULT 0,
    created_at      REAL,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_realities_profile ON realities (profile, updated_at);

-- v7 (决策 41): strand → reality 归并映射（替代 theme_strand_map）
CREATE TABLE IF NOT EXISTS strand_to_reality (
    strand_id   INTEGER NOT NULL,
    reality_id  INTEGER NOT NULL,
    PRIMARY KEY (strand_id)
);
CREATE INDEX IF NOT EXISTS idx_s2r_reality ON strand_to_reality (reality_id);

-- wiki_associations：theme 与 graphify 代码节点 + OV 设计文档的关联
-- v6.5.4: 列名 entry_id → theme_id（themes 表语义对齐；旧列名由迁移处理）
CREATE TABLE IF NOT EXISTS wiki_associations (
    theme_id      INTEGER NOT NULL,
    graph_node_id TEXT    NOT NULL,
    node_label    TEXT    NOT NULL DEFAULT '',
    node_type     TEXT    NOT NULL DEFAULT 'code',
    relation      TEXT    NOT NULL DEFAULT 'related',
    strength      REAL    NOT NULL DEFAULT 0.5,
    source_file   TEXT    NOT NULL DEFAULT '',
    ov_uri        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (theme_id, graph_node_id)
);

-- 空闲精炼管线：L4 守护线程每轮精炼的元数据追踪
CREATE TABLE IF NOT EXISTS refinement_meta (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    refined_at        REAL    NOT NULL,
    last_refined_turn INTEGER NOT NULL DEFAULT 0,
    global_turn_max   INTEGER NOT NULL DEFAULT 0,
    tasks_run         TEXT    NOT NULL DEFAULT '[]',
    entries_reviewed  INTEGER DEFAULT 0,
    entries_modified  INTEGER DEFAULT 0,
    entries_split     INTEGER DEFAULT 0,
    fcts_cross_checked  INTEGER DEFAULT 0,
    inconsistencies    INTEGER DEFAULT 0,
    associations_added INTEGER DEFAULT 0,
    graphify_synced   INTEGER DEFAULT 0,
    duration_sec      REAL    DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'completed'
);

-- v7 (决策 44 前置, 2026-08-08): OV 多根持久化配置表
-- build_wiki_subgraph 多根递归拉取 + 精炼轮逐步启用的开关（各 profile 库独立）。
-- filters JSON: {"include": ["首段前缀"], "exclude": ["rel_path 子串"]}，空 {} = 全收录
CREATE TABLE IF NOT EXISTS ov_roots (
    root_uri    TEXT PRIMARY KEY,   -- viking://resources/projects/windows
    filters     TEXT DEFAULT '{}',  -- JSON；空 = 全收录
    enabled     INTEGER DEFAULT 1,  -- 精炼轮逐步启用的开关
    origin      TEXT DEFAULT 'manual', -- manual|refine_probe|seed
    added_at    REAL,
    last_seen   REAL
);
"""


# 种子数据：INSERT OR IGNORE 幂等（迁移时执行一次，重复执行不覆盖用户修改）。
# context-assembler 保持 enabled=1（现状行为不变）；windows 直接启用
# （references_ov 候选目标文档所在根）；irobot enabled=0 由精炼轮探测评估。
_OV_ROOTS_SEED_SQL = """
INSERT OR IGNORE INTO ov_roots (root_uri, filters, enabled, origin, added_at)
VALUES
 ('viking://resources/projects/context-assembler', '{}', 1, 'seed',
  CAST(strftime('%s','now') AS REAL)),
 ('viking://resources/projects/windows',
  '{"exclude":["/code/"]}', 1, 'seed',
  CAST(strftime('%s','now') AS REAL)),
 ('viking://resources/projects/irobot', '{}', 0, 'seed',
  CAST(strftime('%s','now') AS REAL));
"""


# user_version 位语义: 0=未初始化, 1=已初始化（高位留空，供后续迁移使用）
_OV_ROOTS_UV_INITIALIZED = 1


def _migrate_ov_roots_seed(conn: sqlite3.Connection) -> None:
    """ov_roots 种子迁移（user_version 门控，决策 44 续 R7）。

    仅「未初始化」（PRAGMA user_version=0）执行：
      - 表为空（全新库）→ INSERT 种子 3 行 + 置位 user_version=1（同一 executescript）
      - 表非空（存量库）→ 仅置位 user_version=1，不播种不改写
    user_version>=1 → 直接返回（remove 后不再以 enabled=1 复活）。
    置位/执行失败 → warning（不静默）。
    """
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        uv = int(row[0]) if row else 0
        if uv >= _OV_ROOTS_UV_INITIALIZED:
            return
        count = conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0]
        if count == 0:
            # 全新库：播种 + 置位（同一 executescript，尽力原子）
            sql = _OV_ROOTS_SEED_SQL + f"\nPRAGMA user_version = {_OV_ROOTS_UV_INITIALIZED};"
        else:
            # 存量库（user_version=0 且表有行）→ 仅置位不播种
            sql = f"PRAGMA user_version = {_OV_ROOTS_UV_INITIALIZED};"
        conn.executescript(sql)
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("[CA_STORE] ov_roots 种子迁移失败: %s", exc)

def set_ov_root_filters(root_uri: str, filters: dict) -> bool:
    """手动设置根 filters（决策 44 续 R5；LLM 不写 filters）。

    校验: filters 为 dict 且 include/exclude 均 list[str]（可空）；
    非法 → False 拒绝（不写）。root_uri 不在表 → False + warning。
    合法 → UPDATE filters=json.dumps(filters)，commit，True。
    """
    if not isinstance(filters, dict):
        logger.warning("[CA_STORE] set_ov_root_filters: filters 非 dict，拒绝")
        return False
    for key in ("include", "exclude"):
        v = filters.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            logger.warning(
                "[CA_STORE] set_ov_root_filters: %s 必须为 list[str]，拒绝",
                key)
            return False
    conn = _get_topic_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM ov_roots WHERE root_uri=?", (root_uri,)
        ).fetchone()
        if row is None:
            logger.warning(
                "[CA_STORE] set_ov_root_filters: root_uri 不在 ov_roots 表: %s",
                root_uri)
            return False
        conn.execute(
            "UPDATE ov_roots SET filters=? WHERE root_uri=?",
            (json.dumps(filters, ensure_ascii=False), root_uri))
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA_STORE] set_ov_root_filters: 写入失败: %s", exc)
        return False


def list_ov_roots() -> list[dict]:
    """全表行 → [{root_uri, filters(dict), enabled, origin, added_at, last_seen}]。

    filters JSON 容错（非法 JSON → {}，不崩、不丢根）。
    """
    conn = _get_topic_conn()
    try:
        rows = conn.execute(
            "SELECT root_uri, filters, enabled, origin, added_at, last_seen "
            "FROM ov_roots ORDER BY root_uri"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("[CA_STORE] list_ov_roots: 读取失败: %s", exc)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            flt = json.loads(r[1] or "{}")
        except (json.JSONDecodeError, TypeError):
            flt = {}
        if not isinstance(flt, dict):
            flt = {}
        out.append({
            "root_uri": r[0],
            "filters": flt,
            "enabled": r[2],
            "origin": r[3],
            "added_at": r[4],
            "last_seen": r[5],
        })
    return out


# ═══════════════════════════════════════════════════════════
# v6.4 — strand 读写 API（strand_summaries / wiki_strand_map）
# ═══════════════════════════════════════════════════════════


def write_strand_summary(
    session_id: str, topic_id: int, profile: str,
    hdl: str = "", turns: Optional[list] = None,
    ooda_json: str = "{}", changes_json: str = "[]",
    key_facts_json: str = "[]", status: str = "pending",
    db_path: Optional[Path] = None,
) -> Optional[int]:
    """写入一条 strand 摘要，返回 strand_id（失败 None）。"""
    conn = _get_topic_conn(db_path)
    now = time.time()
    turns_json = json.dumps(turns or [], ensure_ascii=False)
    try:
        cur = conn.execute(
            """INSERT INTO strand_summaries
               (session_id, topic_id, profile, hdl, turns, ooda_json,
                changes_json, key_facts_json, status, created_at)
               VALUES (?,?,?,?,?,?, ?,?,?,?)""",
            (session_id, topic_id, profile, hdl, turns_json, ooda_json,
             changes_json, key_facts_json, status, now),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as exc:
        logger.warning("[CA] write_strand_summary failed: %s", exc)
        return None


def update_strand_centroid(
    strand_id: int,
    centroid_json: str,
    db_path: Optional[Path] = None,
) -> bool:
    """写入 strand centroid（embed(hdl+ooda) 均值）。"""
    conn = _get_topic_conn(db_path)
    try:
        conn.execute(
            "UPDATE strand_summaries SET centroid_json=? WHERE strand_id=?",
            (centroid_json, strand_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] update_strand_centroid failed: %s", exc)
        return False


def query_strands_by_session(
    session_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """按 session 查询全部 strand（含 JSON 字段解析）。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT strand_id, session_id, topic_id, profile, hdl, turns, "
            "       ooda_json, changes_json, key_facts_json, centroid_json, "
            "       status, created_at "
            "FROM strand_summaries WHERE session_id=? ORDER BY strand_id",
            (session_id,),
        )
        cols = [
            "strand_id", "session_id", "topic_id", "profile", "hdl", "turns",
            "ooda_json", "changes_json", "key_facts_json", "centroid_json",
            "status", "created_at",
        ]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["turns"] = json.loads(d["turns"]) if d["turns"] else []
            rows.append(d)
        return rows
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        logger.warning("[CA] query_strands_by_session failed: %s", exc)
        return []


def create_theme(
    profile: str,
    title: str,
    overview: str,
    ooda: Optional[dict],
    key_facts: Optional[list],
    open_items: Optional[list],
    timeline_entry: Optional[dict],
    source_strand: Optional[dict],
    centroid_json: Optional[str],
    changes: Optional[list] = None,
    db_path: Optional[Path] = None,
) -> Optional[int]:
    """创建 theme（v6.5）：首条归并的初始状态。

    timeline_entry: {seq, topic_id, turns, session_id, overview} 首条
    source_strand: {session_id, strand_id} 首个来源
    changes: 初始全量去重 changes（create 时由 strand changes 初始化，merge 再追加）
    """
    conn = _get_topic_conn(db_path)
    now = time.time()
    try:
        timeline = []
        if timeline_entry:
            entry = dict(timeline_entry)
            entry.setdefault("seq", 1)   # create 首条 seq=1（与 update_theme 追加衔接）
            timeline.append(entry)
        cur = conn.execute(
            """INSERT INTO themes
               (title, overview, ooda_json, key_facts_json, open_items_json,
                timeline_json, changes_json, centroid_json, source_strands,
                profile, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, overview,
                json.dumps(ooda or {}, ensure_ascii=False),
                json.dumps(key_facts or [], ensure_ascii=False),
                json.dumps(open_items or [], ensure_ascii=False),
                json.dumps(timeline, ensure_ascii=False),
                json.dumps(changes or [], ensure_ascii=False),
                centroid_json or "null",
                json.dumps(
                    {source_strand["session_id"]: [source_strand["strand_id"]]}
                    if source_strand else {}, ensure_ascii=False),
                profile, now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as exc:
        logger.warning("[CA] create_theme failed: %s", exc)
        return None


def load_all_themes(
    db_path: Optional[Path] = None,
) -> list[dict]:
    """返回所有 theme（含 ooda/timeline/source_strands 解析后的 dict）。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT theme_id, title, overview, ooda_json, key_facts_json, "
            "       open_items_json, timeline_json, changes_json, "
            "       centroid_json, source_strands, profile "
            "FROM themes ORDER BY theme_id",
        )
        cols = [
            "theme_id", "title", "overview", "ooda", "key_facts",
            "open_items", "timeline", "changes", "centroid",
            "source_strands", "profile",
        ]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["ooda"] = _safe_load_list(d.get("ooda")) if isinstance(d.get("ooda"), str) else (d.get("ooda") or {})
            if not isinstance(d["ooda"], dict):
                d["ooda"] = {}
            for key in ("key_facts", "open_items", "timeline", "changes"):
                if isinstance(d.get(key), str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = []
            if isinstance(d.get("source_strands"), str):
                try:
                    d["source_strands"] = json.loads(d["source_strands"])
                except (json.JSONDecodeError, TypeError):
                    d["source_strands"] = {}
            if isinstance(d.get("centroid"), str):
                try:
                    d["centroid"] = json.loads(d["centroid"])
                except (json.JSONDecodeError, TypeError):
                    d["centroid"] = None
            out.append(d)
        return out
    except sqlite3.Error as exc:
        logger.warning("[CA] load_all_themes failed: %s", exc)
        return []


def update_theme(
    theme_id: int,
    title: Optional[str] = None,
    overview: Optional[str] = None,
    ooda: Optional[dict] = None,
    key_facts: Optional[list] = None,
    open_items: Optional[list] = None,
    timeline_entry: Optional[dict] = None,
    changes: Optional[list] = None,
    centroid_json: Optional[str] = None,
    source_strand: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """更新 theme（v6.5）：timeline 追加 + 字段覆盖（None 不变）。

    timeline_entry: {seq?, topic_id, turns, session_id, overview}；
       seq 缺省自动 = 现有最大 seq + 1。overview 字段 = timeline 末条语义。
    source_strand: {session_id, strand_id} — 合并进 source_strands（历史追溯指针）。
    """
    conn = _get_topic_conn(db_path)
    try:
        row = conn.execute(
            "SELECT timeline_json, changes_json, source_strands "
            "FROM themes WHERE theme_id=?",
            (theme_id,),
        ).fetchone()
        if not row:
            logger.warning("[CA] update_theme: theme %d not found", theme_id)
            return False

        old_timeline = json.loads(row[0]) if row[0] else []
        old_changes = json.loads(row[1]) if row[1] else []
        old_source = {}
        if row[2]:
            try:
                old_source = json.loads(row[2])
            except (json.JSONDecodeError, TypeError):
                old_source = {}

        new_timeline = list(old_timeline)
        if timeline_entry:
            entry = dict(timeline_entry)
            if "seq" not in entry:
                entry["seq"] = max([e.get("seq", 0) for e in new_timeline] or [0]) + 1
            new_timeline.append(entry)

        new_changes = list(old_changes)
        if changes:
            for c in changes:
                if isinstance(c, str) and c not in new_changes:
                    new_changes.append(c)

        new_source = dict(old_source)
        if source_strand:
            sid = source_strand.get("session_id")
            s_id = source_strand.get("strand_id")
            if sid:
                if sid not in new_source:
                    new_source[sid] = []
                if s_id not in new_source[sid]:
                    new_source[sid].append(s_id)

        updates: dict = {}
        if title is not None:
            updates["title"] = title
        if overview is not None:
            updates["overview"] = overview
        if ooda is not None:
            updates["ooda_json"] = json.dumps(ooda, ensure_ascii=False)
        if key_facts is not None:
            updates["key_facts_json"] = json.dumps(key_facts, ensure_ascii=False)
        if open_items is not None:
            updates["open_items_json"] = json.dumps(open_items, ensure_ascii=False)
        if centroid_json is not None:
            updates["centroid_json"] = centroid_json
        updates["timeline_json"] = json.dumps(new_timeline, ensure_ascii=False)
        updates["changes_json"] = json.dumps(new_changes, ensure_ascii=False)
        updates["source_strands"] = json.dumps(new_source, ensure_ascii=False)
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE themes SET {sets} WHERE theme_id=?", (*updates.values(), theme_id))
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] update_theme failed: %s", exc)
        return False


def insert_theme_strand_map(
    session_id: str,
    strand_id: int,
    theme_id: int,
    method: str = "vector",
    db_path: Optional[Path] = None,
) -> bool:
    """写入 strand → theme 归并映射（v6.5，替代 wiki_strand_map）。"""
    conn = _get_topic_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO theme_strand_map "
            "(session_id, strand_id, theme_id, method) VALUES (?,?,?,?)",
            (session_id, strand_id, theme_id, method),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] insert_theme_strand_map failed: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════
# v7 reality 层（决策 41：生产 reality 化，2026-08-07）
# ══════════════════════════════════════════════════════════════════════════

def create_reality(
    profile: str,
    name: str,
    hdl: str,
    current_status: Optional[dict],
    timeline_entry: Optional[dict],
    source_strand: Optional[dict],
    centroid_json: Optional[str],
    changes: Optional[list] = None,
    db_path: Optional[Path] = None,
) -> Optional[int]:
    """创建 reality（决策 41）：首条归并的初始状态。

    timeline_entry: {seq, topic_id, turns, session_id, overview} 首条
    source_strand: {session_id, strand_id} 首个来源（并入 source_strands）
    changes: 初始全量去重 changes（写 timeline 首条之外的演进摘要）
    """
    conn = _get_topic_conn(db_path)
    now = time.time()
    try:
        timeline = []
        if timeline_entry:
            entry = dict(timeline_entry)
            entry.setdefault("seq", 1)
            timeline.append(entry)
        if changes:
            timeline.append({"seq": 2, "changes": list(changes),
                             "session_id": (source_strand or {}).get("session_id", "")})
        cur = conn.execute(
            """INSERT INTO realities
               (name, hdl, current_status, timeline, source_strands,
                profile, centroid_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                name, hdl,
                json.dumps(current_status or {}, ensure_ascii=False),
                json.dumps(timeline, ensure_ascii=False),
                json.dumps(
                    {source_strand["session_id"]: [source_strand["strand_id"]]}
                    if source_strand else {}, ensure_ascii=False),
                profile,
                centroid_json or "null",
                now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as exc:
        logger.warning("[CA] create_reality failed: %s", exc)
        return None


def load_all_realities(
    db_path: Optional[Path] = None,
) -> list[dict]:
    """返回所有 reality（含 current_status/timeline/source_strands/centroid 解析）。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT reality_id, name, hdl, current_status, timeline, "
            "       source_strands, profile, centroid_json, query_centroid_json, "
            "       query_count, health_score, flagged_for_review, topic_count "
            "FROM realities ORDER BY reality_id",
        )
        cols = [
            "reality_id", "name", "hdl", "current_status", "timeline",
            "source_strands", "profile", "centroid", "query_centroid",
            "query_count", "health_score", "flagged_for_review", "topic_count",
        ]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for key in ("current_status", "timeline", "source_strands"):
                if isinstance(d.get(key), str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = {} if key == "source_strands" else ([] if key == "timeline" else {})
            for key in ("centroid", "query_centroid"):
                if isinstance(d.get(key), str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = None
            out.append(d)
        return out
    except sqlite3.Error as exc:
        logger.warning("[CA] load_all_realities failed: %s", exc)
        return []


def update_reality(
    reality_id: int,
    name: Optional[str] = None,
    hdl: Optional[str] = None,
    current_status: Optional[dict] = None,
    timeline_entry: Optional[dict] = None,
    changes: Optional[list] = None,
    centroid_json: Optional[str] = None,
    source_strand: Optional[Any] = None,
    query_centroid_json: Optional[str] = None,
    query_count: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """更新 reality（决策 41）：timeline 追加 + 字段覆盖（None 不变）。

    timeline_entry: {seq?, topic_id, turns, session_id, overview}；
       seq 缺省 = 现有最大 seq + 1。
    source_strand: {session_id, strand_id} 或 list[同结构] —
      全部合并进 source_strands（s2r↔source_strands 一致，决策 41 审计门）。
    query_centroid_json/query_count: 提问云形心增量维护（调用方算好新值）。
    """
    conn = _get_topic_conn(db_path)
    source_entries = (source_strand if isinstance(source_strand, list)
                      else ([source_strand] if source_strand else []))
    first_source = source_entries[0] if source_entries else None
    try:
        row = conn.execute(
            "SELECT timeline, source_strands, query_centroid_json, query_count "
            "FROM realities WHERE reality_id=?",
            (reality_id,),
        ).fetchone()
        if not row:
            logger.warning("[CA] update_reality: reality %d not found", reality_id)
            return False

        new_timeline = json.loads(row[0]) if row[0] else []
        # R-4（决策 42）存量兼容：str 条目（旧 hdl）在 seq 计算时跳过（dict 才可取 seq）
        if timeline_entry:
            entry = dict(timeline_entry)
            entry["ts"] = entry.get("ts") or time.time()  # R-4：结构化时间戳
            if "seq" not in entry:
                entry["seq"] = max([e.get("seq", 0) for e in new_timeline
                                    if isinstance(e, dict)] or [0]) + 1
            new_timeline.append(entry)
        if changes:
            new_timeline.append({"seq": max([e.get("seq", 0) for e in new_timeline
                                             if isinstance(e, dict)] or [0]) + 1,
                                 "changes": list(changes),
                                 "session_id": (first_source or {}).get("session_id", "")})

        new_source = {}
        if row[1]:
            try:
                new_source = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                new_source = {}
        for src in source_entries:
            if not isinstance(src, dict):
                continue
            sid = src.get("session_id")
            s_id = src.get("strand_id")
            if sid and s_id is not None:
                if sid not in new_source:
                    new_source[sid] = []
                if s_id not in new_source[sid]:
                    new_source[sid].append(s_id)

        updates: dict = {}
        if name is not None:
            updates["name"] = name
        if hdl is not None:
            updates["hdl"] = hdl
        if current_status is not None:
            updates["current_status"] = json.dumps(current_status, ensure_ascii=False)
        if centroid_json is not None:
            updates["centroid_json"] = centroid_json
        if query_centroid_json is not None:
            updates["query_centroid_json"] = query_centroid_json
        if query_count is not None:
            updates["query_count"] = query_count
        updates["timeline"] = json.dumps(new_timeline, ensure_ascii=False)
        updates["source_strands"] = json.dumps(new_source, ensure_ascii=False)
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE realities SET {sets} WHERE reality_id=?",
                     (*updates.values(), reality_id))
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] update_reality failed: %s", exc)
        return False


def insert_reality_strand_map(
    strand_id: int,
    reality_id: int,
    method: str = "llm",
    db_path: Optional[Path] = None,
) -> bool:
    """写入 strand → reality 归并映射（决策 41，strand_to_reality 表）。"""
    conn = _get_topic_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO strand_to_reality "
            "(strand_id, reality_id) VALUES (?,?)",
            (strand_id, reality_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] insert_reality_strand_map failed: %s", exc)
        return False


def query_realities_by_semantics(
    q_emb: list[float],
    profile: str,
    limit: int = 3,
    exclude_session_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """语义检索 reality（决策 41，对标 query_themes_by_semantics）：余弦排序取 top N。

    返回 reality dict：reality_id/name/hdl/current_status/timeline（供注入）。
    跨会话、非当前 session 的 reality 才有意义 → 排除当前 session（source_strands 含该 session）。
    无匹配 → 空列表。
    """
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT reality_id, name, hdl, current_status, timeline, "
            "       centroid_json, source_strands "
            "FROM realities "
            "WHERE centroid_json IS NOT NULL AND centroid_json != 'null' "
            "  AND profile = ? "
            "ORDER BY updated_at DESC",
            (profile,),
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        logger.warning("[CA] query_realities_by_semantics failed: %s", exc)
        return []

    if not rows:
        return []

    if exclude_session_id:
        filtered = []
        for row in rows:
            ss_raw = row[6]
            hit = False
            if ss_raw:
                try:
                    ss = json.loads(ss_raw)
                    if isinstance(ss, dict) and exclude_session_id in ss:
                        hit = True
                except (json.JSONDecodeError, TypeError):
                    pass
            if not hit:
                filtered.append(row)
        rows = filtered
        if not rows:
            return []

    scored: list[tuple[float, dict]] = []
    for row in rows:
        centroid_raw = row[5]
        if not centroid_raw or centroid_raw == "null":
            continue
        try:
            centroid = json.loads(centroid_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        c = _cosine_similarity_inline(q_emb, centroid)
        if not c:
            continue
        try:
            cs = json.loads(row[3]) if row[3] else {}
        except (json.JSONDecodeError, TypeError):
            cs = {}
        try:
            tl = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            tl = []
        scored.append((c, {
            "reality_id": row[0],
            "name": row[1] or "",
            "hdl": row[2] or "",
            "current_status": cs,
            "timeline": tl,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


def find_unmerged_strands(
    profile: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """扫描已 completed 但未归入 theme 的 strand。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT s.strand_id, s.session_id, s.topic_id, s.hdl, s.turns, "
            "       s.ooda_json, s.changes_json, s.key_facts_json, "
            "       s.centroid_json "
            "FROM strand_summaries s "
            "LEFT JOIN theme_strand_map m "
            "  ON m.session_id=s.session_id AND m.strand_id=s.strand_id "
            "WHERE s.profile=? AND s.status='completed' "
            "  AND m.session_id IS NULL "
            "ORDER BY s.created_at ASC",
            (profile,),
        )
        cols = [
            "strand_id", "session_id", "topic_id", "hdl", "turns",
            "ooda_json", "changes_json", "key_facts_json", "centroid_json",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("[CA] find_unmerged_strands failed: %s", exc)
        return []


def _safe_load_list(val):
    try:
        return json.loads(val) if val else []
    except (json.JSONDecodeError, TypeError):
        return []


def upsert_session_meta(
    session_id: str, profile: str,
    last_turn: int, last_topic_id: int,
    db_path: Optional[Path] = None,
) -> bool:
    """记录会话元数据（每次 topic_switch 后调用）。"""
    conn = _get_topic_conn(db_path)
    now = time.time()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO session_meta
               (session_id, profile, last_turn, last_topic_id, created_at, updated_at)
               VALUES (?,?,?,?,
                       COALESCE((SELECT created_at FROM session_meta
                                 WHERE session_id=? AND profile=?), ?),
                       ?)""",
            (session_id, profile, last_turn, last_topic_id,
             session_id, profile, now, now),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] upsert_session_meta failed: %s", exc)
        return False


def get_last_session_meta(
    profile: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """查询最近活跃的会话元数据（turn 1 扫描补缺用）。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT session_id, profile, last_turn, last_topic_id, created_at, updated_at "
            "FROM session_meta WHERE profile=? ORDER BY updated_at DESC LIMIT 1",
            (profile,),
        )
        row = cur.fetchone()
        if row:
            return {
                "session_id": row[0],
                "profile": row[1],
                "last_turn": row[2],
                "last_topic_id": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
        return None
    except sqlite3.Error:
        return None


def get_topic_strand_status(
    session_id: str, topic_id: int,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """v6.4: 查特定话题块下的 strand 聚合状态（backfill 判断用）。

    - 任一 strand completed → 'completed'
    - 只有 skip → 'skip'
    - 无 strand → None
    """
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT status FROM strand_summaries "
            "WHERE session_id=? AND topic_id=?",
            (session_id, topic_id),
        )
        statuses = [r[0] for r in cur.fetchall()]
        if "completed" in statuses:
            return "completed"
        if statuses:
            return "skip"
        return None
    except sqlite3.Error:
        return None


def _entry_from_affairs(turn: int, block_hdl: str, raw_affairs: list) -> dict[str, Any]:
    """决策 45 单一数据源：从 Fct affairs 现场派生旧消费者视图。

    Fct JSON 不再携带 legacy 扁平字段；collect_turn_fcts 在这里把
    affairs[].ooda 四段映射回 entry 的 changes/tags/ooda_tags/todos/
    consensus/key_facts_supp/new_materials 视图（v2 旧记录携带的
    per-affair changes 保留其 stage_tag/user_request，只读兼容）。
    """
    OODA_KEYS = ("现象与问题", "背景与约束", "决策与方案", "后续行动")
    STAGE_TO_LEGACY = {
        "现象与问题": "new_materials",
        "背景与约束": "key_facts_supp",
        "决策与方案": "consensus",
    }

    affairs: list[dict] = []
    clean_changes: list[str] = []
    seen_cores: set[str] = set()
    tags: dict[str, str] = {}
    ooda_tags: dict[str, str] = {}
    todos: list[str] = []
    user_requests: list[str] = []
    consensus: list[str] = []
    key_facts_supp: list[str] = []
    new_materials: list[str] = []

    for a in raw_affairs:
        if not isinstance(a, dict):
            continue
        raw_ooda = a.get("ooda") if isinstance(a.get("ooda"), dict) else {}
        ooda: dict[str, list[str]] = {
            k: [str(x).strip() for x in (raw_ooda.get(k) or []) if str(x).strip()]
            for k in OODA_KEYS
        }
        a_changes = a.get("changes")
        if not isinstance(a_changes, list):
            a_changes = []
        v2_changes: list[dict] = []
        for c in a_changes:
            if not isinstance(c, dict):
                continue
            core = str(c.get("core_change") or "").strip()
            if not core:
                continue
            stag = str(c.get("stage_tag") or "").strip()
            item: dict[str, str] = {"core_change": core}
            if stag:
                item["stage_tag"] = stag
            if c.get("user_request"):
                item["user_request"] = str(c["user_request"]).strip()
            v2_changes.append(item)

        affair: dict[str, Any] = {
            "hdl": str(a.get("hdl") or "").strip(),
            "turns": a.get("turns") if isinstance(a.get("turns"), list) else [turn],
            "ooda": ooda,
        }
        if v2_changes:
            affair["changes"] = v2_changes
        affairs.append(affair)

        # v3：OODA 阶段项即变更；同时派生旧消费者的扁平视图
        for stage in OODA_KEYS:
            for item_text in ooda[stage]:
                if item_text not in seen_cores:
                    seen_cores.add(item_text)
                    clean_changes.append(item_text)
                ooda_tags[item_text] = stage
                legacy_key = STAGE_TO_LEGACY.get(stage)
                if legacy_key:
                    target = {"new_materials": new_materials,
                              "key_facts_supp": key_facts_supp,
                              "consensus": consensus}[legacy_key]
                    if item_text not in target:
                        target.append(item_text)
                if stage == "后续行动" and item_text not in todos:
                    todos.append(item_text)

        # v2 旧库：per-affair changes 补差集，并保留状态标签语义
        for c in v2_changes:
            core = c["core_change"]
            if core not in seen_cores:
                seen_cores.add(core)
                clean_changes.append(core)
            if c.get("stage_tag"):
                tags[core] = c["stage_tag"]
                if c["stage_tag"] == "评估中" and core not in todos:
                    todos.append(core)
            ur = c.get("user_request", "").strip()
            if ur and ur not in user_requests:
                user_requests.append(ur)

    first_affair_hdl = next((a["hdl"] for a in affairs if a["hdl"]), "")
    entry: dict[str, Any] = {
        "turn": turn,
        "hdl": block_hdl or first_affair_hdl,
        "changes": clean_changes,
        "tags": tags,
        "ooda_tags": ooda_tags,
        "todos": todos,
        "user_requests": user_requests,
        "consensus": consensus,
        "key_facts_supp": key_facts_supp,
        "new_materials": new_materials,
        "affairs": affairs,
    }
    return entry


def _entry_from_legacy_fct(turn: int, block_hdl: str, fct_data: dict) -> dict[str, Any]:
    """旧单事务 Fct JSON → collect_turn_fcts 的 entry 视图（行为不变）。"""
    raw_changes = fct_data.get("changes") or fct_data.get("changes_before", [])
    clean_changes: list[str] = []
    tags: dict[str, str] = {}
    ooda_tags: dict[str, str] = {}
    todos: list[str] = []
    user_requests: list[str] = []
    for c in raw_changes:
        if not isinstance(c, dict):
            continue
        core = (c.get("core_change") or c.get("change", "")).strip()
        if not core:
            continue
        clean_changes.append(core)
        stag = c.get("stage_tag", "").strip()
        if stag:
            tags[core] = stag
            if stag == "评估中":
                todos.append(core)
        ooda_val = c.get("ooda", "").strip()
        if ooda_val:
            ooda_tags[core] = ooda_val
        ur = c.get("user_request", "").strip()
        if ur:
            user_requests.append(ur)
    entry: dict[str, Any] = {
        "turn": turn,
        "hdl": block_hdl,
        "changes": clean_changes,
        "tags": tags,
        "ooda_tags": ooda_tags,
        "todos": todos,
        "user_requests": user_requests,
    }
    consensus = fct_data.get("consensus")
    if isinstance(consensus, list):
        entry["consensus"] = [str(x).strip() for x in consensus if x]
    obj_facts = fct_data.get("objective_facts")
    if isinstance(obj_facts, list):
        entry["key_facts_supp"] = [str(x).strip() for x in obj_facts if x]
    new_mat = fct_data.get("new_materials")
    if isinstance(new_mat, list):
        entry["new_materials"] = [str(x).strip() for x in new_mat if x]
    return entry


def collect_turn_fcts(
    store, session_id: str, turns: list[int],
) -> list[dict]:
    """从 turn_stream 收集话题块各轮的 Fct 数据。

    - 多事务 Fct（v3，决策 45）：JSON 只有 `affairs[]`，OODA 阶段项即变更；
      此处现场派生旧消费者视图（changes/tags/ooda_tags/todos/补充四段）。
    - 旧单事务 Fct：维持原有 `changes/core_change/四段字段` 解析行为。

    返回 [{"turn": n, "hdl": "...", "changes": [str, ...],
            "tags": {str: str}, "ooda_tags": {str: str},
            "todos": [str, ...], "user_requests": [str, ...],
            "consensus": [str, ...], "key_facts_supp": [str, ...],
            "new_materials": [str, ...], "affairs": [...]}, ...]
    """
    result = []
    for t in sorted(turns):
        rows = get_turn_ca_rows(store, session_id, t)
        fct = None
        hdl = ""
        for seq, role, fin, _, _, Fct, Hdl in rows:
            if role == "assistant" and fin == "stop" and Fct:
                fct = Fct
                hdl = Hdl or ""
        if not fct:
            continue
        try:
            fct_data = json.loads(fct)
            raw_affairs = fct_data.get("affairs")
            if isinstance(raw_affairs, list) and raw_affairs:
                entry = _entry_from_affairs(t, hdl, raw_affairs)
            else:
                entry = _entry_from_legacy_fct(t, hdl, fct_data)
            result.append(entry)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def find_sessions_by_mtime(ca_cache_dir: Optional[Path] = None, max_results: int = 3) -> list[str]:
    """按 mtime 降序列出 ca_cache 中最近的 turn_stream DB 文件（排除 ca_topics.db）。

    turn 1 扫描的 fallback：ca_topics.db 缺失时用目录 scan 发现上次会话。
    """
    cache_dir = ca_cache_dir or _get_topic_store_path().parent
    if not cache_dir.exists():
        return []
    db_files = sorted(
        [f for f in cache_dir.iterdir()
         if f.suffix == ".db" and f.stem != "ca_topics"],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return [f.stem for f in db_files[:max_results]]
# ═══════════════════════════════════════════════════════════
# v5.11 — wiki 归并 + 语义召回  API
# ═══════════════════════════════════════════════════════════


def upsert_wiki_entry(
    title: str,
    overview: str,
    centroid_json: str,
    changes_json: str,
    key_facts_json: str,
    open_items_json: str,
    source_ids: str = "{}",
    entry_id: Optional[int] = None,
    source_strands: Optional[str] = None,
    profile: str = "",
    db_path: Optional[Path] = None,
) -> Optional[int]:
    """创建或更新 theme（v6.5.4: 原 topic_wiki → themes 表适配）。

    Args:
        entry_id: 传入则 UPDATE themes，None 则 INSERT。
        source_strands: v6.4 新字段。themes 表无 source_ids 列，
            仅写 source_strands（旧 source_ids 参数保留仅为兼容旧调用签名）。

    Returns:
        theme_id（新建时是自增 id，更新时是原 id）。
    """
    conn = _get_topic_conn(db_path)
    now = time.time()
    src_val = source_strands if source_strands is not None else source_ids
    try:
        if entry_id is not None:
            conn.execute(
                """UPDATE themes SET
                   title=?, overview=?, centroid_json=?,
                   changes_json=?, key_facts_json=?, open_items_json=?,
                   source_strands=?, updated_at=?
                   WHERE theme_id=?""",
                (title, overview, centroid_json,
                 changes_json, key_facts_json, open_items_json,
                 src_val, now, entry_id),
            )
            conn.commit()
            return entry_id
        else:
            cur = conn.execute(
                """INSERT INTO themes
                   (title, overview, centroid_json,
                    changes_json, key_facts_json, open_items_json,
                    source_strands, profile, updated_at, created_at)
                   VALUES (?,?,?, ?,?,?, ?,?,?,?)""",
                (title, overview, centroid_json,
                 changes_json, key_facts_json, open_items_json,
                 src_val, profile, now, now),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.Error as exc:
        logger.warning("[CA] upsert_wiki_entry failed: %s", exc)
        return None


# ── Wiki 动态参数存储（v5.19）──

WIKI_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

WIKI_MERGE_THRESHOLD_DEFAULT = 0.70


def get_wiki_threshold(db_path: Optional[Path] = None) -> float:
    """从 wiki_meta 读取当前阈值，无则返回默认值 0.70。"""
    conn = _get_topic_conn(db_path)
    try:
        conn.execute(WIKI_META_SCHEMA)
        row = conn.execute(
            "SELECT value FROM wiki_meta WHERE key='merge_threshold'"
        ).fetchone()
        if row:
            return float(row[0])
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return WIKI_MERGE_THRESHOLD_DEFAULT


def set_wiki_threshold(value: float, db_path: Optional[Path] = None) -> bool:
    """写入 wiki_meta 阈值，供下一轮 merge 使用。"""
    conn = _get_topic_conn(db_path)
    try:
        conn.execute(WIKI_META_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO wiki_meta (key, value) VALUES ('merge_threshold', ?)",
            (str(value),),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("[CA] set_wiki_threshold failed: %s", exc)
        return False


def record_block_cooccurrences(
    session_id: str,
    topic_id: int,
    reality_ids: list,
    profile: str = "",
    db_path: Optional[Path] = None,
) -> int:
    """块归并完成后：同块 reality 两两记共现（幂等，同块去重）。

    聚合键铁律（2026-08-03 实证）：topic_id 是 session 内编号，
    跨 session 同名 topic_id 是不同块 → (session_id, topic_id) 复合键。

    Args:
        reality_ids: 该块 strand 归并到的 reality/theme 集合
            （即 run_theme_merge 返回的 theme_ids；去重后 ≥2 才记）。
    Returns:
        本次新增条数（同块重复调用返回 0）。
    """
    conn = _get_topic_conn(db_path)
    ids = sorted({int(x) for x in reality_ids if x is not None})
    if len(ids) < 2:
        return 0
    now = time.time()
    n = 0
    try:
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO cooccurrence_events "
                    "(session_id, topic_id, profile, reality_a, reality_b, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, topic_id, profile, a, b, now),
                )
                n += cur.rowcount
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("[CA_STORE] record_block_cooccurrences failed: %s", exc)
        return 0
    return n


def query_cooccurrences(
    profile: str = "",
    since: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> tuple:
    """聚合共现边（决策 38 图模型数据源）。

    Returns:
        (edges, deg, n_blocks)
        edges:    {(reality_a, reality_b): weight}（a < b 规范化，无向）
        deg:      {reality_id: 总共现权重}（PMI 分母）
        n_blocks: 共现来源块数（PMI 分母 N）
    """
    conn = _get_topic_conn(db_path)
    sql = "SELECT reality_a, reality_b, COUNT(*) FROM cooccurrence_events WHERE 1=1"
    params: list = []
    if profile:
        sql += " AND profile = ?"
        params.append(profile)
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " GROUP BY reality_a, reality_b"
    rows = conn.execute(sql, params).fetchall()
    edges = {(int(a), int(b)): int(w) for a, b, w in rows}
    deg: dict = {}
    for (a, b), w in edges.items():
        deg[a] = deg.get(a, 0) + w
        deg[b] = deg.get(b, 0) + w
    n_sql = ("SELECT COUNT(DISTINCT session_id || ':' || topic_id) "
             "FROM cooccurrence_events")
    n_params: list = []
    if profile:
        n_sql += " WHERE profile = ?"
        n_params.append(profile)
    n_blocks = conn.execute(n_sql, n_params).fetchone()[0]
    return edges, deg, n_blocks


def query_themes_by_semantics(
    q_emb: list[float],
    profile: str,
    limit: int = 3,
    exclude_session_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """语义检索 theme（v6.5，替代 query_wiki_by_semantics）：余弦排序取 top N。

    返回 theme dict：theme_id/title/overview/ooda/key_facts/open_items（供注入）。
    跨会话、非当前 session 的 theme 才有意义 → 必须排除当前 session（v6.5.2 修复：
    原实现未排除，FAR 切换可能召回本会话刚归并的 theme 造成自注入）。
    无匹配 → 空列表。
    """
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT theme_id, title, overview, centroid_json, ooda_json, "
            "       key_facts_json, open_items_json, source_strands "
            "FROM themes "
            "WHERE centroid_json IS NOT NULL AND centroid_json != '' "
            "  AND profile = ? "
            "ORDER BY updated_at DESC",
            (profile,),
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        logger.warning("[CA] query_themes_by_semantics failed: %s", exc)
        return []

    if not rows:
        return []

    # 排除当前 session 的 theme（source_strands 含该 session_id）
    if exclude_session_id:
        filtered: list[tuple] = []
        for row in rows:
            ss_raw = row[7]
            hit = False
            if ss_raw:
                try:
                    ss = json.loads(ss_raw)
                    if isinstance(ss, dict) and exclude_session_id in ss:
                        hit = True
                except (json.JSONDecodeError, TypeError):
                    pass
            if not hit:
                filtered.append(row)
        rows = filtered
        if not rows:
            return []

    scored: list[tuple[float, dict]] = []
    for row in rows:
        centroid_raw = row[3]
        if not centroid_raw:
            continue
        try:
            centroid = json.loads(centroid_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        sim = _cosine_similarity_inline(q_emb, centroid)
        ooda = {}
        try:
            ooda = json.loads(row[4]) if row[4] else {}
        except (json.JSONDecodeError, TypeError):
            ooda = {}
        scored.append((sim, {
            "theme_id": row[0],
            "title": row[1] or "",
            "overview": row[2] or "",
            "ooda": ooda,
            "key_facts": json.loads(row[5]) if row[5] else [],
            "open_items": json.loads(row[6]) if row[6] else [],
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored if _ > 0][:limit]


def _cosine_similarity_inline(vec_a: list[float], vec_b: list[float]) -> float:
    """局部余弦相似度（避免跨模块依赖 topic_manager）。"""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ═══════════════════════════════════════════════════════════
# wiki_associations — 图关联 API
# ═══════════════════════════════════════════════════════════


def _source_to_ov_uri(source_file: str) -> str:
    """将 graphify 的 source_file 路径映射到 OpenViking 资源 URI。"""
    if not source_file:
        return ""
    # docs/{architecture,decisions,testing}/xxx → viking://resources/projects/context-assembler/
    # 目录重组（2026-08-07 决策 43 v4）：architecture/ + decisions/ 分类
    if source_file.startswith("docs/architecture/"):
        tail = source_file[len("docs/"):]
        return f"viking://resources/projects/context-assembler/{tail}"
    if source_file.startswith("docs/decisions/"):
        tail = source_file[len("docs/"):]
        return f"viking://resources/projects/context-assembler/{tail}"
    if source_file.startswith("docs/testing/"):
        tail = source_file[len("docs/"):]
        return f"viking://resources/projects/context-assembler/{tail}"
    if source_file.startswith("docs/"):
        tail = source_file[len("docs/"):]
        return f"viking://resources/projects/context-assembler/{tail}"
    return ""


def build_wiki_associations(
    graph_path: str,
    db_path: Optional[Path] = None,
) -> int:
    """从 graph.json 为每个 wiki entry 查找关联代码节点。

    匹配规则：wiki entry 的 title/overview 中的关键字与图节点 label 做子串匹配。
    结果写入 wiki_associations 表。

    Returns:
        写入的关联条目数。
    """
    import re

    conn = _get_topic_conn(db_path)
    try:
        with open(graph_path) as f:
            graph = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("[CA] build_wiki_associations: cannot load graph: %s", exc)
        return 0

    # 获取所有 theme（v6.5: themes 表替代 topic_wiki）
    entries = conn.execute(
        "SELECT theme_id, title, overview FROM themes"
    ).fetchall()
    if not entries:
        return 0

    # 构建代码节点索引：label → [nodes]
    code_nodes: dict[str, list[dict]] = {}
    for n in graph.get("nodes", []):
        if n.get("_origin") == "wiki":
            continue  # 跳过 wiki 节点自身
        label = n.get("label", "").lower()
        if not label:
            continue
        code_nodes.setdefault(label, [])
        # 去重
        if not any(c["id"] == n["id"] for c in code_nodes[label]):
            code_nodes[label].append({
                "id": n["id"],
                "label": n.get("label", ""),
                "file_type": n.get("file_type", ""),
                "source_file": n.get("source_file", ""),
                "community": n.get("community", 0),
            })

    # 提取中文/英文关键词
    def extract_keywords(text: str) -> list[str]:
        words = set()
        # 英文词（>=3 字符，首字母大写）
        for m in re.finditer(r"[A-Z][a-z]{2,}", text):
            words.add(m.group().lower())
        # 下划线连接的标识符
        for m in re.finditer(r"[a-z_][a-z0-9_]{2,}", text.lower()):
            words.add(m.group())
        # 中文双字词
        text_cn = text.encode("ascii", "ignore").decode()
        if text != text_cn:  # 有中文
            for m in re.finditer(r"[\u4e00-\u9fff]{2}", text):
                words.add(m.group())
        return list(words)

    # 为每个 entry 扫描关联
    total = 0
    seen: set[tuple] = set()
    for eid, title, overview in entries:
        text = f"{title or ''} {overview or ''}"
        kws = extract_keywords(text)
        for kw in kws:
            for candidates in code_nodes.values():
                for cn in candidates:
                    if kw in cn["label"].lower():
                        key = (eid, cn["id"])
                        if key in seen:
                            break
                        seen.add(key)
                        # 计算匹配强度
                        strength = len(kw) / max(len(cn["label"]), 1)
                        strength = min(max(strength, 0.3), 1.0)
                        ov_uri = _source_to_ov_uri(cn.get("source_file", ""))
                        try:
                            conn.execute(
                                """INSERT OR REPLACE INTO wiki_associations
                                   (theme_id, graph_node_id, node_label, node_type,
                                    relation, strength, source_file, ov_uri)
                                   VALUES (?,?,?,?, 'related', ?, ?, ?)""",
                                (eid, cn["id"], cn["label"],
                                 cn.get("file_type", "code"),
                                 round(strength, 3),
                                 cn.get("source_file", ""),
                                 ov_uri),
                            )
                            total += 1
                        except sqlite3.Error:
                            continue
                        break  # 一个 keyword 只匹配一个节点

    conn.commit()
    logger.info("[CA] build_wiki_associations: %d entries → %d associations",
                len(entries), total)
    return total


def query_wiki_associations(
    theme_ids: list[int],
    limit: int = 5,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """查询 theme 的图关联节点。"""
    if not theme_ids:
        return []
    conn = _get_topic_conn(db_path)
    placeholders = ",".join("?" for _ in theme_ids)
    try:
        cur = conn.execute(
            f"SELECT graph_node_id, node_label, node_type, relation, "
            f"       strength, source_file, ov_uri "
            f"FROM wiki_associations "
            f"WHERE theme_id IN ({placeholders}) "
            f"ORDER BY strength DESC LIMIT ?",
            (*theme_ids, limit),
        )
        cols = ["node_id", "label", "type", "relation", "strength", "source_file", "ov_uri"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("[CA] query_wiki_associations failed: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════
# L4 空闲精炼 — refinement_meta CRUD
# ═══════════════════════════════════════════════════════════════


def write_refinement_meta(
    last_refined_turn: int = 0,
    tasks_run: Optional[list] = None,
    entries_reviewed: int = 0,
    entries_modified: int = 0,
    entries_split: int = 0,
    fcts_cross_checked: int = 0,
    inconsistencies: int = 0,
    associations_added: int = 0,
    graphify_synced: int = 0,
    duration_sec: float = 0.0,
    status: str = "completed",
    db_path: Optional[Path] = None,
) -> bool:
    """写入一轮精炼元数据。"""
    conn = _get_topic_conn(db_path)
    now = time.time()
    conn.execute(
        """INSERT INTO refinement_meta
           (refined_at, last_refined_turn, global_turn_max, tasks_run,
            entries_reviewed, entries_modified, entries_split,
            fcts_cross_checked, inconsistencies, associations_added,
            graphify_synced, duration_sec, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, last_refined_turn, 0,
         json.dumps(tasks_run or []),
         entries_reviewed, entries_modified, entries_split,
         fcts_cross_checked, inconsistencies, associations_added,
         graphify_synced, duration_sec, status),
    )
    conn.commit()
    return True


def get_last_refinement_meta(
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """获取最近一次精炼元数据。"""
    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT id, refined_at, last_refined_turn, global_turn_max, "
            "       tasks_run, entries_reviewed, entries_modified, "
            "       duration_sec, status "
            "FROM refinement_meta ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            return {
                "id": row[0],
                "refined_at": row[1],
                "last_refined_turn": row[2],
                "global_turn_max": row[3],
                "tasks_run": json.loads(row[4]) if row[4] else [],
                "entries_reviewed": row[5],
                "entries_modified": row[6],
                "duration_sec": row[7],
                "status": row[8],
            }
        return None
    except sqlite3.OperationalError:
        return None  # 表尚未创建
    except sqlite3.Error as exc:
        logger.warning("[CA] get_last_refinement_meta failed: %s", exc)
        return None
