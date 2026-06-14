"""
ca/lstage.py — L‑stage 异步补全线程 (v5.0 tool_group)

功能：
- 对话轮补全 + 工具轮补全（tool_group 格式，no per-tool 条目）。
- 补全对话轮时自动提取工具调用并回填为 tool_group。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .config import Config
from .post_process import robust_json_parse, clean_increment, parse_v1_markdown_xml
from .tool_summarizer import ToolSummarizer
from . import FctTruncatedException

if TYPE_CHECKING:
    from . import ContextAssembler

logger = logging.getLogger(__name__)


class BackfillThread(threading.Thread):
    def __init__(self, engine: 'ContextAssembler', turn_type: str, rate: float):
        super().__init__(daemon=True, name=f"L-stage-{turn_type}")
        self.engine = engine
        self.turn_type = turn_type
        self.rate = rate
        self.start_event = threading.Event()
        self.stop_event = threading.Event()

    def run(self):
        logger.info("L‑stage %s thread started", self.turn_type)
        while not self.stop_event.is_set():
            self.start_event.wait(60)
            if self.stop_event.is_set():
                break
            try:
                self._do_backfill()
            except Exception as e:
                logger.exception("Unexpected error in L‑stage %s: %s", self.turn_type, e)
        logger.info("L‑stage %s thread stopped", self.turn_type)

    def trigger(self):
        self.start_event.set()

    def shutdown(self):
        self.stop_event.set()
        self.start_event.set()
        self.join(timeout=Config.SHUTDOWN_TIMEOUT)

    def _do_backfill(self):
        session_id = self.engine._session_id
        records = self.engine.store.get_pending_backfill(session_id, self.turn_type)
        if not records:
            return
        count = 0
        for rec in records:
            if self.stop_event.is_set():
                break
            l2_text = rec.get("l2_text")
            if not l2_text:
                continue
            try:
                if self.turn_type == "dialogue":
                    self._backfill_dialogue(rec, l2_text)
                else:
                    self._backfill_tool(rec, l2_text)
                count += 1
                self.engine.stats.tool_backfill_success += 1
            except Exception as e:
                logger.warning("Backfill failed for turn %s-%d: %s", self.turn_type, rec["turn_index"], e)
                self._handle_failure(rec)
                self.engine.stats.tool_backfill_failure += 1
            time.sleep(1.0 / self.rate)
        if count > 0:
            logger.debug("L‑stage %s backfilled %d turns", self.turn_type, count)

    def _backfill_dialogue(self, rec: Dict, l2_text: str):
        """回填对话轮 L1，并提取工具调用回填为 tool_group。"""
        prev_l1 = self._get_prev_l1(rec["turn_index"])
        try:
            response_text, finish_reason = self.engine._call_llm_for_fct(prev_l1, l2_text)
        except FctTruncatedException as e:
            logger.warning("[CA-METRIC] ca.fct.truncated_fallback: turn=%d, finish_reason=truncated, len=%d",
                           rec["turn_index"], len(e.response_text))
            session_id = self.engine._session_id
            turn_index = rec["turn_index"]
            turn_type = rec["turn_type"]
            sub_index = rec.get("tool_sub_index", 0)
            self.engine.store.increment_backfill_attempts(session_id, turn_index, turn_type, sub_index)
            new_attempts = rec.get("backfill_attempts", 0) + 1
            if new_attempts >= 3:
                error_l1 = '{"core_change":"本轮无新内容","_assemble_status":2,"_l_error":true}'
                self.engine.store.write_turn(
                    session_id, turn_index,
                    l0_text="补全失败", l1_text=error_l1,
                    turn_type=turn_type, tool_sub_index=sub_index,
                    l2_text=rec.get("l2_text"), _assemble_status=2,
                )
                self.engine.store.conn.execute(
                    "UPDATE turn_cache SET backfill_attempts=? WHERE session_id=? AND turn_index=? AND turn_type=? AND tool_sub_index=?",
                    (new_attempts, session_id, turn_index, turn_type, sub_index)
                )
                self.engine.store.conn.commit()
                logger.warning("Permanent backfill failure for turn %d (truncated)", turn_index)
            return

        l1_dict, l0_text, core_state = parse_v1_markdown_xml(response_text)
        cleaned = clean_increment(l1_dict)
        if "core_change" not in cleaned:
            cleaned["core_change"] = "本轮无新内容"
        l1_str = json.dumps(cleaned, ensure_ascii=False)
        l0 = self.engine._extract_l0(cleaned)
        try:
            l1_emb = self.engine.embed_client.embed(l1_str)
            # l0_emb 不再使用（2026-06-13，见 graphify 分析报告）
            # l0_emb = self.engine.embed_client.embed(l0)
            l0_emb = None
        except Exception:
            l1_emb = None
            l0_emb = None
        self._update_record(rec, l0, l1_str, l0_emb, l1_emb)

        # 提取工具调用，回填 tool_group
        self._backfill_tool_group(rec, l2_text)

    def _backfill_tool_group(self, rec: Dict, l2_text: str):
        """从 l2_text 中提取工具调用，回填为 tool_group 格式（v5）。"""
        try:
            msgs = json.loads(l2_text)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msgs, list):
            return
        tool_turns = self.engine._extract_tool_calls(msgs)
        if not tool_turns:
            return

        session_id = self.engine._session_id
        turn_idx = rec["turn_index"]
        api_count = 1  # 补全场景不保留原始 api_call_count，统一为 1

        per_tool_summaries = []
        for sub_index, turn in enumerate(tool_turns, start=1):
            tc_call = turn["tool_call"]
            tc_responses = turn["tool_responses"]
            try:
                tool_l1, tool_l0 = self.engine.tool_summarizer.summarize(tc_call, tc_responses)
            except Exception as e:
                logger.warning("ToolSummarizer failed during backfill turn %d-%d: %s",
                               turn_idx, sub_index, e)
                continue

            try:
                l1_emb = self.engine.embed_client.embed(json.dumps(tool_l1, ensure_ascii=False))
                # l0_emb 不再使用（2026-06-13，见 graphify 分析报告）
                # l0_emb = self.engine.embed_client.embed(tool_l0)
                l0_emb = None
            except Exception:
                l1_emb = None
                l0_emb = None

            tool_content = json.dumps([r.get("content", "") for r in tc_responses], ensure_ascii=False)
            tc_id = tc_call.get("id", "")
            tc_name = tc_call.get("function", {}).get("name", "")
            tc_status = tool_l1.get("status", "ok")

            # ① 写 tool 行（v5 格式）
            self.engine.store.write_turn(
                session_id, turn_idx,
                l0_text=tool_l0,
                l1_text=json.dumps(tool_l1, ensure_ascii=False),
                l0_embedding=None,
                l1_embedding=l1_emb,
                api_call_count=api_count, seq_index=sub_index,
                role='tool', content=tool_content,
                tool_call_id=tc_id,
                tool_name=tc_name,
                status=tc_status,
                _assemble_status=0,
            )

            per_tool_summaries.append({
                "tool_name": tc_name,
                "l0": tool_l0,
                "l1": json.dumps(tool_l1, ensure_ascii=False),
                "status": tc_status,
                "result_summary": tool_l1.get("result_summary", tool_content[:80]),
            })

        if not per_tool_summaries:
            logger.warning("No tool summaries generated during backfill turn %d", turn_idx)
            return

        # ② 组摘要：找 assistant 消息（含 tool_calls 的那条）提取 thought
        thought = ""
        for m in msgs:
            if m.get("role") == "assistant" and "tool_calls" in m:
                thought = m.get("content", "") or ""
                break
        tool_results_for_summary = [
            {"tool_name": s["tool_name"], "status": s["status"],
             "result_summary": s["result_summary"]}
            for s in per_tool_summaries
        ]
        group_summary = ToolSummarizer.generate_group_summary(thought, tool_results_for_summary)

        # ③ 组 L0
        group_l0_parts = [s["l0"] for s in per_tool_summaries[:5]]
        group_l0 = " | ".join(group_l0_parts)
        if len(per_tool_summaries) > 5:
            group_l0 += "..."

        # ④ tool_calls_json：从 tool_turns 重建
        tool_defs = [t["tool_call"] for t in tool_turns]
        tool_calls_json = json.dumps(tool_defs, ensure_ascii=False)

        # ⑤ 写 assistant{tc} 行
        self.engine.store.write_turn(
            session_id, turn_idx,
            l0_text=group_l0,
            l1_text=json.dumps(group_summary, ensure_ascii=False),
            l0_embedding=None, l1_embedding=None,
            api_call_count=api_count, seq_index=0,
            role='assistant', content=thought,
            tool_calls_json=tool_calls_json,
            finish_reason='tool_calls',
            _assemble_status=0,
        )

        # ⑥ 更新 cache
        self.engine.cache.add_tool_group(
            turn_idx, api_count,
            group_l0, json.dumps(group_summary, ensure_ascii=False),
        )

    def _backfill_tool(self, rec: Dict, l2_text: str):
        """回填旧 per-tool 记录（legacy 兼容），转为 tool_group 格式。"""
        self._backfill_tool_group(rec, l2_text)

    def _update_record(self, rec, l0, l1, l0_emb, l1_emb):
        """更新对话轮记录（不更新工具轮——已由 _backfill_tool_group 处理）。"""
        session_id = self.engine._session_id
        turn_index = rec["turn_index"]
        self.engine.store.write_turn(
            session_id, turn_index,
            l0_text=l0, l1_text=l1,
            l0_embedding=l0_emb, l1_embedding=l1_emb,
            turn_type="dialogue", tool_sub_index=0,
            l2_text=rec.get("l2_text"), _assemble_status=0,
        )
        self.engine.cache.add_turn(turn_index, l0, l1, l0_emb, l1_emb)

    def _handle_failure(self, rec: Dict):
        session_id = self.engine._session_id
        turn_index = rec["turn_index"]
        turn_type = rec["turn_type"]
        sub_index = rec.get("tool_sub_index", 0)
        new_attempts = rec.get("backfill_attempts", 0) + 1
        if new_attempts >= 3:
            if turn_type == "dialogue":
                error_l1 = '{"core_change":"本轮无新内容","_assemble_status":2,"_l_error":true}'
            else:
                error_l1 = '{"error":"补全失败","result_summary":"无法生成摘要","_assemble_status":2,"_l_error":true}'
            self.engine.store.write_turn(
                session_id, turn_index,
                l0_text="补全失败", l1_text=error_l1,
                turn_type=turn_type, tool_sub_index=sub_index,
                l2_text=rec.get("l2_text"), _assemble_status=2,
            )
            self.engine.store.conn.execute(
                "UPDATE turn_cache SET backfill_attempts=? WHERE session_id=? AND turn_index=? AND turn_type=? AND tool_sub_index=?",
                (new_attempts, session_id, turn_index, turn_type, sub_index)
            )
            self.engine.store.conn.commit()
            logger.warning("Permanent backfill failure for %s turn %d", turn_type, turn_index)
        else:
            self.engine.store.increment_backfill_attempts(session_id, turn_index, turn_type, sub_index)

    def _get_prev_l1(self, current_turn_index: int) -> Optional[Dict]:
        prev_turn = current_turn_index - 1
        if prev_turn < 0:
            return None
        rec = self.engine.store.read_turn(self.engine._session_id, prev_turn)
        if rec and rec.get("l1_text"):
            try:
                data = json.loads(rec["l1_text"])
                if data.get("core_change") != "本轮无新内容":
                    return data
            except Exception:
                pass
        return None

    def _filter_embeddings(self, embeddings: Dict, query_emb: List[float]) -> Dict:
        if not query_emb:
            return embeddings
        valid = {k: v for k, v in embeddings.items() if len(v) == len(query_emb)}
        if len(valid) != len(embeddings):
            logger.warning("Filtered %d embedding(s) with mismatched dimensions",
                           len(embeddings) - len(valid))
        return valid
