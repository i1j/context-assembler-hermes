"""
ca/__init__.py — ContextAssembler 主引擎 (v4.4.0 alpha)

整合所有修正：
- 子索引一致性 (_build_tool_key_map)
- A‑stage 解耦
- 单工具调用拆分
- 预算计算修正
- L‑stage 补全拆组
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import logging
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import Config
from .store import SQLiteStore
from .cache import AssemblyCache, CacheBuilder, BM25Snapshot, tokenise
from .retrieval import Retriever, cosine_similarity
from .embedding import EmbeddingClient
from .ooda_parser import OODAParser
from .post_process import robust_json_parse, clean_increment, parse_v1_markdown_xml, _safe_truncate
from .prompts import FCT_GENERATION_PROMPT
from .store import format_previous_summary_for_prompt
from .stats import AssembleStats

logger = logging.getLogger(__name__)


class FctTruncatedException(Exception):
    """LLM 输出被截断时抛出的异常。

    携带 response_text 以便降级代码读取部分输出。
    """
    def __init__(self, message: str = "L1 output truncated", response_text: str = ""):
        super().__init__(message)
        self.message = message
        self.response_text = response_text


try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"
from .tool_summarizer import ToolSummarizer


@dataclass
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


session_manager = SessionManager(max_sessions=Config.CACHE_MAX_SESSIONS,
                                 session_ttl=Config.SESSION_TTL)


@dataclass
class _TopicSwitchData:
    topic_id: int
    turn_indices: List[int]
    agg_text: str
    fct_texts: Dict[int, str]
    tool_group_l1: Dict[str, str]




@dataclass
class ContextAssembler:
    """Hermes 上下文组装引擎，支持工具轮摘要与异步补全。"""

    def __init__(self, db_path="./ca_store.db", session_id="default"):
        self.store = SQLiteStore(db_path)
        self.embed_client = EmbeddingClient()
        self.ooda_parser = OODAParser(self.embed_client)
        self.context_length = Config.CONTEXT_LENGTH
        self._session_id = session_id
        builder = CacheBuilder(self.store)
        self.cache: AssemblyCache = builder.build(session_id)
        self._pending_tasks: Dict[int, threading.Thread] = {}
        self._task_lock = threading.Lock()
        self._turn_counter = self._restore_turn_index()

        self.tool_summarizer = ToolSummarizer()
        self._topic_tool_boost: Set[int] = set()
        # (removed _tool_buffer, _api_sequence — E-stage write-on-receive in v5.0)
        self.stats = AssembleStats()
        self._original_messages: Optional[List[Dict]] = None

        from .lstage import BackfillThread
        self._dialogue_backfill = BackfillThread(self, 'dialogue', Config.BACKFILL_DIALOGUE_RATE)
        self._tool_backfill = BackfillThread(self, 'tool', Config.BACKFILL_TOOL_RATE)
        self._dialogue_backfill.start()
        self._tool_backfill.start()

        # 话题边界追踪 + OpenViking 提交
        self._last_topic_id: Optional[int] = None
        self._pending_ov_submit: Optional[_TopicSwitchData] = None

        # (removed _conv_encoding, _encoding_* — v5.0 uses turn_stream PK)

        # ── v5.0 E-stage 属性 ──
        self._current_turn: int = 0
        self._seq_counter: Dict[int, int] = {}       # turn → max_seq
        self._tool_seq_map: Dict[str, Tuple[int, int]] = {}  # tool_call_id → (turn, seq)


    def _restore_turn_index(self) -> int:
        try:
            idx = self.store.max_turn_index(self._session_id)
            if idx < 0:
                logger.info("No turns for session %s, starting at 0", self._session_id)
                return 0
            return idx
        except Exception as e:
            logger.error("Failed to restore turn index: %s, defaulting to 0", e)
            return 0


    def destroy(self):
        # (removed tool_buffer flush — E-stage write-on-receive in v5.0)
        # 1. 先等待所有 F‑stage 任务完成
        self.wait_for_pending(Config.SHUTDOWN_TIMEOUT)
        # 2. 再停止 L‑stage 线程
        self._dialogue_backfill.shutdown()
        self._tool_backfill.shutdown()
        self._dialogue_backfill.join(timeout=Config.SHUTDOWN_TIMEOUT)
        self._tool_backfill.join(timeout=Config.SHUTDOWN_TIMEOUT)
        if self._dialogue_backfill.is_alive() or self._tool_backfill.is_alive():
            logger.warning("L-stage threads did not exit cleanly")
        self.cache.destroy()
        try:
            self.store.close()
            self.embed_client.close()
        except Exception as e:
            logger.warning("Resource cleanup partially failed: %s", e)
        with self._task_lock:
            self._pending_tasks.clear()

    def _shutdown_cache_executor(self):
        if sys.version_info >= (3, 10):
            try:
                self.cache._rebuild_executor.shutdown(wait=True, timeout=Config.SHUTDOWN_TIMEOUT)
            except Exception:
                logger.warning("Cache executor shutdown timeout")
                self.cache._rebuild_executor.shutdown(wait=False)
        else:
            self.cache._rebuild_executor.shutdown(wait=False)

    # ---------- F‑stage ----------
    def process_turn_f_stage(self, turn_index: int) -> int:
        """F-stage 入口：异步启动 _run_f_stage。turn_index 由调用者确定。"""
        # 如果 turn 的 Fct 已在 E-stage 写入（如 bg_review），跳过 F-stage
        from .store import get_turn_ca_rows
        existing = get_turn_ca_rows(self.store, self._session_id, turn_index)
        if existing:
            for seq, role, fr, tc, fct in existing:
                if role == "user" and fct:
                    logger.info("[CA] process_turn_f_stage: turn %d already has Fct, skipping F-stage", turn_index)
                    return turn_index

        with self._task_lock:
            if turn_index in self._pending_tasks and self._pending_tasks[turn_index].is_alive():
                logger.info("[CA] process_turn_f_stage: turn %d already pending, returning", turn_index)
                return turn_index
            self._turn_counter = turn_index

        logger.info("[CA] process_turn_f_stage: starting F-stage for turn %d", turn_index)

        # 在主线程捕获 write_origin（ContextVar 不自动传播到 daemon 线程）
        _bg_review = (get_current_write_origin() == "background_review")
        if _bg_review:
            logger.info("[CA] process_turn_f_stage: background review detected for turn %d", turn_index)

        thread = threading.Thread(
            target=self._run_f_stage,
            args=(self._session_id, turn_index, _bg_review),
            daemon=True, name=f"CA-FStage-{turn_index}"
        )
        with self._task_lock:
            self._pending_tasks[turn_index] = thread
        logger.info("[CA] process_turn_f_stage: starting thread CA-FStage-%d", turn_index)
        thread.start()
        logger.info("[CA] process_turn_f_stage: thread CA-FStage-%d started", turn_index)
        return turn_index

    def _run_f_stage(self, session_id, turn_index, bg_review=False):
        start = time.monotonic()
        logger.info("[CA] _run_f_stage: START turn %d", turn_index)
        dialogue_ok = False

        # ── 从 DB 读一轮 Elm ──
        from .store import read_turn_elm_rows, read_prev_fct
        rows = read_turn_elm_rows(self.store, session_id, turn_index)
        if not rows:
            logger.warning("[CA] _run_f_stage turn %d: no Elm rows found, skipping", turn_index)
            return

        parts = []
        user_elm = ""
        for seq, role, content, tool_name, tool_call_id in rows:
            if role == "user":
                user_elm = content or ""
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            elif role == "tool":
                parts.append(f"Tool({tool_name}): {(content or '')[:200]}")
        elm_text = "\n".join(parts)
        prev_fct = read_prev_fct(self.store, session_id, turn_index)
        token_offset = sum(len(r[2] or "") for r in rows)

        if bg_review:
            logger.info("[CA] _run_f_stage turn %d: background review, skipping LLM", turn_index)

        try:
            if bg_review:
                # bg_review 路径：从 user Elm 提取实际内容摘要
                _src = user_elm or elm_text or ""
                if len(_src) > 80:
                    _brief = _src[:80].strip()
                else:
                    _brief = _src.strip()
                if not _brief:
                    _brief = "后台审查"
                cleaned = {
                    "changes": [{"stage_tag": "已实施", "core_change": _brief}],
                    "core_change": _brief,
                    "_assemble_status": 0
                }
                dialogue_ok = True
                logger.info("[CA] _run_f_stage turn %d: bg_review summary: %s",
                            turn_index, _brief)
            else:
                logger.info("[CA] _run_f_stage turn %d: calling LLM", turn_index)
                try:
                    response_text, finish_reason = self._call_llm_for_fct(prev_fct, elm_text)
                except FctTruncatedException as e:
                    response_text = e.response_text
                    finish_reason = "length"

                # 截断检测（在 _call_llm_for_fct 返回后进行，即使被 mock 也能覆盖）
                _has_stage_tag = "<stage_tag>" in response_text
                if finish_reason == "length" or (_has_stage_tag and not response_text.strip().endswith("</core_change>")):
                    logger.warning("[CA-METRIC] ca.fct.truncated_fallback: turn=%d, finish_reason=%s, len=%d",
                                   turn_index, finish_reason, len(response_text))
                    self.stats.fct_truncated_fallback += 1
                    # 截断降级：写入待补全记录
                    truncated_cleaned = {
                        "changes": [],
                        "core_change": "本轮无新内容",
                        "new_materials": [],
                        "objective_facts": [],
                        "consensus": [],
                        "todo": [],
                        "_assemble_status": 1,
                    }
                    fct_str = json.dumps(truncated_cleaned, ensure_ascii=False)
                    self._update_fct_v5(
                        session_id, turn_index, fct_str, "",
                    )
                    logger.info("[CA] _run_f_stage turn %d: truncated, saved as pending backfill", turn_index)
                    return

                fct_dict, parser_hdl, _ = parse_v1_markdown_xml(response_text)
                if not parser_hdl:
                    logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", turn_index)
                    self.stats.skipped_empty += 1
                # fct_dict 已由 parse_v1_markdown_xml 完成结构化解析，
                # 直接传给 clean_increment（跳过 ooda_parser.parse，
                # 后者只兼容旧格式 "标题：内容" 格式，不兼容 Markdown ### 标题）
                cleaned = clean_increment(fct_dict)
                cleaned["_assemble_status"] = 0
                dialogue_ok = True

            fct_str = json.dumps(cleaned, ensure_ascii=False)
            hdl_text = self._extract_l0(cleaned)
            try:
                fct_emb = self.embed_client.embed(fct_str)
                hdl_emb = self.embed_client.embed(hdl_text)
            except Exception:
                fct_emb = None
                hdl_emb = None

            # v5: 用 _update_fct_v5 只写 fct/hdl，不碰 content
            self._update_fct_v5(
                session_id, turn_index, fct_str, hdl_text,
            )
            self.cache.add_turn(turn_index, hdl_text, fct_str, hdl_emb, fct_emb)

            if dialogue_ok:
                self.stats.tool_pre_upgrade_count = 0

        except Exception as e:
            logger.error("F‑stage crash turn %d: %s", turn_index, e, exc_info=True)
            fallback = json.dumps({
                "changes": [],
                "core_change": user_elm or "本轮无新内容",
                "_assemble_status": 0,
                "new_materials": [], "objective_facts": [],
                "consensus": [], "todo": []
            }, ensure_ascii=False)
            hdl = (user_elm or "本轮无新内容")[:100]
            self._update_fct_v5(
                session_id, turn_index, fallback, hdl,
            )
            self.cache.add_turn(turn_index, hdl, fallback, None, None)
        finally:
            with self._task_lock:
                self._pending_tasks.pop(turn_index, None)
            elapsed = time.monotonic() - start
            logger.info("[CA] _run_f_stage: FINISH turn %d in %.1fs (dialogue_ok=%s)",
                       turn_index, elapsed, dialogue_ok)
            self._dialogue_backfill.trigger()
            self._tool_backfill.trigger()
            # ── OpenViking 话题提交 ──
            if Config.CA_OV_SUBMIT_ENABLED and self._pending_ov_submit is not None:
                try:
                    if self._fire_ov_submit(self._pending_ov_submit):
                        self._pending_ov_submit = None
                        logger.info("[CA] topic %d submitted to OpenViking", self._last_topic_id)
                except Exception as exc:
                    logger.warning("[CA] OV submit failed (will retry next turn): %s", exc)

    def _on_api_response_v5(self, *,
                             api_request_id: str,
                             assistant_message: Any,
                             api_call_count: int,
                             turn_index: int,
                             finish_reason: str = "tool_calls",
                             usage: Optional[Dict] = None) -> None:
        """v5: 写 thought 行 L2 + tool 占位行 + thought L1。不经过 buffer。"""
        # DeepSeek v4: thought 思考链在 provider_data["reasoning_content"]，不在 content
        pd = getattr(assistant_message, "provider_data", None) or {}
        thought = pd.get("reasoning_content", "") or getattr(assistant_message, "content", "") or ""
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        tool_defs = []
        for tc in tool_calls:
            tc_id = getattr(tc, "id", "") or ""
            tc_name = getattr(tc, "name", "") or ""
            tc_args = getattr(tc, "arguments", None)
            if isinstance(tc_args, str):
                try:
                    tc_args = json.loads(tc_args)
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}
            tool_defs.append({"id": tc_id, "type": getattr(tc, "type", "function"),
                              "function": {"name": tc_name, "arguments": tc_args}})

        # 纯对话（无工具且非 tool_calls）→ 不写 thought 行
        if not tool_defs and finish_reason != "tool_calls":
            return

        turn = turn_index
        seq = self._seq_counter.get(turn, 0) + 1
        self._seq_counter[turn] = seq

        # thought L1（轻量摘要，无需 LLM）
        thought_l1 = ""
        try:
            from .tool_summarizer import ToolSummarizer
            thought_l1 = ToolSummarizer.generate_group_summary(thought)
        except Exception:
            pass

        from .store import write_turn_v5
        write_turn_v5(
            self.store, self._session_id, turn, seq,
            role='assistant', content=thought,
            tool_calls_json=json.dumps(tool_defs, ensure_ascii=False),
            finish_reason=finish_reason,
            usage_prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            usage_completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            fct_text=thought_l1,
            written_at=time.time(),
        )
        logger.info("[CA_v5] _on_api_response: wrote thought turn=%d seq=%d tools=%d",
                    turn, seq, len(tool_defs))

        # tool 占位行
        for i, tc_def in enumerate(tool_defs, start=1):
            tool_seq = self._seq_counter[turn] + i
            tc_id = tc_def["id"]
            write_turn_v5(
                self.store, self._session_id, turn, tool_seq,
                role='tool', content='',
                tool_name=tc_def.get("function", {}).get("name", ""),
                tool_call_id=tc_id,
                status='pending',
                written_at=time.time(),
            )
            self._tool_seq_map[tc_id] = (turn, tool_seq)

        self._seq_counter[turn] += len(tool_defs)
        logger.info("[CA_v5] _on_api_response: wrote %d tool placeholders", len(tool_defs))

    def _on_pre_tool_call_v5(self, *,
                              tool_call_id: str,
                              tool_name: str,
                              args: Optional[Dict] = None,
                              api_request_id: str = "") -> None:
        """v5: 无操作（占位行已在 _on_api_response_v5 写入）。"""
        logger.debug("[CA_v5] _on_pre_tool_call: %s (%s) — placeholder already written", tool_name, tool_call_id)

    def _on_post_tool_call_v5(self, *,
                               tool_call_id: str,
                               tool_name: str,
                               args: Optional[Dict] = None,
                               result: Any = None,
                               status: str = "ok",
                               duration_ms: int = 0,
                               error_type: Optional[str] = None,
                               error_message: Optional[str] = None,
                               api_request_id: str = "") -> None:
        """v5: 回填 tool 行 L2 + 生成 per-tool L1。"""
        entry = self._tool_seq_map.get(tool_call_id)
        if entry:
            turn, seq = entry
        else:
            # 异常时序：无预占行，现场插
            turn = self._current_turn
            seq = self._seq_counter.get(turn, 0) + 1
            self._seq_counter[turn] = seq
            logger.warning("[CA_v5] _on_post_tool_call: no placeholder for %s, created at turn=%d seq=%d",
                          tool_call_id, turn, seq)

        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False) if result else ""

        # 生成 per-tool L1
        fct_str, hdl_text = "", ""
        try:
            tc_def = {"id": tool_call_id, "type": "function",
                      "function": {"name": tool_name, "arguments": args or {}}}
            result_entry = [{"content": content, "status": status}]
            fct_dict, hdl_text = self.tool_summarizer.summarize(tc_def, result_entry)
            fct_str = json.dumps(fct_dict, ensure_ascii=False) if fct_dict else ""
        except Exception as e:
            logger.warning("[CA_v5] tool summarize failed: %s", e)

        from .store import write_turn_v5
        write_turn_v5(
            self.store, self._session_id, turn, seq,
            role='tool', content=content,
            tool_name=tool_name, tool_call_id=tool_call_id,
            args_json=json.dumps(args) if args else None,
            status=status, duration_ms=duration_ms,
            fct_text=fct_str, hdl_text=hdl_text,
            written_at=time.time(),
        )
        logger.debug("[CA_v5] _on_post_tool_call: wrote %s turn=%d seq=%d status=%s",
                     tool_name, turn, seq, status)

    def _update_fct_v5(self, session_id: str, turn_index: int,
                       fct_text: str, hdl_text: str) -> bool:
        """v5: 只 UPDATE turn_stream 的 l1/l0 列（seq=0），不碰 content。"""
        from .store import update_seq0_fct_v5
        return update_seq0_fct_v5(self.store, session_id, turn_index, fct_text, hdl_text)


    # ---------- A‑stage ----------
    def _extend_elm_messages(messages: List[Dict], elm_text: str, turn_index: int) -> None:
        """将 Elm (JSON 消息数组) 展开到 messages，标记 _turn_index。"""
        try:
            msgs = json.loads(elm_text)
            if isinstance(msgs, list):
                for m in msgs:
                    m["_turn_index"] = turn_index
                messages.extend(msgs)
            else:
                messages.append({"role": "user", "content": elm_text, "_turn_index": turn_index})
        except (json.JSONDecodeError, ValueError):
            messages.append({"role": "user", "content": elm_text, "_turn_index": turn_index})

    def set_system_overhead(self, overhead: int):
        """(已弃用) 动态测量不可行——Hermes 不暴露 tool schemas 等非消息开销。
        测量代码已于 2026-06-14 移除。保留方法作为公开 API 以防外部调用。"""
        if overhead > 0:
            self._system_overhead = overhead

    def _grade_topics_by_radius(self, turn_to_topic: Dict[int, int],
                                 topic_data: Dict,
                                 fct_embeddings: Dict[int, List[float]],
                                 q_emb: Optional[List[float]],
                                 retrieved_topics: set) -> Dict[int, str]:
        """按半径 r 对话题三级定级。
        
        Returns:
            topic_grades: Dict[topic_id → "L0"|"L1"|"L2"]
        """
        topic_grades: Dict[int, str] = {}

        if q_emb is None:
            # 无 query embedding 时：所有非 BG 话题 L1，BG L0
            for tid, td in topic_data.items():
                topic_grades[tid] = Config.TOPIC_BG_LEVEL if td["is_bg"] else "L1"
            return topic_grades

        for topic_id, td in topic_data.items():
            if td["is_bg"]:
                topic_grades[topic_id] = Config.TOPIC_BG_LEVEL
                continue
            if td["centroid"] is None:
                topic_grades[topic_id] = "L1"
                continue

            # 计算 query 到 topic 形心的距离（余弦距离）
            sim = cosine_similarity(q_emb, td["centroid"])
            d = 1.0 - sim

            # 半径 r = min(max_intra, nearest_centroid / TOPIC_RADIUS_WEIGHT)
            r = td["max_intra"]
            if td["nearest_centroid_dist"] > 0:
                r = min(r, td["nearest_centroid_dist"] / Config.TOPIC_RADIUS_WEIGHT) if r > 0 else td["nearest_centroid_dist"] / Config.TOPIC_RADIUS_WEIGHT
            if r <= 0:
                r = 0.05  # 最小半径保护

            # 定级
            if d <= r / 2.0:
                topic_grades[topic_id] = "L2"
            elif d <= r:
                topic_grades[topic_id] = "L1"
            elif topic_id in retrieved_topics:
                topic_grades[topic_id] = "L1"
            else:
                topic_grades[topic_id] = "L0"

            if Config.DEBUG_MODE:
                logger.debug("topic %d: d=%.4f r=%.4f → %s (retrieved=%s)",
                             topic_id, d, r, topic_grades[topic_id], topic_id in retrieved_topics)

        return topic_grades

    # ── 话题级 Plan 计算（v4.6.0）──

    def _truncate_to_l0(text: str, max_chars: int = 100) -> str:
        """将系统消息截断为 L0 风格（v4.7 格式）：优先在句号处断句，降级到逗号，最后硬截断。"""
        if not text:
            return ""
        return _safe_truncate(text, max_chars)

    def _extend_with_l2(result: List[Dict], elm_text: str, turn_index: int) -> None:
        """将 Elm (JSON 消息数组) 展开到 result，标记 _turn_index。"""
        try:
            msgs = json.loads(elm_text)
            if isinstance(msgs, list):
                for m in msgs:
                    m["_turn_index"] = turn_index
                result.extend(msgs)
            else:
                result.append({"role": "user", "content": elm_text, "_turn_index": turn_index})
        except (json.JSONDecodeError, TypeError):
            result.append({"role": "user", "content": elm_text, "_turn_index": turn_index})

    # ---------- 辅助方法 ----------
    def _token_estimate(self, text: str) -> int:
        """粗略 token 估算。CJK 文本按 1.5× 字符数，其他按 0.5×。
        与 Hermes 的 _CHARS_PER_TOKEN=4 对齐（但 CA 偏保守以保护对话轮）。
        """
        if not text:
            return 0
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        if cjk > len(text) * 0.5:
            return int(len(text) * 1.5)
        return max(1, len(text) // 2)

    def _is_valid_fct(self, fct_text: str) -> bool:
        if not fct_text or not fct_text.strip():
            return False
        try:
            data = json.loads(fct_text)
            changes = data.get("changes", [])
            if changes:
                return True
            core = data.get("core_change", "")
            return bool(core and core != "本轮无新内容")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False

    _FCT_DEBUG_PATTERNS = (
        "当前会话", "CA 注入", "CA插件", "CA 插件", "当前 CA",
        "此会话", "本对话", "此对话",
        "当前上下文", "当前注入", "ctx 中",
        "完全无工具", "无工具组",
    )

    def _format_fct_for_display(self, fct_text: str) -> str:
        """将对话轮 L1 JSON 摘要格式化为可读文本，替代原始 JSON 注入。

        当 LLM 生成非 JSON 调试描述时（如"当前会话 CA 注入 ctx 中完全无工具组..."），
        识别并返回空字符串，不泄漏原始文本。
        """
        if not fct_text or not fct_text.strip():
            return fct_text
        try:
            data = json.loads(fct_text)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON → 检测是否为调试描述（LLM 错误输出）
            for pat in self._FCT_DEBUG_PATTERNS:
                if pat in fct_text:
                    return ""
            return fct_text
        core = data.get("core_change", "")
        if not core:
            # JSON 格式但无有效核心内容 → 检测调试模式
            for pat in self._FCT_DEBUG_PATTERNS:
                if pat in fct_text:
                    return ""
            return fct_text
        changes = data.get("changes", [])
        if changes:
            lines = [f"【{c['stage_tag']}】{c['core_change']}" for c in changes]
        else:
            lines = [core]
        for key in ("new_materials", "objective_facts"):
            items = data.get(key, [])
            if items:
                # 过滤占位符（LLM 写 "无"/"-"/"- 无" 等表示空）
                real_items = [str(i)[:240] for i in items
                              if str(i).strip() not in ("", "无", "-", "- 无", "无有效内容")]
                if real_items:
                    joined = " | ".join(real_items)
                    lines.append(f"  {joined}")
        return "\n".join(lines)

    def _call_llm_for_fct(self, prev_fct, elm_text) -> Tuple[str, str]:
        """返回 (response_text, finish_reason)。

        所有重试均失败时返回 ("", "error")。
        """
        import urllib.request
        llm_start = time.monotonic()
        prompt = FCT_GENERATION_PROMPT.format(
            previous_summary=format_previous_summary_for_prompt(
                json.dumps(prev_fct, ensure_ascii=False) if prev_fct else None
            ),
            current_dialog=elm_text)
        req_body = {
            "model": Config.LLM_MODEL,
            "prompt": prompt, "stream": False,
            "options": {
                "num_predict": Config.L1_MAX_TOKENS,
                "temperature": Config.L1_TEMPERATURE,
            },
            "keep_alive": -1
        }
        if Config.LLM_THINK is not None:
            req_body["think"] = Config.LLM_THINK
        payload = json.dumps(req_body).encode()
        response_text = ""
        finish_reason = "error"
        for attempt in range(Config.LLM_MAX_RETRIES):
            try:
                req = urllib.request.Request(f"{Config.LLM_ENDPOINT}/api/generate", data=payload,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
                response_text = data.get("response", "")
                finish_reason = data.get("done_reason") or data.get("finish_reason", "stop")
                break
            except Exception as e:
                logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
                time.sleep(2 ** attempt)

        elapsed_ms = int((time.monotonic() - llm_start) * 1000)
        logger.warning("[CA-METRIC] ca.fct.latency_ms: turn=%d, ms=%d", self._turn_counter, elapsed_ms)
        self.stats.fct_latency_ms += elapsed_ms

        # 所有重试均失败，返回退化输出
        if finish_reason == "error":
            self.stats.fct_truncated_fallback += 1
            return ("", "error")

        return (response_text, finish_reason)

    def _extract_l0(self, fct_dict):
        changes = fct_dict.get("changes", [])
        if changes:
            core = "；".join(c["core_change"] for c in changes)
        else:
            core = fct_dict.get("core_change", "")
            if core:
                # 旧格式兼容：去掉【计划】/【探讨】开头的续写段落
                _first_low = len(core)
                for _tag in ("【计划】", "【探讨】"):
                    _idx = core.find(_tag)
                    if 0 < _idx < _first_low:
                        _first_low = _idx
                if _first_low < len(core):
                    core = core[:_first_low].rstrip()
        if not core or core in ("无", "本轮无新内容"):
            logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", self._turn_counter)
            return "无"
        result = _safe_truncate(core, 100)
        return result or "无"

    def _estimate_token_offset(self, history):
        return sum(self._token_estimate(m.get("content", "")) for m in history) if history else 0

    def _get_previous_fct(self) -> Optional[Dict]:
        """从 turn_stream 查前一轮 Fct。"""
        from .store import read_prev_fct
        raw = read_prev_fct(self.store, self._session_id, self._turn_counter)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def reset(self):
        self.wait_for_pending(5.0)
        self.cache.destroy()
        builder = CacheBuilder(self.store)
        self.cache = builder.build(self._session_id)
        with self._task_lock:
            self._pending_tasks.clear()
        self.stats = AssembleStats()
        self._turn_counter = self._restore_turn_index()

    def wait_for_pending(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        with self._task_lock:
            tasks = list(self._pending_tasks.values())
        for t in tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=max(0.1, remaining))

    # ── Token 水位查询（v4.6.0）──

    def debug_token_budget(self, session_id: str = "") -> dict:
        """查询当前会话的 Token 水位。纯只读，不修改任何状态。

        Returns:
            dict: {
                "context_length": int,    # 总窗口上限
                "budget_max": int,        # 可用预算上限（95%）
                "used_tokens": int,       # 当前累计 token 偏移
                "remaining": int,         # 剩余预算
                "usage_pct": float,       # 使用率百分比
            }
        """
        if not session_id:
            session_id = self._session_id
        max_offset = self.store.get_max_token_offset(session_id) or 0
        budget_max = int(Config.CONTEXT_LENGTH * 0.95)
        return {
            "context_length": Config.CONTEXT_LENGTH,
            "budget_max": budget_max,
            "used_tokens": max_offset,
            "remaining": max(0, budget_max - max_offset),
            "usage_pct": round(max_offset / Config.CONTEXT_LENGTH * 100, 1),
        }

    def _fire_ov_submit(self, ts: _TopicSwitchData) -> bool:
        """将已完成话题的 L1 摘要提交到 OpenViking。

        POST {OPENVIKING_ENDPOINT}/api/v1/content/write
        URI: viking://user/{user}/memories/ca_topics/topic_{id}_{ts}.md

        Returns:
            True  — HTTP 200（成功）
            False — 任何失败（网络、非 200、异常）
        """
        import urllib.request
        import time as _time

        endpoint = os.getenv("OPENVIKING_ENDPOINT", "http://localhost:1933")
        ov_user = os.getenv("OPENVIKING_USER", "tester")
        uri = f"viking://user/{ov_user}/memories/ca_topics/topic_{ts.topic_id}_{int(_time.time())}.md"

        # 组装 Markdown
        lines = [f"# Topic {ts.topic_id} — CA Conversation Summary"]
        if ts.turn_indices:
            lines.append(f"\n> Turns: {ts.turn_indices[0]}-{ts.turn_indices[-1]}")
        lines.append(f"\n## Overview\n{ts.agg_text}")

        for ti in sorted(ts.turn_indices):
            l1 = ts.fct_texts.get(ti, "")
            core = l1
            if l1:
                try:
                    l1j = json.loads(l1)
                    core = l1j.get("core_change", l1)
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append(f"\n## Dialogue Turn {ti}\n{core}")

        if ts.tool_group_l1:
            lines.append("\n## Tool Groups")
            for key in sorted(ts.tool_group_l1.keys()):
                lines.append(f"\n### {key}\n{ts.tool_group_l1[key]}")

        content = "\n".join(lines)

        payload = json.dumps({
            "uri": uri,
            "content": content,
            "mode": "create",
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url=f"{endpoint}/api/v1/content/write",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("[CA] OV submit success: topic=%d uri=%s", ts.topic_id, uri)
                    return True
                body = resp.read().decode("utf-8", errors="replace")[:200]
                logger.warning("[CA] OV submit returned %d: %s", resp.status, body)
                return False
        except Exception as exc:
            logger.warning("[CA] OV submit exception for topic %d: %s", ts.topic_id, exc)
            return False
