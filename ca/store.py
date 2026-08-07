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
        return "【无历史回顾】——本轮所有内容相对空历史均为首次出现，必须选取核心发现输出 &lt;stage_tag&gt;/&lt;core_change&gt; 对"

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

    PRIMARY KEY (session_id, turn, seq)
);
"""


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
                  hdl_text: Optional[str] = None) -> bool:
    """v5.0 INSERT OR REPLACE — 简化参数，无 v4 兼容映射。"""
    if written_at is None:
        written_at = time.time()
    for attempt in range(Config.DB_MAX_RETRY):
        try:
            store.conn.execute(
                """INSERT OR REPLACE INTO turn_stream
                   (session_id, turn, seq, role, Elm,
                    tool_name, tool_call_id, args_json, status, duration_ms,
                    tool_calls_json, finish_reason,
                    usage_prompt_tokens, usage_completion_tokens,
                    biz_category, written_at,
                    Fct, Hdl)
                   VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?)""",
                (session_id, turn, seq, role, elm_text,
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
      biz_category, written_at, Fct, Hdl
    Used by CacheBuilder.build to warm cache from DB.
    """
    try:
        cur = store.conn.execute(
            "SELECT turn, seq, role, Elm, tool_name, tool_call_id, args_json, "
            "       status, duration_ms, tool_calls_json, finish_reason, "
            "       usage_prompt_tokens, usage_completion_tokens, "
            "       biz_category, written_at, Fct, Hdl "
            "FROM turn_stream WHERE session_id=? ORDER BY turn, seq",
            (session_id,),
        )
        cols = ["turn", "seq", "role", "Elm", "tool_name", "tool_call_id", "args_json",
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


# ═══════════════════════════════════════════════════════════
# v5.10 — topic_summaries 共享 DB（取代 OV Memory Provider）
# ═══════════════════════════════════════════════════════════

_TOPIC_STORE_CACHE: Dict[str, sqlite3.Connection] = {}
_TOPIC_STORE_LOCK = threading.Lock()


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
    """获取共享 topic DB 的连接（线程级缓存，单例 per-process）。"""
    if isinstance(db_path, sqlite3.Connection):
        # BUG-11 防御：调用方误传有效连接时直接复用（绕过路径缓存）。
        return db_path
    p = db_path or _get_topic_store_path()
    key = str(p.resolve())
    with _TOPIC_STORE_LOCK:
        if key in _TOPIC_STORE_CACHE:
            conn = _TOPIC_STORE_CACHE[key]
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
        _TOPIC_STORE_CACHE[key] = conn
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
"""


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
    source_strand: Optional[dict] = None,
    query_centroid_json: Optional[str] = None,
    query_count: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """更新 reality（决策 41）：timeline 追加 + 字段覆盖（None 不变）。

    timeline_entry: {seq?, topic_id, turns, session_id, overview}；
       seq 缺省 = 现有最大 seq + 1。
    source_strand: {session_id, strand_id} — 合并进 source_strands。
    query_centroid_json/query_count: 提问云形心增量维护（调用方算好新值）。
    """
    conn = _get_topic_conn(db_path)
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
        if timeline_entry:
            entry = dict(timeline_entry)
            if "seq" not in entry:
                entry["seq"] = max([e.get("seq", 0) for e in new_timeline] or [0]) + 1
            new_timeline.append(entry)
        if changes:
            new_timeline.append({"seq": max([e.get("seq", 0) for e in new_timeline] or [0]) + 1,
                                 "changes": list(changes),
                                 "session_id": (source_strand or {}).get("session_id", "")})

        new_source = {}
        if row[1]:
            try:
                new_source = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                new_source = {}
        if source_strand:
            sid = source_strand.get("session_id")
            s_id = source_strand.get("strand_id")
            if sid:
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
            cs = json.loads(row[4]) if row[4] else {}
        except (json.JSONDecodeError, TypeError):
            cs = {}
        try:
            tl = json.loads(row[3]) if row[3] else []
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


def collect_turn_fcts(
    store, session_id: str, turns: list[int],
) -> list[dict]:
    """从 turn_stream 收集话题块各轮的 Fct 数据。

    新格式（v5+ turn_stream）：Fct JSON 结构为
      {"changes": [{"stage_tag": "已实施", "core_change": "...", "user_request": "..."}, ...],
       "consensus": [...], "objective_facts": [...], "new_materials": [...]}

    返回 [{"turn": n, "hdl": "...", "changes": [str, ...],
            "tags": {str: str}, "todos": [str, ...], "user_requests": [str, ...],
            "consensus": [str, ...], "key_facts_supp": [str, ...],
            "new_materials": [str, ...]}, ...]
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
        if fct:
            try:
                fct_data = json.loads(fct)
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
                    # 收集 change 级的 user_request
                    ur = c.get("user_request", "").strip()
                    if ur:
                        user_requests.append(ur)
                entry: dict[str, Any] = {
                    "turn": t,
                    "hdl": hdl,
                    "changes": clean_changes,
                    "tags": tags,
                    "ooda_tags": ooda_tags,
                    "todos": todos,
                    "user_requests": user_requests,
                }
                # 提取 Fct 顶层补充字段
                consensus = fct_data.get("consensus")
                if isinstance(consensus, list):
                    entry["consensus"] = [str(x).strip() for x in consensus if x]
                obj_facts = fct_data.get("objective_facts")
                if isinstance(obj_facts, list):
                    entry["key_facts_supp"] = [str(x).strip() for x in obj_facts if x]
                new_mat = fct_data.get("new_materials")
                if isinstance(new_mat, list):
                    entry["new_materials"] = [str(x).strip() for x in new_mat if x]
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
    # docs/wiki/xxx → viking://resources/projects/context-assembler/
    if source_file.startswith("docs/wiki/"):
        tail = source_file[len("docs/wiki/"):]
        return f"viking://resources/projects/context-assembler/design/{tail}"
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
