"""ca/__init__.py — ContextAssembler 主引擎薄壳 (v5.10)

设计决策: C-010 (A-stage 解耦), E-stage (写即落盘)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md
  - A-stage: topic-aware 三级替换 (ca/a_stage.py)
  - E-stage: 5 钩子同步写入 turn_stream (ca/e_stage.py)
  - F-stage: daemon 异步 LLM 摘要 (ca/f_stage.py)

整合 v5.0 模块拆分：
  e_stage.py   — E-stage 写即落盘 + 代码级摘要
  f_stage.py   — F-stage 异步 LLM 摘要
  lstage.py    — L-stage 生命周期 + 补全线程
  a_stage.py   — A-stage 话题注入
  store.py     — SQLite 持久化
  cache.py     — AssemblyCache / CacheBuilder
  grade.py     — Elm/Fct/Hdl 常量

ContextAssembler 通过混入（mixin）继承聚合所有 stage 方法。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import ASSEMBLE_OK, ASSEMBLE_PENDING_BACKFILL, Config
from .exceptions import FctTruncatedException
from .store import SQLiteStore
from .cache import AssemblyCache, CacheBuilder
from .embedding import EmbeddingClient
from .stats import AssembleStats
from .tool_summarizer import ToolSummarizer

# ── 模块化 stage mixins ──
from .e_stage import EStageMixin
from .f_stage import FStageMixin
from .lstage import LStageMixin
from .a_stage import AStageMixin

logger = logging.getLogger(__name__)

try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"


# ── SessionManager ──

class SessionManager:
    """全局会话管理器，支持 LRU、TTL 和异步安全销毁。"""

    def __init__(self, max_sessions: int = 50, session_ttl: int = 1800):
        self._sessions: OrderedDict[str, ContextAssembler] = OrderedDict()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._last_access: Dict[str, float] = {}
        self._destroying: Dict[str, bool] = {}
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def get(self, session_id: str, db_path: str) -> ContextAssembler:
        with self._lock:
            while session_id in self._destroying:
                self._cond.wait(timeout=Config.SHUTDOWN_TIMEOUT)
                if session_id in self._destroying:
                    logger.error("Timeout waiting for %s destruction, forcing clear", session_id)
                    self._destroying.pop(session_id, None)
            if session_id in self._sessions:
                self._sessions.move_to_end(session_id)
                self._last_access[session_id] = time.time()
                return self._sessions[session_id]
            engine = ContextAssembler(db_path=db_path, session_id=session_id)
            self._sessions[session_id] = engine
            self._last_access[session_id] = time.time()
            to_evict = []
            while len(self._sessions) > self._max_sessions:
                sid, eng = self._sessions.popitem(last=False)
                self._last_access.pop(sid, None)
                to_evict.append((sid, eng))
        for sid, eng in to_evict:
            self._start_destroy(sid, eng)
        return engine

    def remove(self, session_id: str):
        with self._lock:
            eng = self._sessions.pop(session_id, None)
            self._last_access.pop(session_id, None)
        if eng:
            self._start_destroy(session_id, eng)

    def _start_destroy(self, sid: str, eng: ContextAssembler):
        with self._lock:
            if sid in self._destroying:
                return
            self._destroying[sid] = True
        threading.Thread(target=self._destroy_and_notify, args=(sid, eng), daemon=True).start()

    def _destroy_and_notify(self, sid: str, eng: ContextAssembler):
        try:
            destroy_thread = threading.Thread(target=eng.destroy, daemon=True)
            destroy_thread.start()
            destroy_thread.join(timeout=Config.SHUTDOWN_TIMEOUT)
            if destroy_thread.is_alive():
                logger.warning("Destroy for session %s timed out", sid)
        except Exception as exc:
            logger.warning("Destroy session %s failed: %s", sid, exc)
        finally:
            with self._lock:
                self._destroying.pop(sid, None)
                self._cond.notify_all()

    def _cleanup_loop(self):
        while True:
            time.sleep(300)
            to_destroy = []
            with self._lock:
                now = time.time()
                stale_ids = [sid for sid, ts in self._last_access.items() if now - ts > self._session_ttl]
                for sid in stale_ids:
                    eng = self._sessions.pop(sid, None)
                    self._last_access.pop(sid, None)
                    if eng:
                        to_destroy.append((sid, eng))
                while len(self._sessions) > self._max_sessions and self._last_access:
                    oldest_sid = min(self._last_access.keys(), key=lambda k: self._last_access.get(k, 0))
                    eng = self._sessions.pop(oldest_sid, None)
                    self._last_access.pop(oldest_sid, None)
                    if eng:
                        to_destroy.append((oldest_sid, eng))
            for sid, eng in to_destroy:
                self._start_destroy(sid, eng)

    def reset_all(self):
        with self._lock:
            engines = list(self._sessions.values())
            self._sessions.clear()
            self._last_access.clear()
        for eng in engines:
            self._start_destroy("reset_all", eng)


class ContextAssembler(EStageMixin, FStageMixin, LStageMixin, AStageMixin):
    """Hermes 上下文组装置（薄壳 + stage mixin 聚合）。

    生命周期：
      __init__         → 初始化 store / cache / LLM / embedding / 补全线程
      destroy          → 清理所有资源
      process_turn_f_stage → 路由：E-stage 写码级 Fct → 需 LLM 则起 F-stage 线程

    Stage 方法来自 mixin：
      EStageMixin  — _on_api_response_v5, _on_post_tool_call_v5, _update_fct_v5
      FStageMixin  — _run_f_stage, _call_llm_for_fct, _extract_hdl
      LStageMixin  — reset, wait_for_pending
      AStageMixin  — _grade_topics_by_radius
    """

    def __init__(self, db_path: str = "./ca_store.db", session_id: str = "default"):
        self.store = SQLiteStore(db_path)
        self.embed_client = EmbeddingClient()
        self.context_length = Config.CONTEXT_LENGTH
        self._session_id = session_id
        builder = CacheBuilder(self.store)
        self.cache: AssemblyCache = builder.build(session_id)
        self._pending_tasks: Dict[Tuple[int, int], threading.Thread] = {}
        self._task_lock = threading.Lock()
        self._turn_counter = self._restore_turn_index()

        self.tool_summarizer = ToolSummarizer()
        self._topic_tool_boost: Set[int] = set()
        self.stats = AssembleStats()
        self._original_messages: Optional[List[Dict]] = None

        # v5.0 E-stage 属性
        self._current_turn: int = 0
        self._seq_counter: Dict[int, int] = {}            # turn → max_seq
        self._tool_seq_map: Dict[str, Tuple[int, int]] = {}  # tool_call_id → (turn, seq)

    def _restore_turn_index(self) -> int:
        try:
            from .store import max_turn_v5
            return max_turn_v5(self.store, self._session_id)
        except Exception as e:
            logger.error("Failed to restore turn index: %s, defaulting to 0", e)
            return 0

    def destroy(self):
        """清理所有资源。"""
        self.wait_for_pending(Config.SHUTDOWN_TIMEOUT)
        self.cache.destroy()
        try:
            self.store.close()
            self.embed_client.close()
        except Exception as e:
            logger.warning("Resource cleanup partially failed: %s", e)
        with self._task_lock:
            self._pending_tasks.clear()

    def process_turn_f_stage(self, turn_index: int, fin_seq: int) -> int:
        """F-stage 路由：LLM 摘要写入 fin 行的入口。

        流程：
          1. 检查指定 fin 行（fin_seq）是否已有 Fct
             → 有则跳过 F-stage
          2. 检测 bg_review write_origin → 同步写代码级 Fct，跳过 F-stage
          3. 否则 → 启 F-stage 线程（LLM）

        每条 fin 行独立触发 F-stage，互不阻塞。
        """

        # ── 1. 检查指定 fin 行已有 Fct ──
        try:
            cur = self.store.conn.execute(
                "SELECT Fct FROM turn_stream "
                "WHERE session_id=? AND turn=? AND seq=? AND role='assistant' AND finish_reason='stop'",
                (self._session_id, turn_index, fin_seq),
            )
            row = cur.fetchone()
            if row and row[0]:
                logger.info("[CA] process_turn_f_stage: turn %d fin_seq %d already has Fct, skipping", turn_index, fin_seq)
                return turn_index
        except Exception:
            pass

        with self._task_lock:
            if (turn_index, fin_seq) in self._pending_tasks and self._pending_tasks[(turn_index, fin_seq)].is_alive():
                logger.info("[CA] process_turn_f_stage: turn %d fin_seq %d already pending, returning", turn_index, fin_seq)
                return turn_index
            self._turn_counter = turn_index

        # ── 2. 检测 bg_review → 同步写代码级 Fct ──
        _bg_review = (get_current_write_origin() == "background_review")
        if _bg_review:
            logger.info("[CA] process_turn_f_stage: background review for turn %d fin_seq %d, writing code-level Fct inline",
                        turn_index, fin_seq)
            from .store import read_turn_elm_rows
            rows = read_turn_elm_rows(self.store, self._session_id, turn_index)
            user_elm = ""
            for seq, role, elm_text, tool_name, tool_call_id in (rows or []):
                if role == "user":
                    user_elm = elm_text or ""
                    break
            _brief = (user_elm or "")[:80].strip() or "后台审查"
            cleaned = {
                "changes": [{"stage_tag": "已实施", "core_change": _brief}],
                "core_change": _brief,
                "_assemble_status": ASSEMBLE_OK,
            }
            self._update_fct_v5(self._session_id, turn_index, fin_seq,
                               json.dumps(cleaned, ensure_ascii=False), _brief)
            logger.info("[CA] process_turn_f_stage: wrote code-level Fct for bg_review turn %d fin_seq %d: %s",
                        turn_index, fin_seq, _brief)
            return turn_index

        # ── 3. 启 F-stage 线程（LLM 路径） ──
        logger.info("[CA] process_turn_f_stage: starting F-stage for turn %d fin_seq %d", turn_index, fin_seq)

        thread = threading.Thread(
            target=self._run_f_stage,
            args=(self._session_id, turn_index, fin_seq),
            daemon=True,
            name=f"CA-FStage-{turn_index}-{fin_seq}",
        )
        with self._task_lock:
            self._pending_tasks[(turn_index, fin_seq)] = thread
        logger.info("[CA] process_turn_f_stage: starting thread CA-FStage-%d-%d", turn_index, fin_seq)
        thread.start()
        logger.info("[CA] process_turn_f_stage: thread CA-FStage-%d-%d started", turn_index, fin_seq)
        return turn_index


# ── 模块级 SessionManager 单例 ──
session_manager = SessionManager(max_sessions=Config.CACHE_MAX_SESSIONS,
                                 session_ttl=Config.SESSION_TTL)
