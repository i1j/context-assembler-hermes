"""
ca/store.py — SQLite 持久化存储层 (v4.4.0 alpha)

功能：
- WAL 模式 + busy_timeout + 后台 checkpoint 守护线程。
- 线程本地连接，空闲超时自动关闭。
- 写入重试（指数退避），批量写入单事务。
- 内存缓存 session_id 列表（24h TTL），写操作后失效。
- 嵌入 BLOB 安全解包（损坏时返回 None）。
- 表结构完整性检查，损坏自动重建。
- 新增列：turn_type, tool_sub_index, backfill_attempts, l2_text, _assemble_status。
- 提供独立迁移脚本（见 migration_v4_3_to_v4_4.py）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2
INITIAL_BACKFILL_ATTEMPTS = 0

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS turn_cache (
    session_id    TEXT    NOT NULL,
    turn_index    INTEGER NOT NULL,
    l0_text       TEXT    NOT NULL DEFAULT '',
    l1_text       TEXT    NOT NULL DEFAULT '',
    l0_embedding  BLOB,
    l1_embedding  BLOB,
    bm25_tokens   TEXT,
    token_offset  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    turn_type     TEXT    NOT NULL DEFAULT 'dialogue',
    backfill_attempts INTEGER NOT NULL DEFAULT {INITIAL_BACKFILL_ATTEMPTS},
    tool_sub_index INTEGER NOT NULL DEFAULT 0,
    l2_text       TEXT,
    _assemble_status INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)
);

CREATE INDEX IF NOT EXISTS idx_tc_session ON turn_cache(session_id);
CREATE INDEX IF NOT EXISTS idx_tc_offset ON turn_cache(session_id, token_offset);
CREATE INDEX IF NOT EXISTS idx_tc_turn_type ON turn_cache(session_id, turn_type);
CREATE INDEX IF NOT EXISTS idx_tc_assemble_status ON turn_cache(_assemble_status);
CREATE INDEX IF NOT EXISTS idx_pending_backfill ON turn_cache(session_id, turn_type, _assemble_status);

CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '{_SCHEMA_VERSION}');
"""


def _pack_f32(arr: List[float]) -> bytes:
    if not arr:
        return b""
    return struct.pack(f"<{len(arr)}f", *arr)


def _unpack_f32(blob: bytes) -> Optional[List[float]]:
    if not blob:
        return []
    if len(blob) % 4 != 0:
        logger.warning("Embedding BLOB length %d not multiple of 4, returning None", len(blob))
        return None
    try:
        return list(struct.unpack(f"<{len(blob)//4}f", blob))
    except struct.error as e:
        logger.warning("Failed to unpack embedding: %s", e)
        return None


class SQLiteStore:
    def __init__(self, db_path: str | Path, checkpoint_interval: Optional[int] = None):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._checkpoint_stop = threading.Event()
        self._checkpoint_interval = checkpoint_interval or Config.DB_CHECKPOINT_INTERVAL
        if self._checkpoint_interval <= 0:
            logger.warning("Invalid checkpoint interval %d, resetting to 300", self._checkpoint_interval)
            self._checkpoint_interval = 300
        self._checkpoint_thread: Optional[threading.Thread] = None
        self._start_checkpoint_daemon(self._checkpoint_interval)

        self._session_cache: List[str] = []
        self._session_cache_time: float = 0.0
        self._cache_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        now = time.monotonic()
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            if now - getattr(self._local, 'last_used', now) > Config.DB_IDLE_TIMEOUT_SECONDS:
                try:
                    self._local.conn.close()
                    logger.debug("Idle DB connection recycled")
                except Exception as exc:
                    logger.debug("Error closing idle connection: %s: %s", type(exc).__name__, exc)
                self._local.conn = None

        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=10, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            cur = self._local.conn.execute("PRAGMA journal_mode")
            row = cur.fetchone()
            if row and row[0].lower() != "wal":
                logger.warning("WAL mode could not be enabled (current=%s), performance may degrade", row[0])
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute(f"PRAGMA busy_timeout={Config.DB_BUSY_TIMEOUT_MS}")
            self._local.conn.executescript(_SCHEMA_SQL)
            self._check_schema()

        self._local.last_used = now
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def _check_schema(self) -> None:
        conn = self.conn
        try:
            row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
            if row and int(row[0]) != _SCHEMA_VERSION:
                logger.warning("Schema version mismatch, rebuilding.")
                self._rebuild()
                return
        except sqlite3.Error:
            logger.warning("Schema version check failed, assuming fresh DB.")
            return

        try:
            cur = conn.execute("PRAGMA integrity_check")
            res = cur.fetchone()
            if res and res[0] != "ok":
                logger.warning("Database integrity check failed: %s, rebuilding.", res[0])
                self._rebuild()
        except sqlite3.Error:
            pass

    def _rebuild(self) -> None:
        conn = self.conn
        conn.executescript("DROP TABLE IF EXISTS turn_cache; DROP TABLE IF EXISTS _meta;")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def _start_checkpoint_daemon(self, interval: int) -> None:
        def _daemon():
            while not self._checkpoint_stop.is_set():
                for _ in range(interval):
                    if self._checkpoint_stop.is_set():
                        return
                    time.sleep(1)
                try:
                    tmp = sqlite3.connect(str(self._db_path))
                    tmp.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    tmp.close()
                except Exception as e:
                    logger.warning("Checkpoint error: %s", e)

        self._checkpoint_thread = threading.Thread(target=_daemon, daemon=True)
        self._checkpoint_thread.start()

    def close(self) -> None:
        self._checkpoint_stop.set()
        if hasattr(self._local, 'conn') and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    TurnRecord = Dict[str, Any]

    def write_turn(
        self,
        session_id: str,
        turn_index: int,
        *,
        l0_text: str = "",
        l1_text: str = "",
        l0_embedding: Optional[List[float]] = None,
        l1_embedding: Optional[List[float]] = None,
        bm25_tokens: Optional[List[str]] = None,
        token_offset: int = 0,
        turn_type: str = "dialogue",
        tool_sub_index: int = 0,
        l2_text: Optional[str] = None,
        _assemble_status: int = 0,
        max_retries: Optional[int] = None,
    ) -> bool:
        effective_retries = max_retries if max_retries is not None else Config.DB_MAX_RETRY
        for attempt in range(effective_retries):
            try:
                self.conn.execute(
                    """INSERT OR REPLACE INTO turn_cache
                       (session_id, turn_index, l0_text, l1_text,
                        l0_embedding, l1_embedding, bm25_tokens, token_offset,
                        turn_type, backfill_attempts, tool_sub_index, l2_text, _assemble_status)
                       VALUES (:session_id, :turn_index, :l0_text, :l1_text,
                               :l0_embedding, :l1_embedding, :bm25_tokens, :token_offset,
                               :turn_type, :backfill_attempts, :tool_sub_index, :l2_text, :_assemble_status)""",
                    {
                        "session_id": session_id,
                        "turn_index": turn_index,
                        "l0_text": l0_text,
                        "l1_text": l1_text,
                        "l0_embedding": _pack_f32(l0_embedding) if l0_embedding else None,
                        "l1_embedding": _pack_f32(l1_embedding) if l1_embedding else None,
                        "bm25_tokens": json.dumps(bm25_tokens, ensure_ascii=False) if bm25_tokens else None,
                        "token_offset": token_offset,
                        "turn_type": turn_type,
                        "backfill_attempts": INITIAL_BACKFILL_ATTEMPTS,
                        "tool_sub_index": tool_sub_index,
                        "l2_text": l2_text,
                        "_assemble_status": _assemble_status,
                    },
                )
                self.conn.commit()
                self._invalidate_session_cache()
                return True
            except sqlite3.OperationalError as exc:
                if attempt < effective_retries - 1:
                    wait = 0.1 * (2 ** attempt)
                    logger.debug("DB locked, retrying in %.2fs (attempt %d/%d)", wait, attempt + 1, effective_retries)
                    time.sleep(wait)
                else:
                    logger.error("write_turn failed after %d retries: %s", effective_retries, exc)
                    return False
            except sqlite3.Error as exc:
                logger.error("write_turn error: %s", exc)
                return False
        return False

    def write_turns_batch(self, session_id: str, records: List[TurnRecord]) -> bool:
        if not records:
            return True

        defaults = {
            "turn_index": 0, "l0_text": "", "l1_text": "",
            "l0_embedding": None, "l1_embedding": None,
            "bm25_tokens": None, "token_offset": 0,
            "turn_type": "dialogue", "tool_sub_index": 0,
            "l2_text": None, "_assemble_status": 0,
        }

        try:
            conn = self.conn
            conn.execute("BEGIN TRANSACTION")
            for rec in records:
                vals = {**defaults, **rec}
                conn.execute(
                    """INSERT OR REPLACE INTO turn_cache
                       (session_id, turn_index, l0_text, l1_text,
                        l0_embedding, l1_embedding, bm25_tokens, token_offset,
                        turn_type, backfill_attempts, tool_sub_index, l2_text, _assemble_status)
                       VALUES (:session_id, :turn_index, :l0_text, :l1_text,
                               :l0_embedding, :l1_embedding, :bm25_tokens, :token_offset,
                               :turn_type, :backfill_attempts, :tool_sub_index, :l2_text, :_assemble_status)""",
                    {
                        "session_id": session_id,
                        "turn_index": vals["turn_index"],
                        "l0_text": vals["l0_text"],
                        "l1_text": vals["l1_text"],
                        "l0_embedding": _pack_f32(vals["l0_embedding"]) if vals["l0_embedding"] else None,
                        "l1_embedding": _pack_f32(vals["l1_embedding"]) if vals["l1_embedding"] else None,
                        "bm25_tokens": json.dumps(vals["bm25_tokens"], ensure_ascii=False) if vals["bm25_tokens"] else None,
                        "token_offset": vals["token_offset"],
                        "turn_type": vals["turn_type"],
                        "backfill_attempts": vals.get("backfill_attempts", INITIAL_BACKFILL_ATTEMPTS),
                        "tool_sub_index": vals["tool_sub_index"],
                        "l2_text": vals["l2_text"],
                        "_assemble_status": vals["_assemble_status"],
                    },
                )
            conn.commit()
            logger.debug("Batch write: %d records for session %s", len(records), session_id)
            self._invalidate_session_cache()
            return True
        except sqlite3.Error as exc:
            logger.error("Batch write failed for session %s: %s", session_id, exc)
            return False

    def _invalidate_session_cache(self):
        with self._cache_lock:
            self._session_cache = []
            self._session_cache_time = 0.0

    def read_session(self, session_id: str) -> List[TurnRecord]:
        cur = self.conn.execute(
            """SELECT turn_index, l0_text, l1_text, l0_embedding, l1_embedding,
                      bm25_tokens, token_offset, turn_type, backfill_attempts,
                      tool_sub_index, l2_text, _assemble_status
               FROM turn_cache WHERE session_id=? ORDER BY turn_index ASC""",
            (session_id,),
        )
        rows = []
        for row in cur:
            rows.append({
                "turn_index": row[0], "l0_text": row[1] or "", "l1_text": row[2] or "",
                "l0_embedding": _unpack_f32(row[3]) if row[3] else None,
                "l1_embedding": _unpack_f32(row[4]) if row[4] else None,
                "bm25_tokens": json.loads(row[5]) if row[5] else [],
                "token_offset": row[6], "turn_type": row[7] or "dialogue",
                "backfill_attempts": row[8] or 0, "tool_sub_index": row[9] or 0,
                "l2_text": row[10], "_assemble_status": row[11] or 0,
            })
        return rows

    def read_turn(self, session_id: str, turn_index: int) -> Optional[TurnRecord]:
        cur = self.conn.execute(
            """SELECT turn_index, l0_text, l1_text, l0_embedding, l1_embedding,
                      bm25_tokens, token_offset, turn_type, backfill_attempts,
                      tool_sub_index, l2_text, _assemble_status
               FROM turn_cache WHERE session_id=? AND turn_index=? AND turn_type='dialogue' LIMIT 1""",
            (session_id, turn_index),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "turn_index": row[0], "l0_text": row[1] or "", "l1_text": row[2] or "",
            "l0_embedding": _unpack_f32(row[3]) if row[3] else None,
            "l1_embedding": _unpack_f32(row[4]) if row[4] else None,
            "bm25_tokens": json.loads(row[5]) if row[5] else [],
            "token_offset": row[6], "turn_type": row[7], "backfill_attempts": row[8],
            "tool_sub_index": row[9] or 0, "l2_text": row[10], "_assemble_status": row[11],
        }

    def max_turn_index(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(turn_index) FROM turn_cache WHERE session_id=? AND turn_type='dialogue'",
            (session_id,),
        ).fetchone()
        return row[0] if row[0] is not None else -1

    def list_session_ids(self) -> List[str]:
        now = time.time()
        with self._cache_lock:
            if self._session_cache and (now - self._session_cache_time) < 86400:
                return list(self._session_cache)
        cur = self.conn.execute("SELECT DISTINCT session_id FROM turn_cache ORDER BY session_id")
        ids = [row[0] for row in cur]
        with self._cache_lock:
            self._session_cache = ids
            self._session_cache_time = now
        return ids

    def delete_session(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM turn_cache WHERE session_id=?", (session_id,))
        self.conn.commit()
        self._invalidate_session_cache()

    def get_pending_backfill(self, session_id: str, turn_type: str) -> List[TurnRecord]:
        cur = self.conn.execute(
            """SELECT turn_index, l0_text, l1_text, l0_embedding, l1_embedding,
                      bm25_tokens, token_offset, turn_type, backfill_attempts,
                      tool_sub_index, l2_text, _assemble_status
               FROM turn_cache
               WHERE session_id=? AND turn_type=? AND _assemble_status=1
               ORDER BY turn_index ASC""",
            (session_id, turn_type),
        )
        rows = []
        for row in cur:
            rows.append({
                "turn_index": row[0], "l0_text": row[1] or "", "l1_text": row[2] or "",
                "l0_embedding": _unpack_f32(row[3]) if row[3] else None,
                "l1_embedding": _unpack_f32(row[4]) if row[4] else None,
                "bm25_tokens": json.loads(row[5]) if row[5] else [],
                "token_offset": row[6], "turn_type": row[7], "backfill_attempts": row[8],
                "tool_sub_index": row[9] or 0, "l2_text": row[10], "_assemble_status": row[11],
            })
        return rows

    def increment_backfill_attempts(self, session_id: str, turn_index: int, turn_type: str, sub_index: int = 0):
        self.conn.execute(
            """UPDATE turn_cache SET backfill_attempts = backfill_attempts + 1
               WHERE session_id=? AND turn_index=? AND turn_type=? AND tool_sub_index=?""",
            (session_id, turn_index, turn_type, sub_index),
        )
        self.conn.commit()
