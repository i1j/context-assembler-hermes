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
import logging
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .store import SQLiteStore
from .cache import AssemblyCache, CacheBuilder, BM25Snapshot, tokenise
from .retrieval import Retriever
from .embedding import EmbeddingClient
from .ooda_parser import OODAParser
from .post_process import robust_json_parse, clean_increment
from .prompts import L1_GENERATION_PROMPT
from .stats import AssembleStats

try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"
from .tool_summarizer import ToolSummarizer
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
        self._pre_upgraded_tool_turns: set = set()
        self._pre_upgraded_lock = threading.Lock()
        self.stats = AssembleStats()

        self._dialogue_backfill = BackfillThread(self, 'dialogue', Config.BACKFILL_DIALOGUE_RATE)
        self._tool_backfill = BackfillThread(self, 'tool', Config.BACKFILL_TOOL_RATE)
        self._dialogue_backfill.start()
        self._tool_backfill.start()

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
        # 1. 先等待所有 C‑stage 任务完成
        self.wait_for_pending(Config.SHUTDOWN_TIMEOUT)
        # 2. 再停止 L‑stage 线程
        self._dialogue_backfill.shutdown()
        self._tool_backfill.shutdown()
        self._dialogue_backfill.join(timeout=Config.SHUTDOWN_TIMEOUT)
        self._tool_backfill.join(timeout=Config.SHUTDOWN_TIMEOUT)
        if self._dialogue_backfill.is_alive() or self._tool_backfill.is_alive():
            logger.warning("L-stage threads did not exit cleanly")
        self.cache.cancel_retry_timer()
        self._shutdown_cache_executor()
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
                           conversation_history: Optional[List[Dict]] = None,
                           messages: Optional[List[Dict]] = None) -> int:
        expected = len([m for m in (conversation_history or []) if m.get("role") == "user"])
        with self._task_lock:
            target = max(self._turn_counter + 1, expected)
            if target in self._pending_tasks and self._pending_tasks[target].is_alive():
                logger.info("[CA] process_turn_async: turn %d already pending, returning", target)
                return target
            self._turn_counter = target
            turn_index = target

        logger.info("[CA] process_turn_async: starting C-stage for turn %d (expected=%d, messages=%s)",
                    turn_index, expected,
                    "yes" if messages else "no")

        l2_text = f"User: {user_message}\nAssistant: {assistant_response}"
        prev_l1 = self._get_previous_l1()
        token_offset = self._estimate_token_offset(conversation_history)

        # 在主线程捕获 write_origin（ContextVar 不自动传播到 daemon 线程）
        _bg_review = (get_current_write_origin() == "background_review")
        if _bg_review:
            logger.info("[CA] process_turn_async: background review detected for turn %d", turn_index)

        thread = threading.Thread(
            target=self._run_c_stage,
            args=(self._session_id, turn_index, prev_l1, l2_text, token_offset, messages,
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
                     messages=None, user_message="", assistant_response="",
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
                    "_assemble_status": 0,
                    "new_materials": [],
                    "objective_facts": [],
                    "consensus": [],
                    "todo": []
                }
                dialogue_ok = True
            else:
                logger.info("[CA] _run_c_stage turn %d: calling LLM", turn_index)
                ooda_text = self._call_llm_for_l1(prev_l1, l2_text)
                parsed = self.ooda_parser.parse(ooda_text, previous_summary=prev_l1)
                robust, _ = robust_json_parse(json.dumps(parsed, ensure_ascii=False))
                cleaned = clean_increment(robust)
                is_llm_fallback = ooda_text.startswith("核心摘要：无有效增量") and "资源与观察：\n- 无" in ooda_text
                cleaned["_assemble_status"] = 1 if is_llm_fallback else 0
                if not is_llm_fallback:
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

            if messages:
                tool_turns = self._extract_tool_calls(messages)
                for sub_index, turn in enumerate(tool_turns, start=1):
                    try:
                        tool_l1, tool_l0 = self.tool_summarizer.summarize(turn["tool_call"], turn["tool_responses"])
                        try:
                            tool_l1_emb = self.embed_client.embed(json.dumps(tool_l1, ensure_ascii=False))
                            tool_l0_emb = self.embed_client.embed(tool_l0)
                        except Exception:
                            tool_l1_emb = None
                            tool_l0_emb = None
                        self.store.write_turn(
                            session_id, turn_index,
                            l0_text=tool_l0,
                            l1_text=json.dumps(tool_l1, ensure_ascii=False),
                            l0_embedding=tool_l0_emb, l1_embedding=tool_l1_emb,
                            token_offset=token_offset,
                            turn_type='tool', tool_sub_index=sub_index,
                            l2_text=json.dumps(turn["l2_messages"], ensure_ascii=False),
                            _assemble_status=0
                        )
                        self.cache.add_tool_turn(turn_index, sub_index, tool_l0, json.dumps(tool_l1, ensure_ascii=False), tool_l0_emb, tool_l1_emb)
                    except Exception as e:
                        logger.error("Tool call %d-%d failed: %s", turn_index, sub_index, e)

            if dialogue_ok:
                self._pre_upgrade_tools(cleaned, turn_index)
                with self._pre_upgraded_lock:
                    self.stats.tool_pre_upgrade_count = len(self._pre_upgraded_tool_turns)

        except Exception as e:
            logger.error("C‑stage crash turn %d: %s", turn_index, e, exc_info=True)
            fallback = json.dumps({
                "core_change": "无有效增量",
                "_assemble_status": 1,
                "new_materials": [], "objective_facts": [],
                "consensus": [], "todo": []
            }, ensure_ascii=False)
            self.store.write_turn(
                session_id, turn_index,
                l0_text="无有效增量",
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
            self.cache.add_turn(turn_index, "无有效增量", fallback, None, None)
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

    def _pre_upgrade_tools(self, dialogue_l1: Dict, current_turn: int):
        snapshot = self.cache.get_bm25_snapshot()
        if not snapshot or not snapshot.tool_bm25:
            return
        query_text = dialogue_l1.get("core_change", "")
        if not query_text.strip():
            return
        query_tokens = tokenise(query_text)
        scores = snapshot.tool_bm25.get_scores(query_tokens)
        k = min(Config.TOOL_PRE_UPGRADE_COUNT, len(scores))
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        top_indices = [idx for idx in top_indices if scores[idx] > 0]
        with self._pre_upgraded_lock:
            self._pre_upgraded_tool_turns.clear()
            tool_names = []
            for idx in top_indices:
                key = snapshot.tool_bm25.get_turn_key(idx)
                if key:
                    self._pre_upgraded_tool_turns.add(key)
                    l1 = self.cache.tool_l1_texts.get(key)
                    if l1:
                        try:
                            name = json.loads(l1).get("tool_name", "?")
                        except Exception:
                            name = "?"
                        tool_names.append(name)
        logger.debug("Pre-upgraded %d tool turns for turn %d: %s",
                     len(self._pre_upgraded_tool_turns), current_turn,
                     tool_names[:3] if tool_names else "none")

    # ---------- A‑stage ----------
    def _rebuild_messages_from_cache(self) -> List[Dict]:
        """从 l2_text 重建消息列表。

        每条 record 的 l2_text 是该轮独立的消息 JSON 数组：
        - dialogue 轮: [{"role": "user", ...}, {"role": "assistant", ...}]
        - tool 轮:     [{"role": "assistant", "tool_calls": [...]}, {"role": "tool", ...}]

        按 turn_index + turn_type 顺序拼接。

        TODO: 指纹去重 — 防止网络故障多路返回等完全重复信息污染上下文。
        原实现（_msg_fp + dialogue_count 累积快照去重）已随累计快照模式移除，
        需在合适的层级加回（A-stage 组装时或 C-stage 写入时）。
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

        with stats.time_phase("layers"):
            with self._pre_upgraded_lock:
                tool_head_snapshot = set(self._pre_upgraded_tool_turns)
            dialogue_head, dialogue_middle, tool_middle, tail_start = \
                self._compute_layers_v2(messages, l1_texts, tool_l1_texts, tool_head_snapshot)

        with stats.time_phase("embed_query"):
            try:
                q_emb = self.embed_client.embed(user_message)
            except Exception:
                q_emb = None

        with stats.time_phase("retrieval"):
            budget = self._available_budget(context_length, messages, dialogue_head, tool_head_snapshot, tail_start, idx_to_turn, tool_key_map)
            retriever = Retriever(snapshot)
            dial_upgrades = retriever.retrieve(user_message, query_embedding=q_emb, upgrade_budget=budget) if budget > 0 else []
            tool_upgrades_raw = retriever.retrieve_tools(user_message, query_embedding=q_emb, max_k=Config.TOOL_MAX_UPGRADE_K) if budget > 0 else []

            all_candidates = self._build_candidates(
                dial_upgrades, tool_upgrades_raw,
                l1_texts, l0_texts, tool_l1_texts, tool_l0_texts,
                tool_key_map, idx_to_turn, messages
            )
            upgrades = self._select_upgrades(all_candidates, budget)
            stats.tool_upgrade_count = sum(1 for k in upgrades if isinstance(k, tuple))

        with stats.time_phase("assemble"):
            final = self._build_final_messages_v4(
                messages, l1_texts, l0_texts, tool_l1_texts, tool_l0_texts,
                dialogue_head, tool_head_snapshot, dialogue_middle, tool_middle,
                upgrades, tail_start, idx_to_turn, tool_key_map
            )

        self._compute_and_store_turn_plan(
            messages, l1_texts, l0_texts, tool_l1_texts, tool_l0_texts,
            dialogue_head, tool_head_snapshot, upgrades,
            tail_start, idx_to_turn, tool_key_map, budget)

        if Config.is_dedup_enabled():
            with stats.time_phase("dedup"):
                final = self._deduplicate_messages(final)

        stats.finalize(tokens_before, sum(self._token_estimate(m.get("content", "")) for m in final))
        if Config.DEBUG_MODE:
            logger.debug("Assemble stats: %s", stats)
        return final

    def _compute_layers_v2(self, messages, l1_texts, tool_l1_texts, tool_head_snapshot):
        valid_dialogue = sorted(
            [idx for idx in l1_texts if self._is_valid_summary(l1_texts[idx])]
        )[-Config.HEAD_AUTO_L1_COUNT:]
        dialogue_head = set(valid_dialogue)
        tail_start = self._compute_tail_start(messages)
        dialogue_middle = set(l1_texts.keys()) - dialogue_head
        tool_middle = set(tool_l1_texts.keys()) - tool_head_snapshot
        return dialogue_head, dialogue_middle, tool_middle, tail_start

    def _available_budget(self, context_length, messages, dialogue_head, tool_head, tail_start, idx_to_turn, tool_key_map):
        system_tokens = 0
        head_tokens = 0
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
                        # 工具尾区优先级高于 head：同组若在 tail 则整组原文保留，
                        # 不应再计入 head_tokens
                        if i >= tail_start:
                            tail_tokens += token_count
                        elif key in tool_head:
                            head_tokens += token_count
                    group_len = len(self._extract_tool_group(messages, i)[0])
                    i += group_len
                else:
                    i += 1
                continue

            turn = idx_to_turn.get(i, i)
            token = self._token_estimate(msg.get("content", ""))
            if turn in dialogue_head:
                head_tokens += token
            elif i >= tail_start:
                # elif 确保 head 优先（head 摘要比 tail 原文更节省），避免双计
                tail_tokens += token
            i += 1

        used = system_tokens + head_tokens + tail_tokens
        return max(0, int(context_length * 0.95) - used)

    def _build_candidates(self, dial_keys, tool_keys, l1_texts, l0_texts,
                          tool_l1_texts, tool_l0_texts, tool_key_map,
                          idx_to_turn, messages):
        candidates = []
        for rank, key in enumerate(dial_keys):
            turn = key[0] if isinstance(key, tuple) else key
            original_tokens = sum(
                self._token_estimate(m.get("content", ""))
                for i, m in enumerate(messages)
                if idx_to_turn.get(i, i) == turn and m.get("role") not in ("system", "tool")
            )
            summary = l1_texts.get(turn) or l0_texts.get(turn, "")
            saving = max(0, original_tokens - self._token_estimate(summary))
            rrf_score = 1.0 / (Config.RETRIEVAL_RRF_K + rank)
            candidates.append({"type": "dialogue", "key": key, "saving": saving, "rrf_score": rrf_score})

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

    def _build_final_messages_v4(self, messages, l1_texts, l0_texts,
                                 tool_l1_texts, tool_l0_texts,
                                 dialogue_head, tool_head,
                                 dialogue_middle, tool_middle,
                                 upgrades, tail_start, idx_to_turn,
                                 tool_key_map):
        result = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "user")
            if role == "system":
                result.append(msg)
                i += 1
                continue
            if "tool_calls" in msg:
                entries = tool_key_map.get(i, [])
                group, next_i = self._extract_tool_group(messages, i)
                for idx_in_msg, (key, _) in enumerate(entries):
                    call = msg["tool_calls"][idx_in_msg]
                    call_id = call.get("id")
                    matched_responses = [r for r in group[1:] if r.get("tool_call_id") == call_id]
                    tool_msgs = [msg] + matched_responses
                    if i >= tail_start:
                        result.extend(tool_msgs)
                        continue
                    if key in tool_head and key not in upgrades:
                        l1 = tool_l1_texts.get(key)
                        if l1:
                            result.append({"role": "assistant", "content": f"[~/{key[0]}/{key[1]}] {l1}"})
                            continue
                    if key in upgrades:
                        summary = tool_l1_texts.get(key) or tool_l0_texts.get(key)
                        if summary:
                            result.append({"role": "assistant", "content": f"[~/{key[0]}/{key[1]}] {summary}"})
                        else:
                            result.extend(tool_msgs)
                    else:
                        # 中段末升级工具轮 → L0 一行摘要（与对话 middle 行为一致）
                        l0 = tool_l0_texts.get(key)
                        if l0:
                            result.append({"role": "assistant", "content": f"[~/{key[0]}/{key[1]}] {l0}"})
                        else:
                            result.extend(tool_msgs)
                i = next_i
                continue
            # 普通消息
            turn = idx_to_turn.get(i, i)
            if turn in dialogue_head:
                l1 = l1_texts.get(turn)
                if l1 and self._is_valid_summary(l1):
                    result.append({"role": "assistant", "content": f"[~/{turn}] {l1}"})
                else:
                    result.append(msg)
            elif i >= tail_start:
                # tail 保护：最近的消息保留原文，优先级高于 middle 降级
                result.append(msg)
            elif turn in dialogue_middle:
                if turn in upgrades:
                    l1 = l1_texts.get(turn)
                    if l1 and self._is_valid_summary(l1):
                        result.append({"role": "assistant", "content": f"[~/{turn}] {l1}"})
                    else:
                        result.append(msg)
                else:
                    l0 = l0_texts.get(turn)
                    if l0:
                        result.append({"role": "assistant", "content": f"[~/{turn}] {l0}"})
                    else:
                        result.append(msg)
            else:
                result.append(msg)
            i += 1
        return result

    def _compute_and_store_turn_plan(self, messages, l1_texts, l0_texts,
                                     tool_l1_texts, tool_l0_texts,
                                     dialogue_head, tool_head, upgrades,
                                     tail_start, idx_to_turn, tool_key_map,
                                     budget):
        """计算每轮拣选决策并写入 store.turn_plan，供调试比对。"""
        entries: List[TurnPlanEntry] = []

        # ── 对话轮 ──
        for turn in sorted(l1_texts.keys()):
            l1 = l1_texts.get(turn) or ""
            l0 = l0_texts.get(turn) or ""
            l1_valid = self._is_valid_summary(l1)

            # 确定该 turn 的任一消息是否在 tail 中
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

            entry = TurnPlanEntry(turn_index=turn, turn_type="dialogue",
                                  l2_tokens=l2_tk, summary_tokens=sum_tk,
                                  tokens_saved=max(0, l2_tk - sum_tk),
                                  budget_remaining=budget)

            if turn in dialogue_head and l1_valid:
                entry.target_level = "L1"
                entry.decision_reason = "head"
            elif in_tail:
                entry.target_level = "L2"
                entry.decision_reason = "tail"
            elif turn in upgrades and l1_valid:
                entry.target_level = "L1"
                entry.decision_reason = "retrieved"
            elif l0:
                entry.target_level = "L0"
                entry.decision_reason = "middle"
            else:
                entry.target_level = "L2"
                entry.decision_reason = "middle"
            entries.append(entry)

        # ── 工具轮 ──
        for key, l1 in sorted(tool_l1_texts.items(), key=lambda x: (x[0][0], x[0][1])):
            l0 = tool_l0_texts.get(key) or ""

            # 确定工具组起始消息索引是否在 tail 中
            in_tail = False
            l2_tk = 0
            for msg_idx, entries_map in tool_key_map.items():
                for (ekey, tk) in entries_map:
                    if ekey == key:
                        in_tail = msg_idx >= tail_start
                        l2_tk = tk
                        break
                if l2_tk > 0:
                    break

            sum_tk = self._token_estimate(l1) or self._token_estimate(l0 or "")

            entry = TurnPlanEntry(turn_index=key[0], turn_type="tool",
                                  tool_sub_index=key[1],
                                  l2_tokens=l2_tk, summary_tokens=sum_tk,
                                  tokens_saved=max(0, l2_tk - sum_tk),
                                  budget_remaining=budget)

            if in_tail:
                entry.target_level = "L2"
                entry.decision_reason = "tail"
            elif key in tool_head and key not in upgrades:
                if l1:
                    entry.target_level = "L1"
                    entry.decision_reason = "tool_head"
                else:
                    entry.target_level = "L2"
                    entry.decision_reason = "tool_head"
            elif key in upgrades:
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

        self.store.write_turn_plan(self._session_id, [e.as_dict() for e in entries])
        logger.info("[CA] turn_plan: wrote %d entries for session %s", len(entries), self._session_id)

    # ---------- 辅助方法 ----------
    def _token_estimate(self, text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        if cjk > len(text) * 0.5:
            return int(len(text) * 1.5)
        return max(1, len(text))

    def _is_valid_summary(self, l1_text: str) -> bool:
        if not l1_text or not l1_text.strip():
            return False
        try:
            data = json.loads(l1_text)
            core = data.get("core_change", "")
            return bool(core and core != "无有效增量")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False

    def _compute_tail_start(self, messages):
        """从消息尾部反向累计 token 数，找到 tail 保护区的起始索引。

        tool 响应（role=tool）不单独计 token —— 它们属于工具组，
        其压缩/保留决策由前导 tool_calls 消息统一管理。
        跳过它们可防止末尾大量工具响应"劫持" tail 预算。
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

    def _deduplicate_messages(self, messages):
        seen = set()
        deduped = []
        for msg in messages:
            if msg.get("role") == "system":
                deduped.append(msg)
                continue
            normalized = self._deep_normalize(msg)
            key = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                deduped.append(msg)
        if Config.DEBUG_MODE:
            logger.debug("dedup: before=%d, after=%d", len(messages), len(deduped))
        return deduped

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

    def _call_llm_for_l1(self, prev_l1, l2_text) -> str:
        import urllib.request
        prompt = L1_GENERATION_PROMPT.format(
            previous_summary=json.dumps(prev_l1, ensure_ascii=False) if prev_l1 else "无",
            current_dialog=l2_text)
        req_body = {
            "model": Config.LLM_MODEL,
            "prompt": prompt, "stream": False,
            "options": {"num_predict": Config.LLM_NUM_PREDICT, "temperature": 0.3},
            "keep_alive": -1
        }
        if Config.LLM_THINK is not None:
            req_body["think"] = Config.LLM_THINK
        payload = json.dumps(req_body).encode()
        for attempt in range(Config.LLM_MAX_RETRIES):
            try:
                req = urllib.request.Request(f"{Config.LLM_ENDPOINT}/api/generate", data=payload,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
                if data.get("response"):
                    return data["response"]
            except Exception as e:
                logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        return ("核心摘要：无有效增量\n资源与观察：\n- 无\n事实与约束：\n- 无\n决策与结论：\n- 无\n后续行动：\n- 无")

    def _extract_l0(self, l1_dict):
        core = l1_dict.get("core_change", "")
        return core[:100] if core else "无"

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
                if data.get("core_change") != "无有效增量":
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
        self._shutdown_cache_executor()
        self.cache.cancel_retry_timer()
        builder = CacheBuilder(self.store)
        self.cache = builder.build(self._session_id)
        with self._task_lock:
            self._pending_tasks.clear()
        with self._pre_upgraded_lock:
            self._pre_upgraded_tool_turns.clear()
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
