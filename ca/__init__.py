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

import hashlib
import json
import re
import logging
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import Config
from .store import SQLiteStore
from .cache import AssemblyCache, CacheBuilder, BM25Snapshot, tokenise
from .retrieval import Retriever, cosine_similarity
from .embedding import EmbeddingClient
from .ooda_parser import OODAParser
from .post_process import robust_json_parse, clean_increment, parse_v1_markdown_xml, _safe_truncate, ItemState
from .prompts import L1_GENERATION_PROMPT
from .store import format_previous_summary_for_prompt
from .stats import AssembleStats


class L1TruncatedException(Exception):
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
class ToolGroupBuffer:
    """一个 API 调用对应的工具组缓冲区。"""
    thought: str
    tool_defs: List[Dict]                    # ToolCall 定义列表
    api_call_count: int
    turn_index: int
    finish_reason: str = "tool_calls"        # tool_calls / length
    results: Dict[str, Dict] = None          # tool_call_id → 执行结果
    usage: Optional[Dict] = None

    def __post_init__(self):
        if self.results is None:
            self.results = {}


from .lstage import BackfillThread

logger = logging.getLogger(__name__)


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
class TurnPlanEntry:
    """A-stage 单轮拣选决策记录。

    供 assemble() 写入 turn_plan 表，实现拣选过程可观测、可回放。
    后期可通过 topic_group 字段将相邻轮次归并为话题。
    """
    turn_index: int
    turn_type: str = "dialogue"
    tool_sub_index: int = 0

    target_level: str = "L0"           # L2 | L1 | L0
    decision_reason: str = "middle"    # head | tail | retrieved | pre_upgraded | tool_head | middle

    l2_tokens: int = 0
    summary_tokens: int = 0
    tokens_saved: int = 0

    rrf_score: Optional[float] = None
    upgrade_rank: Optional[int] = None

    budget_remaining: Optional[int] = None
    topic_group: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "turn_index": self.turn_index,
            "turn_type": self.turn_type,
            "tool_sub_index": self.tool_sub_index,
            "target_level": self.target_level,
            "decision_reason": self.decision_reason,
            "l2_tokens": self.l2_tokens,
            "summary_tokens": self.summary_tokens,
            "tokens_saved": self.tokens_saved,
            "rrf_score": self.rrf_score,
            "upgrade_rank": self.upgrade_rank,
            "budget_remaining": self.budget_remaining,
            "topic_group": self.topic_group,
        }


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
        self._tool_buffer: Dict[str, ToolGroupBuffer] = {}   # api_request_id → buffer
        self._api_sequence: int = 0                          # fallback 自增计数器
        self.stats = AssembleStats()

        self._dialogue_backfill = BackfillThread(self, 'dialogue', Config.BACKFILL_DIALOGUE_RATE)
        self._tool_backfill = BackfillThread(self, 'tool', Config.BACKFILL_TOOL_RATE)
        self._dialogue_backfill.start()
        self._tool_backfill.start()

        # 话题边界追踪：从 turn_plan 恢复上次 topic_id


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
        # 0. Flush 悬挂 buffer
        if self._tool_buffer:
            n = len(self._tool_buffer)
            logger.warning("[CA] destroy: flushing %d hanging tool groups", n)
            self.flush_tool_buffer()
        # 1. 先等待所有 C‑stage 任务完成
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

    # ---------- C‑stage ----------
    def process_turn_async(self, user_message: str, assistant_response: str,
                           conversation_history: Optional[List[Dict]] = None) -> int:
        expected = len([m for m in (conversation_history or []) if m.get("role") == "user"])
        with self._task_lock:
            target = max(self._turn_counter + 1, expected)
            if target in self._pending_tasks and self._pending_tasks[target].is_alive():
                logger.info("[CA] process_turn_async: turn %d already pending, returning", target)
                return target
            self._turn_counter = target
            turn_index = target

        logger.info("[CA] process_turn_async: starting C-stage for turn %d (expected=%d)",
                    turn_index, expected)

        l2_text = f"User: {user_message}\nAssistant: {assistant_response}"
        prev_l1 = self._get_previous_l1()
        token_offset = self._estimate_token_offset(conversation_history)

        # 在主线程捕获 write_origin（ContextVar 不自动传播到 daemon 线程）
        _bg_review = (get_current_write_origin() == "background_review")
        if _bg_review:
            logger.info("[CA] process_turn_async: background review detected for turn %d", turn_index)

        thread = threading.Thread(
            target=self._run_c_stage,
            args=(self._session_id, turn_index, prev_l1, l2_text, token_offset,
                  user_message, assistant_response, _bg_review),
            daemon=True, name=f"CA-CStage-{turn_index}"
        )
        with self._task_lock:
            self._pending_tasks[turn_index] = thread
        logger.info("[CA] process_turn_async: starting thread CA-CStage-%d", turn_index)
        thread.start()
        logger.info("[CA] process_turn_async: thread CA-CStage-%d started", turn_index)
        return turn_index

    def _run_c_stage(self, session_id, turn_index, prev_l1, l2_text, token_offset,
                     user_message="", assistant_response="",
                     bg_review=False):
        start = time.monotonic()
        logger.info("[CA] _run_c_stage: START turn %d", turn_index)
        dialogue_ok = False

        if bg_review:
            logger.info("[CA] _run_c_stage turn %d: background review, skipping LLM", turn_index)

        try:
            if bg_review:
                cleaned = {
                    "core_change": "系统后台审查",
                    "_assemble_status": 0
                }
                dialogue_ok = True
            else:
                logger.info("[CA] _run_c_stage turn %d: calling LLM", turn_index)
                try:
                    response_text, finish_reason = self._call_llm_for_l1(prev_l1, l2_text)
                except L1TruncatedException as e:
                    response_text = e.response_text
                    finish_reason = "length"

                # 截断检测（在 _call_llm_for_l1 返回后进行，即使被 mock 也能覆盖）
                if finish_reason == "length" or not response_text.strip().endswith("</core_change>"):
                    logger.warning("[CA-METRIC] ca.l1.truncated_fallback: turn=%d, finish_reason=%s, len=%d",
                                   turn_index, finish_reason, len(response_text))
                    self.stats.truncated_fallback += 1
                    # 截断降级：写入待补全记录
                    truncated_cleaned = {
                        "core_change": "本轮无新内容",
                        "new_materials": [],
                        "objective_facts": [],
                        "consensus": [],
                        "todo": [],
                        "_assemble_status": 1,
                    }
                    l1_str = json.dumps(truncated_cleaned, ensure_ascii=False)
                    self.store.write_turn(
                        session_id, turn_index,
                        l0_text="", l1_text=l1_str,
                        l0_embedding=None, l1_embedding=None,
                        token_offset=token_offset,
                        turn_type='dialogue', tool_sub_index=0,
                        l2_text=json.dumps([
                            {"role": "user", "content": user_message or ""},
                            {"role": "assistant", "content": assistant_response or ""},
                        ], ensure_ascii=False),
                        _assemble_status=1,
                    )
                    logger.info("[CA] _run_c_stage turn %d: truncated, saved as pending backfill", turn_index)
                    return

                l1_dict, l0_text, core_state = parse_v1_markdown_xml(response_text)
                if not l0_text:
                    logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", turn_index)
                    self.stats.skipped_empty += 1
                # l1_dict 已由 parse_v1_markdown_xml 完成结构化解析，
                # 直接传给 clean_increment（跳过 ooda_parser.parse，
                # 后者只兼容旧格式 "标题：内容" 格式，不兼容 Markdown ### 标题）
                cleaned = clean_increment(l1_dict)
                # 状态注入：将 core_state 嵌入 l1_dict 内部字段
                if core_state and core_state != ItemState.UNKNOWN:
                    cleaned["_state"] = core_state.value
                cleaned["_assemble_status"] = 0
                dialogue_ok = True

            l1_str = json.dumps(cleaned, ensure_ascii=False)
            l0_text = self._extract_l0(cleaned)
            try:
                l1_emb = self.embed_client.embed(l1_str)
                l0_emb = self.embed_client.embed(l0_text)
            except Exception:
                l1_emb = None
                l0_emb = None

            # 对话轮 l2_text：只存当前轮 [user, assistant]，不存全量历史
            l2_storage = json.dumps([
                {"role": "user", "content": user_message or ""},
                {"role": "assistant", "content": assistant_response or ""},
            ], ensure_ascii=False)

            self.store.write_turn(
                session_id, turn_index,
                l0_text=l0_text, l1_text=l1_str,
                l0_embedding=l0_emb, l1_embedding=l1_emb,
                token_offset=token_offset,
                turn_type='dialogue', tool_sub_index=0,
                l2_text=l2_storage,
                _assemble_status=cleaned["_assemble_status"]
            )
            self.cache.add_turn(turn_index, l0_text, l1_str, l0_emb, l1_emb)

            if dialogue_ok:
                self.stats.tool_pre_upgrade_count = 0

        except Exception as e:
            logger.error("C‑stage crash turn %d: %s", turn_index, e, exc_info=True)
            fallback = json.dumps({
                "core_change": "本轮无新内容",
                "_assemble_status": 1,
                "new_materials": [], "objective_facts": [],
                "consensus": [], "todo": []
            }, ensure_ascii=False)
            self.store.write_turn(
                session_id, turn_index,
                l0_text="本轮无新内容",
                l1_text=fallback,
                l0_embedding=None,
                l1_embedding=None,
                token_offset=token_offset,
                turn_type='dialogue', tool_sub_index=0,
                l2_text=json.dumps([
                    {"role": "user", "content": user_message or ""},
                    {"role": "assistant", "content": assistant_response or ""},
                ], ensure_ascii=False),
                _assemble_status=1,
            )
            self.cache.add_turn(turn_index, "本轮无新内容", fallback, None, None)
        finally:
            with self._task_lock:
                self._pending_tasks.pop(turn_index, None)
            elapsed = time.monotonic() - start
            logger.info("[CA] _run_c_stage: FINISH turn %d in %.1fs (dialogue_ok=%s)",
                       turn_index, elapsed, dialogue_ok)
            self._dialogue_backfill.trigger()
            self._tool_backfill.trigger()

    def _extract_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        tool_turns = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                tool_responses_all = []
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    tool_responses_all.append(messages[j])
                    j += 1
                for call in msg["tool_calls"]:
                    call_id = call.get("id")
                    if call_id is None:
                        continue
                    matched = [r for r in tool_responses_all if r.get("tool_call_id") == call_id]
                    if len(matched) == 0:
                        logger.warning("No tool response for call_id=%s, generating placeholder", call_id)
                        matched = [{"role": "tool", "tool_call_id": call_id, "content": '{"error": "等待响应"}'}]
                    elif len(matched) > 1:
                        logger.warning("Multiple responses for call_id=%s, using first", call_id)
                        matched = matched[:1]
                    tool_turns.append({
                        "tool_call": call,
                        "tool_responses": matched,
                        "l2_messages": [msg] + matched,
                        "start_index": i
                    })
                i = j
            else:
                i += 1
        return tool_turns


    # ---------- Tool Buffer ----------

    def _on_api_response(self, *,
                         api_request_id: str,
                         assistant_message: Any,
                         api_call_count: int,
                         turn_index: int,
                         finish_reason: str = "tool_calls",
                         usage: Optional[Dict] = None) -> None:
        """post_api_request hook handler — 将 assistant 响应写入 buffer。

        从 assistant_message 提取 thought + tool_defs，
        创建 ToolGroupBuffer 存入 _tool_buffer[api_request_id]。
        """
        thought = getattr(assistant_message, "content", "") or ""
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
            tool_defs.append({
                "id": tc_id,
                "type": getattr(tc, "type", "function"),
                "function": {"name": tc_name, "arguments": tc_args},
            })

        # 截断保护：tool_defs 为空且 finish_reason 不是 tool_calls → 纯对话，不写入 buffer
        if not tool_defs and finish_reason != "tool_calls":
            return

        self._tool_buffer[api_request_id] = ToolGroupBuffer(
            thought=thought,
            tool_defs=tool_defs,
            api_call_count=api_call_count,
            turn_index=turn_index,
            finish_reason=finish_reason,
            usage=usage,
        )
        logger.info("[CA] _on_api_response: buffered api_call_count=%d turn=%d api_request_id=%s tools=%d",
                    api_call_count, turn_index, api_request_id, len(tool_defs))

    def _on_pre_tool_call(self, *,
                          tool_call_id: str,
                          tool_name: str,
                          args: Optional[Dict] = None,
                          api_request_id: str = "") -> None:
        """pre_tool_call hook handler — 在 buffer 中预注册工具占位。"""
        buf = self._tool_buffer.get(api_request_id)
        if buf is None:
            # 容错：如果 buffer 不存在（异常时序），auto-create sentinel 条目
            logger.warning("[CA] _on_pre_tool_call: no buffer for api_request_id=%s, auto-creating sentinel (api_call_count=999999)",
                          api_request_id)
            self._tool_buffer[api_request_id] = ToolGroupBuffer(
                thought="",
                tool_defs=[],
                api_call_count=999999,
                turn_index=self._turn_counter,
            )
            buf = self._tool_buffer[api_request_id]

        if tool_call_id not in buf.results:
            buf.results[tool_call_id] = {
                "tool_name": tool_name,
                "args": args or {},
                "status": "pending",
            }
        logger.debug("[CA] _on_pre_tool_call: registered %s (%s) in api_request_id=%s",
                     tool_name, tool_call_id, api_request_id)

    def _on_post_tool_call(self, *,
                           tool_call_id: str,
                           tool_name: str,
                           args: Optional[Dict] = None,
                           result: Any = None,
                           status: str = "ok",
                           duration_ms: int = 0,
                           error_type: Optional[str] = None,
                           error_message: Optional[str] = None,
                           api_request_id: str = "") -> None:
        """post_tool_call hook handler — 将工具执行结果写入 buffer。"""
        buf = self._tool_buffer.get(api_request_id)
        if buf is None:
            # 容错：auto-create sentinel
            logger.warning("[CA] _on_post_tool_call: no buffer for api_request_id=%s, auto-creating sentinel (api_call_count=999999)",
                          api_request_id)
            self._tool_buffer[api_request_id] = ToolGroupBuffer(
                thought="",
                tool_defs=[],
                api_call_count=999999,
                turn_index=self._turn_counter,
            )
            buf = self._tool_buffer[api_request_id]

        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False) if result else ""

        buf.results[tool_call_id] = {
            "tool_name": tool_name,
            "args": args or {},
            "content": content,
            "status": status,
            "duration_ms": duration_ms,
            "error_type": error_type,
            "error_message": error_message,
        }
        logger.debug("[CA] _on_post_tool_call: filled %s (%s) status=%s dur=%dms",
                     tool_name, tool_call_id, status, duration_ms)

    def flush_tool_buffer(self, user_message: str = "") -> int:
        """将 _tool_buffer 全部数据写入 store，并清空 buffer。

        流程：
        ① 按 buffer 创建顺序（api_call_count）排序
        ② user 行 (api=0, seq=0)
        ③ 每组 assistant{tc} (api=N, seq=0) + tool×M (api=N, seq=1..M)
           — 调用 generate_group_summary 写入 assistant{tc}.l1_text
           — 调用 summarize 生成 per-tool L1 (pop thought_process)
        ④ 清空 buffer

        Returns:
            写入的行数（不含 future final 行）
        """
        if not self._tool_buffer:
            logger.debug("[CA] flush_tool_buffer: buffer empty, no rows to write")
            return 0

        # 按 api_call_count 排序
        sorted_items = sorted(
            self._tool_buffer.items(),
            key=lambda kv: kv[1].api_call_count,
        )

        total_rows = 0
        for api_request_id, buf in sorted_items:
            turn_idx = buf.turn_index

            # ② user 行 (仅第一组写入，避免重复)
            if buf.api_call_count == 1 and user_message:
                self.store.write_turn(
                    self._session_id, turn_idx,
                    l0_text="", l1_text="",
                    l0_embedding=None, l1_embedding=None,
                    token_offset=0,
                    api_call_count=0, seq_index=0, role='user',
                    content=user_message,
                    _assemble_status=0,
                )
                total_rows += 1

            # 收集工具结果用于 generate_group_summary
            tool_results_for_summary = []
            for tc_def in buf.tool_defs:
                tc_id = tc_def.get("id", "")
                result_data = buf.results.get(tc_id, {})
                tool_results_for_summary.append({
                    "tool_name": tc_def.get("function", {}).get("name", ""),
                    "status": result_data.get("status", "ok"),
                    "result_summary": result_data.get("content", "")[:80],
                })

            group_summary = ToolSummarizer.generate_group_summary(
                buf.thought, tool_results_for_summary
            )

            # ③ assistant{tc} 行 (api=N, seq=0)
            tool_calls_json = json.dumps(buf.tool_defs, ensure_ascii=False)
            self.store.write_turn(
                self._session_id, turn_idx,
                l0_text=group_summary.get("group_result", "工具组"),
                l1_text=json.dumps(group_summary, ensure_ascii=False),
                l0_embedding=None, l1_embedding=None,
                token_offset=0,
                api_call_count=buf.api_call_count, seq_index=0,
                role='assistant', content=buf.thought,
                tool_calls_json=tool_calls_json,
                finish_reason=buf.finish_reason,
                _assemble_status=0,
            )
            total_rows += 1

            # ③ tool × M 行 (api=N, seq=1..M)
            for seq_idx, tc_def in enumerate(buf.tool_defs, start=1):
                tc_id = tc_def.get("id", "")
                result_data = buf.results.get(tc_id, {})
                tool_content = result_data.get("content", "")
                tool_status = result_data.get("status", "ok")

                per_tool_l1 = {
                    "tool_name": tc_def.get("function", {}).get("name", ""),
                    "result_summary": tool_content[:80] if tool_content else "无返回数据",
                    "status": tool_status,
                    "_assemble_status": 0,
                }
                self.store.write_turn(
                    self._session_id, turn_idx,
                    l0_text=per_tool_l1["tool_name"] + ": " + (tool_content[:60] if tool_content else "无返回"),
                    l1_text=json.dumps(per_tool_l1, ensure_ascii=False),
                    l0_embedding=None, l1_embedding=None,
                    token_offset=0,
                    api_call_count=buf.api_call_count, seq_index=seq_idx,
                    role='tool', content=tool_content,
                    tool_call_id=tc_id,
                    tool_name=tc_def.get("function", {}).get("name", ""),
                    status=tool_status,
                    _assemble_status=0,
                )
                total_rows += 1

        # ④ 清空 buffer
        self._tool_buffer.clear()
        logger.info("[CA] flush_tool_buffer: flushed %d rows for %d api groups",
                    total_rows, len(sorted_items))
        return total_rows


    # ---------- A‑stage ----------
    def _rebuild_messages_from_cache(self) -> List[Dict]:
        """从 l2_text 重建消息列表。

        每条 record 的 l2_text 是该轮独立的消息 JSON 数组：
        - dialogue 轮: [{"role": "user", ...}, {"role": "assistant", ...}]
        - tool 轮:     [{"role": "assistant", "tool_calls": [...]}, {"role": "tool", ...}]

        按 turn_index + turn_type 顺序拼接。

        """
        messages: List[Dict] = []
        for rec in self.store.read_session(self._session_id):
            l2 = rec.get("l2_text")
            if not l2:
                continue
            turn_index = rec.get("turn_index", 0)
            try:
                msgs = json.loads(l2)
                if isinstance(msgs, list):
                    for m in msgs:
                        m["_turn_index"] = turn_index
                    messages.extend(msgs)
                else:
                    raise ValueError("l2_text is not a list")
            except (json.JSONDecodeError, ValueError):
                messages.append({"role": "user", "content": l2, "_turn_index": turn_index})
        return messages

    def _extract_tool_group(self, messages, start):
        group = [messages[start]]
        i = start + 1
        while i < len(messages) and messages[i].get("role") == "tool":
            group.append(messages[i])
            i += 1
        return group, i

    def _build_tool_key_map(self, messages: List[Dict], idx_to_turn: Dict[int, int]) -> Dict[int, List[Tuple[Tuple[int, int], int]]]:
        tool_map: Dict[int, List[Tuple[Tuple[int, int], int]]] = {}
        current_tool_index: Dict[int, int] = {}
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                group, next_i = self._extract_tool_group(messages, i)
                start_index = i
                entries = []
                for call in msg["tool_calls"]:
                    turn = idx_to_turn.get(start_index, start_index)
                    current_tool_index[turn] = current_tool_index.get(turn, 0) + 1
                    sub_idx = current_tool_index[turn]
                    key = (turn, sub_idx)
                    token_count = sum(self._token_estimate(m.get("content", "")) for m in group)
                    entries.append((key, token_count))
                tool_map[start_index] = entries
                i = next_i
            else:
                i += 1
        return tool_map

    def assemble(self, user_message: str, context_length: int = None) -> List[Dict]:
        if context_length is None:
            context_length = self.context_length

        messages = self._rebuild_messages_from_cache()
        messages.append({"role": "user", "content": user_message})

        stats = self.stats
        stats.__init__()
        tokens_before = sum(self._token_estimate(m.get("content", "")) for m in messages)

        try:
            self.cache.ensure_snapshot()
        except Exception:
            logger.warning("Snapshot build failed, retrying once")
            try:
                self.cache.rebuild_bm25_snapshot()
            except Exception as e:
                logger.error("Snapshot rebuild failed, falling back: %s", e)
                if tokens_before > context_length * 0.95:
                    truncated = self._hard_truncation(messages, context_length)
                    stats.finalize(tokens_before, sum(self._token_estimate(m.get("content", "")) for m in truncated))
                    return truncated
                stats.finalize(tokens_before, tokens_before)
                return messages

        snapshot = self.cache.get_bm25_snapshot()
        if not snapshot:
            if tokens_before > context_length * 0.95:
                truncated = self._hard_truncation(messages, context_length)
                stats.finalize(tokens_before, sum(self._token_estimate(m.get("content", "")) for m in truncated))
                return truncated
            stats.finalize(tokens_before, tokens_before)
            return messages

        with stats.time_phase("get_snapshot"):
            l1_texts, l0_texts = self.cache.get_snapshot_data()
            tool_l1_texts, tool_l0_texts = self.cache.get_tool_snapshot_data()

        idx_to_turn = {i: msg.get("_turn_index", i) for i, msg in enumerate(messages)}
        tool_key_map = self._build_tool_key_map(messages, idx_to_turn)

        # 先用 cache 中的 l1_embeddings（含对话轮 embedding）
        l1_embeddings = snapshot.l1_embeddings

        with stats.time_phase("layers"):
            tail_start = self._compute_tail_start(messages)
            # 工具尾区保护
            all_turns = sorted(l1_texts.keys())
            tool_tail_turns: Set[int] = set()
            if Config.TOOL_TAIL_TURN_COUNT > 0 and all_turns:
                tool_tail_turns = set(all_turns[-Config.TOOL_TAIL_TURN_COUNT:])

        with stats.time_phase("embed_query"):
            try:
                q_emb = self.embed_client.embed(user_message)
            except Exception:
                q_emb = None

        # 写入 query_embedding 到当前对话轮（用于后续回放分析）
        current_turn = max(l1_texts.keys()) if l1_texts else 0
        if q_emb is not None and current_turn > 0:
            try:
                self.store.write_query_embedding(self._session_id, current_turn, q_emb)
            except Exception:
                pass

        # ── 话题分割（R1 + R2）──
        with stats.time_phase("topic_seg"):
            turn_to_topic, topic_data = self._compute_topic_groups(l1_texts, l1_embeddings)

        # ── 话题检索 + 三级定级 ──
        with stats.time_phase("topic_retrieval"):
            from .retrieval import TopicRetriever
            topic_retriever = TopicRetriever(snapshot, turn_to_topic, topic_data)
            retrieved_topics = topic_retriever.retrieve(user_message, q_emb, max_k=Config.TOPIC_MAX_UPGRADE)
            topic_grades = self._grade_topics_by_radius(
                turn_to_topic, topic_data, l1_embeddings, q_emb, retrieved_topics)
            stats.topic_count = len(topic_data)
            stats.topic_retrieved_count = len(retrieved_topics)

        # ── 工具轮独立检索（不变）──
        with stats.time_phase("tool_retrieval"):
            budget = self._available_budget(context_length, messages, tail_start, tool_tail_turns, idx_to_turn, tool_key_map)
            retriever = Retriever(snapshot)
            tool_upgrades_raw = retriever.retrieve_tools(user_message, query_embedding=q_emb, max_k=Config.TOOL_MAX_UPGRADE_K) if budget > 0 else []
            tool_candidates = self._build_tool_candidates(
                tool_upgrades_raw, tool_l1_texts, tool_l0_texts, tool_key_map
            )
            selected_tools = self._select_upgrades(tool_candidates, budget)
            stats.tool_upgrade_count = len(selected_tools)

        # ── 话题级 plan（含工具轮绑定）──
        with stats.time_phase("plan"):
            plan = self._compute_turn_plan_v2(
                messages, l1_texts, l0_texts, tool_l1_texts, tool_l0_texts,
                tail_start, tool_tail_turns, idx_to_turn, tool_key_map,
                budget, turn_to_topic, topic_grades, topic_data, retrieved_topics, selected_tools
            )
            self.store.write_turn_plan(self._session_id, [e.as_dict() for e in plan])

        with stats.time_phase("build"):
            final = self._build_messages_from_plan(plan, messages)

        if Config.is_dedup_enabled():
            with stats.time_phase("dedup"):
                final = self._deduplicate_messages(final)

        stats.finalize(tokens_before, sum(self._token_estimate(m.get("content", "")) for m in final))
        if Config.DEBUG_MODE:
            logger.debug("Assemble stats: %s", stats)
        return final


    def _available_budget(self, context_length, messages, tail_start, tool_tail_turns, idx_to_turn, tool_key_map):
        system_tokens = 0
        tail_tokens = 0

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "user")
            if role == "system":
                system_tokens += self._token_estimate(msg.get("content", ""))
                i += 1
                continue

            if "tool_calls" in msg:
                if i in tool_key_map:
                    entries = tool_key_map[i]
                    for key, token_count in entries:
                        if key[0] in tool_tail_turns:
                            tail_tokens += token_count
                    group_len = len(self._extract_tool_group(messages, i)[0])
                    i += group_len
                else:
                    i += 1
                continue

            turn = idx_to_turn.get(i, i)
            token = self._token_estimate(msg.get("content", ""))
            if i >= tail_start:
                tail_tokens += token
            i += 1

        used = system_tokens + tail_tokens
        return max(0, int(context_length * 0.95) - used)

    def set_system_overhead(self, overhead: int):
        """(已弃用) 动态测量不可行——Hermes 不暴露 tool schemas 等非消息开销。
        测量代码已于 2026-06-14 移除。保留方法作为公开 API 以防外部调用。"""
        if overhead > 0:
            self._system_overhead = overhead

    def _build_tool_candidates(self, tool_keys, tool_l1_texts, tool_l0_texts, tool_key_map):
        """仅工具轮候选（对话轮已由话题级决策处理）。"""
        candidates = []
        for rank, key in enumerate(tool_keys):
            found = False
            for entries in tool_key_map.values():
                for entry_key, token_count in entries:
                    if entry_key == key:
                        summary = tool_l1_texts.get(key) or tool_l0_texts.get(key, "")
                        saving = max(0, token_count - self._token_estimate(summary))
                        rrf_score = 1.0 / (Config.RETRIEVAL_RRF_K + rank)
                        candidates.append({"type": "tool", "key": key, "saving": saving, "rrf_score": rrf_score})
                        found = True
                        break
                if found:
                    break
        return candidates

    def _select_upgrades(self, candidates: List[Dict], budget: int) -> List:
        candidates.sort(key=lambda c: (0 if c["type"] == "dialogue" else 1, -c["rrf_score"]))
        selected = []
        remaining = budget
        for c in candidates:
            if c["saving"] <= 0:
                continue
            if remaining <= 0:
                break
            selected.append(c["key"])
            remaining -= c["saving"]
        return selected

    # ── 话题分割 + 三级定级（v4.6.0）──

    def _jaccard_tokens(self, fields_a: Dict, fields_b: Dict) -> float:
        """计算两个对话轮 L1 5 字段的 Jaccard 相似度。
        中文用字符二元组，英文用原词，union 全部 5 字段。
        """
        def _tokenize_field(val) -> set:
            tokens = set()
            if isinstance(val, str):
                for ch in val:
                    if '\u4e00' <= ch <= '\u9fff':
                        tokens.add(ch)
                    else:
                        for word in ch.split():
                            if word.strip():
                                tokens.add(word.lower())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for ch in item:
                            if '\u4e00' <= ch <= '\u9fff':
                                tokens.add(ch)
                            else:
                                for word in ch.split():
                                    if word.strip():
                                        tokens.add(word.lower())
            return tokens

        bag_a: set = set()
        bag_b: set = set()
        for key in ("core_change", "new_materials", "objective_facts", "consensus", "todo"):
            bag_a.update(_tokenize_field(fields_a.get(key)))
            bag_b.update(_tokenize_field(fields_b.get(key)))

        # 中文二元组提升（对 CJK 序列做字符二元组）
        def _add_bigrams(s: set) -> set:
            cjk_chars = [c for c in ''.join(s) if '\u4e00' <= c <= '\u9fff']
            bigrams = set()
            for i in range(len(cjk_chars) - 1):
                bigrams.add(cjk_chars[i] + cjk_chars[i + 1])
            return s | bigrams

        bag_a = _add_bigrams(bag_a)
        bag_b = _add_bigrams(bag_b)

        union_len = len(bag_a | bag_b)
        if union_len == 0:
            return 0.0
        return len(bag_a & bag_b) / union_len

    def _is_bg_turn(self, l1_fields: Optional[Dict]) -> bool:
        """R1 检测：4 个材料字段全空 → BG 轮。"""
        if l1_fields is None:
            return True
        for key in ("new_materials", "objective_facts", "consensus", "todo"):
            val = l1_fields.get(key)
            if isinstance(val, list) and len(val) > 0:
                return False
            if isinstance(val, str) and val.strip():
                return False
        return True

    def _compute_topic_groups(self, l1_texts: Dict[int, str],
                               l1_embeddings: Dict[int, List[float]]) -> Tuple[Dict[int, int], Dict]:
        """话题分割：R1（BG 检测）+ R2（Jaccard + todo 链）。
        
        Returns:
            turn_to_topic: Dict[turn_index → topic_id]
            topic_data: Dict[topic_id → {
                "turn_indices": [...],
                "agg_text": str,        # topic 内 5 字段拼接文本
                "centroid": [...],      # topic 形心
                "max_intra": float,     # topic 内最大形心距离
                "is_bg": bool,
                "nearest_centroid_dist": float,  # 最近邻异 topic 形心距离
            }]
        """
        sorted_turns = sorted(l1_texts.keys())
        if not sorted_turns:
            return {}, {}

        # 解析每个对话轮的 L1 JSON
        turn_fields: Dict[int, Optional[Dict]] = {}
        for t in sorted_turns:
            try:
                turn_fields[t] = json.loads(l1_texts[t])
            except (json.JSONDecodeError, TypeError):
                turn_fields[t] = None

        # 首次扫描：R1 BG 分类（字段存在性）
        turn_bg: Dict[int, bool] = {}
        for t in sorted_turns:
            turn_bg[t] = self._is_bg_turn(turn_fields[t])

        # R1+R2 合并：逐轮扫描
        turn_to_topic: Dict[int, int] = {}
        topic_counter = 1
        chain_turns: List[int] = []  # 当前链中的对话轮索引

        def _start_new_topic(turn_idx: int) -> None:
            nonlocal topic_counter, chain_turns
            topic_counter += 1
            chain_turns = [turn_idx]
            turn_to_topic[turn_idx] = topic_counter

        def _extends_chain(turn_idx: int) -> None:
            chain_turns.append(turn_idx)
            turn_to_topic[turn_idx] = topic_counter

        # 第 0 轮
        if sorted_turns:
            chain_turns = [sorted_turns[0]]
            turn_to_topic[sorted_turns[0]] = topic_counter

        for i in range(1, len(sorted_turns)):
            turn = sorted_turns[i]
            prev = sorted_turns[i - 1]
            prev_bg = turn_bg[prev]
            curr_bg = turn_bg[turn]

            if prev_bg and curr_bg:
                # 连续 BG → 合并
                _extends_chain(turn)
            elif prev_bg and not curr_bg:
                # BG → 实义 → 分裂
                _start_new_topic(turn)
            elif not prev_bg and curr_bg:
                # 实义 → BG → 分裂
                _start_new_topic(turn)
            else:
                # 双实义：R2 Jaccard + todo 链
                prev_fields = turn_fields[prev]
                curr_fields = turn_fields[turn]
                
                j = self._jaccard_tokens(prev_fields or {}, curr_fields or {})
                
                # todo 重叠检测
                prev_todo = set()
                if prev_fields:
                    todo_val = prev_fields.get("todo", [])
                    if isinstance(todo_val, list):
                        prev_todo = set(str(v) for v in todo_val)
                    elif isinstance(todo_val, str):
                        prev_todo = {todo_val}
                
                curr_core = set()
                if curr_fields:
                    core = curr_fields.get("core_change", "")
                    if isinstance(core, str):
                        curr_core.add(core)
                    new_mat = curr_fields.get("new_materials", [])
                    if isinstance(new_mat, list):
                        curr_core.update(str(v) for v in new_mat)
                
                todo_overlap = prev_todo & curr_core
                has_todo_overlap = len(todo_overlap) >= 1

                # 在链中判断
                is_in_chain = len(chain_turns) > 1
                
                if has_todo_overlap and j >= Config.TOPIC_JACCARD_CHAIN and is_in_chain:
                    _extends_chain(turn)
                elif has_todo_overlap and j >= Config.TOPIC_JACCARD_ENTRY:
                    _extends_chain(turn)
                else:
                    _start_new_topic(turn)

        # 构建 topic_data
        topic_data: Dict[int, Dict] = {}
        for t_idx, topic_id in turn_to_topic.items():
            if topic_id not in topic_data:
                topic_data[topic_id] = {
                    "turn_indices": [],
                    "agg_text": "",
                    "centroid": None,
                    "max_intra": 0.0,
                    "is_bg": True,
                    "nearest_centroid_dist": 0.0,
                }
            td = topic_data[topic_id]
            td["turn_indices"].append(t_idx)
            if not turn_bg[t_idx]:
                td["is_bg"] = False
            # 累加 agg_text
            fields = turn_fields.get(t_idx)
            if fields:
                parts = []
                for key in ("core_change", "new_materials", "objective_facts", "consensus", "todo"):
                    val = fields.get(key)
                    if isinstance(val, list):
                        parts.append(" ".join(str(v) for v in val))
                    elif val:
                        parts.append(str(val))
                if td["agg_text"]:
                    td["agg_text"] += " | "
                td["agg_text"] += " ".join(parts)

        # 计算 topic 形心
        for topic_id, td in topic_data.items():
            if not td["is_bg"]:
                emb_list = []
                for t_idx in td["turn_indices"]:
                    if t_idx in l1_embeddings:
                        emb = l1_embeddings[t_idx]
                        if emb:
                            emb_list.append(emb)
                if emb_list:
                    n = len(emb_list)
                    centroid = [sum(emb[i] for emb in emb_list) / n for i in range(len(emb_list[0]))]
                    td["centroid"] = centroid
                    if n == 1:
                        # 单轮话题：自身到形心的距离精确为 0
                        td["max_intra"] = 0.0
                    else:
                        max_dist = 0.0
                        for emb in emb_list:
                            d = 1.0 - cosine_similarity(emb, centroid)
                            max_dist = max(max_dist, d)
                        td["max_intra"] = max_dist

        # 计算最近邻形心距离
        centroids = {tid: td["centroid"] for tid, td in topic_data.items()
                     if td["centroid"] is not None}
        for topic_id, td in topic_data.items():
            if td["centroid"] is None:
                continue
            min_dist = float("inf")
            for other_id, other_centroid in centroids.items():
                if other_id == topic_id:
                    continue
                d = 1.0 - cosine_similarity(td["centroid"], other_centroid)
                min_dist = min(min_dist, d)
            td["nearest_centroid_dist"] = min_dist if min_dist != float("inf") else 0.0

        logger.info("[CA] topic segmentation: %d topics from %d dialogue turns (BG=%d)",
                     len(topic_data), len(sorted_turns),
                     sum(1 for td in topic_data.values() if td["is_bg"]))
        return turn_to_topic, topic_data

    def _grade_topics_by_radius(self, turn_to_topic: Dict[int, int],
                                 topic_data: Dict,
                                 l1_embeddings: Dict[int, List[float]],
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

    def _compute_turn_plan_v2(self, messages, l1_texts, l0_texts,
                               tool_l1_texts, tool_l0_texts,
                               tail_start, tool_tail_turns, idx_to_turn, tool_key_map,
                               budget, turn_to_topic, topic_grades, topic_data,
                               retrieved_topics, selected_tools) -> List[TurnPlanEntry]:
        """话题级拣选决策 + 工具轮绑定。

        对话轮决策规则：
          tail           → L2 (tail)
          BG topic       → TOPIC_BG_LEVEL (default L0)
          L2 grade       → L2 (topic_core)
          L1 grade       → L1 (topic_baseline)
          L0 grade       → L0 (topic_degraded)

        工具轮决策规则：
          tool_tail                       → L2 (tail)
          parent dialogue in L2 topic     → L1 (topic_boost)
          retrieved + L1                  → L1 (retrieved)
          retrieved + L0                  → L0 (retrieved)
          else                            → L0 (middle)
        """
        entries: List[TurnPlanEntry] = []

        # ── 对话轮 ──
        for turn in sorted(l1_texts.keys()):
            l1 = l1_texts.get(turn) or ""
            l0 = l0_texts.get(turn) or ""
            l1_valid = self._is_valid_summary(l1)

            in_tail = any(i >= tail_start for i, mi in enumerate(messages)
                         if mi.get("_turn_index", i) == turn
                         and mi.get("role") not in ("system", "tool")
                         and "tool_calls" not in mi)

            l2_tk = sum(self._token_estimate(m.get("content", ""))
                       for m in messages
                       if m.get("_turn_index", -1) == turn
                       and m.get("role") not in ("system", "tool")
                       and "tool_calls" not in m)
            sum_tk = self._token_estimate(l1) if l1_valid else self._token_estimate(l0 or "")

            topic_id = turn_to_topic.get(turn)
            topic_grade = topic_grades.get(topic_id, "L0") if topic_id is not None else "L0"
            td = topic_data.get(topic_id, {}) if topic_id is not None else {}

            entry = TurnPlanEntry(turn_index=turn, turn_type="dialogue",
                                  l2_tokens=l2_tk, summary_tokens=sum_tk,
                                  tokens_saved=max(0, l2_tk - sum_tk),
                                  budget_remaining=budget,
                                  topic_group=topic_id)

            if in_tail:
                entry.target_level = "L2"
                entry.decision_reason = "tail"
            elif topic_id is not None and td.get("is_bg"):
                entry.target_level = Config.TOPIC_BG_LEVEL
                entry.decision_reason = "topic_bg"
            elif topic_grade == "L2":
                entry.target_level = "L2"
                entry.decision_reason = "topic_core"
            elif topic_grade == "L1":
                entry.target_level = "L1"
                entry.decision_reason = "topic_baseline"
            else:  # L0
                entry.target_level = "L0"
                entry.decision_reason = "topic_degraded"
            entries.append(entry)

        # ── 工具轮 ──
        for key, l1 in sorted(tool_l1_texts.items(), key=lambda x: (x[0][0], x[0][1])):
            l0 = tool_l0_texts.get(key) or ""
            turn_idx = key[0]
            in_tail = turn_idx in tool_tail_turns

            # 父对话轮所属 topic 是否 L2 级
            parent_topic_id = turn_to_topic.get(turn_idx)
            parent_topic_grade = topic_grades.get(parent_topic_id, "L0") if parent_topic_id is not None else "L0"

            l2_tk = 0
            for msg_idx, entries_map in tool_key_map.items():
                for (ekey, tk) in entries_map:
                    if ekey == key:
                        l2_tk = tk
                        break
                if l2_tk > 0:
                    break

            sum_tk = self._token_estimate(l1) or self._token_estimate(l0 or "")

            entry = TurnPlanEntry(turn_index=turn_idx, turn_type="tool",
                                  tool_sub_index=key[1],
                                  l2_tokens=l2_tk, summary_tokens=sum_tk,
                                  tokens_saved=max(0, l2_tk - sum_tk),
                                  budget_remaining=budget,
                                  topic_group=parent_topic_id)

            if in_tail:
                entry.target_level = "L2"
                entry.decision_reason = "tail"
            elif parent_topic_grade == "L2":
                # 父 topic 整体 L2 → 工具轮升 L1
                if l1:
                    entry.target_level = "L1"
                    entry.decision_reason = "topic_boost"
                elif l0:
                    entry.target_level = "L0"
                    entry.decision_reason = "topic_boost"
                else:
                    entry.target_level = "L2"
                    entry.decision_reason = "topic_boost"
            elif key in selected_tools:
                summary = l1 or l0
                if summary:
                    entry.target_level = "L1" if l1 else "L0"
                    entry.decision_reason = "retrieved"
                else:
                    entry.target_level = "L2"
                    entry.decision_reason = "retrieved"
            elif l0:
                entry.target_level = "L0"
                entry.decision_reason = "middle"
            else:
                entry.target_level = "L2"
                entry.decision_reason = "middle"
            entries.append(entry)

        # 按对话原始顺序排序
        entries.sort(key=lambda e: (e.turn_index, 0 if e.turn_type == "dialogue" else 1, e.tool_sub_index))
        return entries

    def _build_messages_from_plan(self, plan: List[TurnPlanEntry],
                                   messages: List[Dict]) -> List[Dict]:
        """按 turn_plan 决策从 turn_cache 按 level 读取文本，构建消息列表。

        对每条 plan entry：
        - target_level=L2 → 读取 l2_text（JSON 消息数组）展开为多条消息
        - target_level=L1 → 生成单条 [~/N] 或 [~/N/M] 摘要消息
        - target_level=L0 → 同上，使用 l0_text
        - 缺失文本时 → 回退到 L2（如果存在），再回退到更低级别

        system 消息从原始 messages 列表透传（不在 turn_cache 中）。
        plan 未覆盖的消息（如当前轮用户消息）原样追加到末尾。
        """
        result: List[Dict] = []

        # 透传 system 消息
        for msg in messages:
            if msg.get("role") == "system":
                result.append(msg)

        # 记录 plan 覆盖的 (turn_index, turn_type) 组合
        covered: set = set()
        for entry in plan:
            key = (entry.turn_index, entry.turn_type)
            covered.add(key)

            l2, l1, l0 = self.store.read_turn_texts(
                self._session_id, entry.turn_index,
                entry.turn_type, entry.tool_sub_index
            )
            # 对话轮退化条目过滤：_assemble_status=1 → l0/l1 不可用，跳过注入
            # （仅检查 DB 记录条目的退化状态，不影响 add_turn 写入 cache 但未落库的用例）
            if entry.turn_type == "dialogue" and l2 is not None:
                db_status = self.store.read_assemble_status(
                    self._session_id, entry.turn_index,
                    entry.turn_type, entry.tool_sub_index
                )
                if db_status == 1:  # ASSEMBLE_PENDING_BACKFILL
                    continue
            is_tool = entry.turn_type == "tool"
            prefix = f"[~/{entry.turn_index}"
            if is_tool:
                prefix += f"/{entry.tool_sub_index}"
            else:
                prefix += "/0"
            prefix += "] "

            # 对话轮 L1 → 格式化为可读文本（替代原始 JSON 注入）
            l1_display = self._format_l1_for_display(l1) if entry.turn_type == "dialogue" else l1

            if entry.target_level == "L2":
                # 保留原文
                if l2:
                    self._extend_with_l2(result, l2, entry.turn_index)
                elif l1 or l0:
                    # L2 不可用，降级到摘要
                    text = l1_display or l0
                    result.append({"role": "assistant", "content": f"{prefix}{text}"})
                # else: 无任何文本，跳过
            elif entry.target_level == "L1":
                if l1_display:
                    result.append({"role": "assistant", "content": f"{prefix}{l1_display}"})
                elif l0:
                    result.append({"role": "assistant", "content": f"{prefix}{l0}"})
                elif l2:
                    self._extend_with_l2(result, l2, entry.turn_index)
            else:  # L0
                if l0:
                    result.append({"role": "assistant", "content": f"{prefix}{l0}"})
                elif l1_display:
                    result.append({"role": "assistant", "content": f"{prefix}{l1_display}"})
                elif l2:
                    self._extend_with_l2(result, l2, entry.turn_index)

        # 追加 plan 未覆盖的消息（当前轮用户消息等）
        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                continue
            turn_idx = msg.get("_turn_index", -1)
            # 判断消息属于对话轮还是工具轮
            msg_turn_type = "tool" if ("tool_calls" in msg or role == "tool") else "dialogue"
            if (turn_idx, msg_turn_type) not in covered:
                result.append(msg)

        return result

    @staticmethod
    def _extend_with_l2(result: List[Dict], l2_text: str, turn_index: int) -> None:
        """将 l2_text (JSON 消息数组) 展开到 result，标记 _turn_index。"""
        try:
            msgs = json.loads(l2_text)
            if isinstance(msgs, list):
                for m in msgs:
                    m["_turn_index"] = turn_index
                result.extend(msgs)
            else:
                result.append({"role": "user", "content": l2_text, "_turn_index": turn_index})
        except (json.JSONDecodeError, TypeError):
            result.append({"role": "user", "content": l2_text, "_turn_index": turn_index})

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

    def _is_valid_summary(self, l1_text: str) -> bool:
        if not l1_text or not l1_text.strip():
            return False
        try:
            data = json.loads(l1_text)
            core = data.get("core_change", "")
            return bool(core and core != "本轮无新内容")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False

    def _format_l1_for_display(self, l1_text: str) -> str:
        """将对话轮 L1 JSON 摘要格式化为可读文本，替代原始 JSON 注入。"""
        if not l1_text or not l1_text.strip():
            return l1_text
        try:
            data = json.loads(l1_text)
        except (json.JSONDecodeError, TypeError):
            return l1_text
        core = data.get("core_change", "")
        if not core:
            return l1_text
        lines = [core]
        for key in ("new_materials", "objective_facts"):
            items = data.get(key, [])
            if items:
                joined = " | ".join(str(i)[:240] for i in items)
                lines.append(f"  {joined}")
        return "\n".join(lines)

    def _compute_tail_start(self, messages):
        """从消息尾部反向累计对话消息的 token 数，找到对话 tail 保护区。

        工具响应（role=tool）不参与计数——它们已有独立的 TOOL_TAIL_TURN_COUNT
        保护机制。对话 tail 只需保护对话文本的最近 ~N tokens。
        """
        tail_tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "tool":
                continue
            tail_tokens += self._token_estimate(messages[i].get("content", ""))
            if tail_tokens >= Config.PROTECT_TAIL_TOKENS:
                return i
        return 0

    def _hard_truncation(self, messages, context_length=32000):
        if not messages:
            return []
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        groups = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if "tool_calls" in msg:
                group = [msg]
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    group.append(messages[i])
                    i += 1
                groups.append(("tool", group))
            else:
                groups.append(("msg", [msg]))
                i += 1
        budget = int(context_length * 0.95) - sum(self._token_estimate(m.get("content", "")) for m in sys_msgs)
        if budget <= 0:
            return sys_msgs
        kept = []
        for gtype, grp in reversed(groups):
            group_tokens = sum(self._token_estimate(m.get("content", "")) for m in grp)
            if budget >= group_tokens:
                kept.insert(0, (gtype, grp))
                budget -= group_tokens
            else:
                break
        result = list(sys_msgs)
        if sum(len(g) for _, g in kept) < sum(len(g) for _, g in groups):
            result.append({"role": "assistant", "content": "[工具调用结果因上下文截断已被省略]"})
        for _, grp in kept:
            result.extend(grp)
        return result

    # 匹配 [~/N] 或 [~/N/M] 前缀标签（N,M 为正整数）
    _CA_TAG_RE = re.compile(r'^\[~/\d+(?:/\d+)?\]\s*')

    def _deduplicate_messages(self, messages):
        """全指纹去重，含跨轮摘要标签归一化 + 原位指向标记。

        对 content 中的 [~/N] / [~/N/M] 前缀标签做剥离后再计算指纹，
        使跨轮相同摘要（同名工具同结果、同 core_change 对话轮等）可命中同一指纹。
        重复项保留**最先**出现的那条（留最先），后续重复在原位替换为
        指向标记 `(同[~/N/0])` / `(同[~/N/m])`，指向第一次出现的 tag。
        两位格式统一：对话轮 `[~/N/0]`，工具轮 `[~/N/m]`。
        留最先策略：首次出现位置永远不动 → 前缀稳定；
        标记在原位替换不会进一步破坏缓存（重复位置本就要变动）。
        system 消息豁免，始终保留。
        """
        # Pass 1: 计算每条消息的指纹，收集所有出现位置及其标签
        fp_occurrences: dict = {}  # fingerprint → [(index, tag_str), ...]
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                continue
            key = self._msg_fingerprint(msg)
            tag = ""
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("[~/"):
                end = content.find("]")
                if end > 0:
                    tag = content[:end+1]
            fp_occurrences.setdefault(key, []).append((i, tag))

        # Pass 2: 为重复组构建标记映射：later_idx → first_tag
        # 留最先：第一次出现的位置永远保留，后续位置插入指向标记
        placeholders: dict = {}  # later_idx → placeholder_text
        for key, occurrences in fp_occurrences.items():
            if len(occurrences) <= 1:
                continue
            first_idx, first_tag = occurrences[0]
            ref_tag = first_tag if first_tag else ""
            if not ref_tag:
                continue  # 无标签的不做标记
            for idx, _ in occurrences[1:]:
                placeholders[idx] = f"(同{ref_tag})"

        # ── Pass 3: 构建输出 ──
        # 首次出现保留不动（前缀稳定），后续重复在原位替换为指向标记。
        # 标记 (同[~/N/0]) / (同[~/N/m]) 指向第一次出现的 tag。
        # 原位替换仅影响重复位置（它们本就要变动），首次出现位置永远不变。
        keep_first = {occ[0][0] for occ in fp_occurrences.values()}
        deduped = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                deduped.append(msg)
            elif i in keep_first:
                deduped.append(msg)
            elif i in placeholders:
                # 被删位置 → 指向首次出现的标记，原位保留不偏移
                deduped.append({
                    "role": "assistant",
                    "content": placeholders[i]
                })
            # else: singletons not in fp_occurrences? shouldn't happen, but skip

        if Config.DEBUG_MODE:
            logger.debug("dedup: before=%d, after=%d, placeholders=%d",
                         len(messages), len(deduped), len(placeholders))
        return deduped

    def _msg_fingerprint(self, msg: dict) -> str:
        """计算消息指纹，CA 摘要标签 [~/N] / [~/N/M] 剥离后归一化。"""
        content = msg.get("content", "")
        if isinstance(content, str):
            stripped = self._CA_TAG_RE.sub('', content)
            if stripped != content:
                msg = {**msg, "content": stripped}
        normalized = self._deep_normalize(msg)
        return hashlib.sha256(
            json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _deep_normalize(self, obj, depth=0):
        if depth > 10:
            if isinstance(obj, dict):
                return {k: "[DEPTH_LIMITED]" for k in list(obj.keys())[:3]}
            if isinstance(obj, list):
                return ["[DEPTH_LIMITED]"] * min(3, len(obj))
            return str(obj)[:200]
        if obj is None:
            return ""
        if isinstance(obj, dict):
            return {k: self._deep_normalize(v, depth + 1) for k, v in sorted(obj.items())}
        if isinstance(obj, list):
            return [self._deep_normalize(item, depth + 1) for item in obj]
        if isinstance(obj, str):
            try:
                parsed = json.loads(obj)
                if isinstance(parsed, (dict, list)):
                    return self._deep_normalize(parsed, depth + 1)
            except Exception:
                pass
            return obj
        return obj

    def _call_llm_for_l1(self, prev_l1, l2_text) -> Tuple[str, str]:
        """返回 (response_text, finish_reason)。

        所有重试均失败时返回 ("", "error")。
        """
        import urllib.request
        llm_start = time.monotonic()
        prompt = L1_GENERATION_PROMPT.format(
            previous_summary=format_previous_summary_for_prompt(
                json.dumps(prev_l1, ensure_ascii=False) if prev_l1 else None
            ),
            current_dialog=l2_text)
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
        logger.warning("[CA-METRIC] ca.l1.latency_ms: turn=%d, ms=%d", self._turn_counter, elapsed_ms)
        self.stats.l1_latency_ms += elapsed_ms

        # 所有重试均失败，返回退化输出
        if finish_reason == "error":
            self.stats.truncated_fallback += 1
            return ("", "error")

        return (response_text, finish_reason)

    def _extract_l0(self, l1_dict):
        core = l1_dict.get("core_change", "")
        if not core or core in ("无", "本轮无新内容"):
            logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", self._turn_counter)
            return "无"
        return _safe_truncate(core, 100)

    def _estimate_token_offset(self, history):
        return sum(self._token_estimate(m.get("content", "")) for m in history) if history else 0

    def _get_previous_l1(self) -> Optional[Dict]:
        with self._task_lock:
            current_turn = self._turn_counter
        if current_turn <= 0:
            return None
        prev_idx = current_turn - 1
        if prev_idx in self.cache.l1_texts:
            try:
                data = json.loads(self.cache.l1_texts[prev_idx])
                if data.get("core_change") != "本轮无新内容":
                    return data
            except Exception:
                pass
        rec = self.store.read_turn(self._session_id, prev_idx)
        if rec:
            try:
                return json.loads(rec["l1_text"])
            except Exception:
                pass
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
