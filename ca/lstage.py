"""
ca/lstage.py — L‑stage 异步补全线程 (v4.4.0 alpha)

功能：
- 对话轮与工具轮各自独立补全。
- 补全对话轮时自动拆分工具调用为独立工具轮。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .config import Config
from .post_process import robust_json_parse, clean_increment

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
        prev_l1 = self._get_prev_l1(rec["turn_index"])
        ooda_text = self.engine._call_llm_for_l1(prev_l1, l2_text)
        parsed = self.engine.ooda_parser.parse(ooda_text, previous_summary=prev_l1)
        robust, _ = robust_json_parse(json.dumps(parsed, ensure_ascii=False))
        cleaned = clean_increment(robust)
        if "core_change" not in cleaned:
            cleaned["core_change"] = "无有效增量"
        l1_str = json.dumps(cleaned, ensure_ascii=False)
        l0 = cleaned.get("core_change", "")[:100]
        try:
            l1_emb = self.engine.embed_client.embed(l1_str)
            l0_emb = self.engine.embed_client.embed(l0)
        except Exception:
            l1_emb = None
            l0_emb = None
        self._update_record(rec, l0, l1_str, l0_emb, l1_emb)

        # 拆分为工具轮
        try:
            msgs = json.loads(l2_text)
        except json.JSONDecodeError:
            return
        if not isinstance(msgs, list):
            return
        tool_turns = self.engine._extract_tool_calls(msgs)
        for sub_index, turn in enumerate(tool_turns, start=1):
            try:
                tool_l1, tool_l0 = self.engine.tool_summarizer.summarize(
                    turn["tool_call"], turn["tool_responses"]
                )
                try:
                    tool_l1_emb = self.engine.embed_client.embed(json.dumps(tool_l1, ensure_ascii=False))
                    tool_l0_emb = self.engine.embed_client.embed(tool_l0)
                except Exception:
                    tool_l1_emb = None
                    tool_l0_emb = None
                self.engine.store.write_turn(
                    self.engine._session_id, rec["turn_index"],
                    l0_text=tool_l0,
                    l1_text=json.dumps(tool_l1, ensure_ascii=False),
                    l0_embedding=tool_l0_emb,
                    l1_embedding=tool_l1_emb,
                    turn_type='tool', tool_sub_index=sub_index,
                    l2_text=json.dumps(turn["l2_messages"], ensure_ascii=False),
                    _assemble_status=0
                )
                self.engine.cache.add_tool_turn(
                    rec["turn_index"], sub_index,
                    tool_l0, json.dumps(tool_l1, ensure_ascii=False),
                    tool_l0_emb, tool_l1_emb
                )
                self.engine.stats.tool_backfill_success += 1
            except Exception as e:
                logger.warning("L‑stage tool backfill failed for turn %d-%d: %s",
                               rec["turn_index"], sub_index, e)
                self.engine.stats.tool_backfill_failure += 1

    def _backfill_tool(self, rec: Dict, l2_text: str):
        try:
            msgs = json.loads(l2_text)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("L2 data is not valid JSON")
        if not msgs or not isinstance(msgs, list):
            raise ValueError("L2 data is not a non-empty list")
        tool_turns = self.engine._extract_tool_calls(msgs)
        if not tool_turns:
            raise ValueError("No tool calls found in L2 data")
        for sub_index, turn in enumerate(tool_turns, start=1):
            tool_l1, tool_l0 = self.engine.tool_summarizer.summarize(
                turn["tool_call"], turn["tool_responses"]
            )
            try:
                tool_l1_emb = self.engine.embed_client.embed(json.dumps(tool_l1, ensure_ascii=False))
                tool_l0_emb = self.engine.embed_client.embed(tool_l0)
            except Exception:
                tool_l1_emb = None
                tool_l0_emb = None
            self.engine.store.write_turn(
                self.engine._session_id, rec["turn_index"],
                l0_text=tool_l0,
                l1_text=json.dumps(tool_l1, ensure_ascii=False),
                l0_embedding=tool_l0_emb,
                l1_embedding=tool_l1_emb,
                turn_type='tool', tool_sub_index=sub_index,
                l2_text=json.dumps(turn["l2_messages"], ensure_ascii=False),
                _assemble_status=0
            )
            self.engine.cache.add_tool_turn(
                rec["turn_index"], sub_index,
                tool_l0, json.dumps(tool_l1, ensure_ascii=False),
                tool_l0_emb, tool_l1_emb
            )

    def _update_record(self, rec, l0, l1, l0_emb, l1_emb):
        session_id = self.engine._session_id
        turn_index = rec["turn_index"]
        turn_type = rec["turn_type"]
        sub_index = rec.get("tool_sub_index", 0)
        self.engine.store.write_turn(
            session_id, turn_index,
            l0_text=l0, l1_text=l1,
            l0_embedding=l0_emb, l1_embedding=l1_emb,
            turn_type=turn_type, tool_sub_index=sub_index,
            l2_text=rec.get("l2_text"), _assemble_status=0,
        )
        if turn_type == "dialogue":
            self.engine.cache.add_turn(turn_index, l0, l1, l0_emb, l1_emb)
        else:
            key = (turn_index, sub_index)
            self.engine.cache.add_tool_turn(turn_index, sub_index, l0, l1, l0_emb, l1_emb)

    def _handle_failure(self, rec: Dict):
        session_id = self.engine._session_id
        turn_index = rec["turn_index"]
        turn_type = rec["turn_type"]
        sub_index = rec.get("tool_sub_index", 0)
        new_attempts = rec.get("backfill_attempts", 0) + 1
        if new_attempts >= 3:
            if turn_type == "dialogue":
                error_l1 = '{"core_change":"无有效增量","_assemble_status":2,"_l_error":true}'
            else:
                error_l1 = '{"error":"补全失败","result_summary":"无法生成摘要","_assemble_status":2,"_l_error":true}'
            self.engine.store.write_turn(
                session_id, turn_index,
                l0_text="补全失败", l1_text=error_l1,
                turn_type=turn_type, tool_sub_index=sub_index,
                l2_text=rec.get("l2_text"), _assemble_status=2,
                backfill_attempts=new_attempts,
            )
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
                if data.get("core_change") != "无有效增量":
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
