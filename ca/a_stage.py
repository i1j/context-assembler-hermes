"""ca/a_stage.py — A-stage 话题注入 (v5.10)

设计决策: C-010 (A-stage 解耦), C-011 (三区模型 → 话题等级替换)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-a-stage-同步上下文组装
  职责：
  - 话题检测 → grade 驱动
  - 三级替换（Elm/Fct/Hdl 注入）
  - 增量缓存 + 尾巴保护

v5.10 迁移：2 个 A-stage 核心方法（_simple_mutation_mode_v5 / _incremental_mutation）
从 CAContextAssemblerPlugin（plugins/ca_assembler/__init__.py）迁入此模块。
Plugin 层通过 engine._simple_mutation_mode_v5(topic_mgr, conv_hist) 委托。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

from .config import Config
from .grade import Grade, TopicGrade
from .post_process import _safe_truncate

logger = logging.getLogger(__name__)


class AStageMixin:
    """A-stage 混合类。由 ContextAssembler 通过多重继承引入。

    新增属性（初始化为 None，由 __init__ 或首次调用前赋值）：
      _A_stable_cache     — 稳定区已替换 Fct 的 conv_hist 片段
      _A_cache_turns      — cache 中的 user 消息数
      _A_cache_is_stale   — Fct pending 标记
      _saved_history_snapshot — A-stage 替换前的原始 conv_hist 快照
    """

    # ── A-stage 缓存状态（由 ContextAssembler.__init__ 初始化） ──
    _A_stable_cache: Optional[List[Dict]] = None
    _A_cache_turns: int = 0
    _A_cache_is_stale: bool = False
    _saved_history_snapshot: Optional[List[Dict]] = None

    # ── 话题等级 → 行等级映射（user/fin 不降级） ──
    _USER_FIN_MAP = {
        TopicGrade.ACT: Grade.ELM,   # 原文保留
        TopicGrade.REL: Grade.FCT,   # 完整摘要
        TopicGrade.FAR: Grade.HDL,   # Hdl[:150]
    }

    # ── 核心 A-stage 装配 ──

    def _simple_mutation_mode_v5(self, topic_mgr: Any, conversation_history: list) -> Optional[str]:
        """v5: 轮内按角色类型匹配注入 Fct。

        topic_mgr: TopicGradeManager 实例（插件层持有，作为参数传入）。
        访问 self.store / self.cache / self.embed_client 等（来自 ContextAssembler）。
        """
        if not conversation_history:
            return None
        self._saved_history_snapshot = [{**m} for m in conversation_history]
        store = self.store
        sid = self._session_id

        from .store import get_turn_ca_rows

        # 尾巴保护：最后 2 个 user turn 不处理
        tail_boundary = 0
        _user_count = 0
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        # Phase 1: 按 turn 收集 conv_hist 中需匹配的行
        turn_rows: dict = {}
        current_turn = -1
        for i, msg in enumerate(conversation_history):
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                current_turn += 1
            if i >= tail_boundary:
                continue

            if role == "user":
                row_type = "user"
            elif role == "assistant" and not msg.get("tool_calls"):
                row_type = "fin"
            elif role == "assistant":
                row_type = "thought"
            elif role == "tool":
                row_type = "tool"
            else:
                continue

            turn_rows.setdefault(current_turn, []).append((i, row_type))

        # Phase 1.5: 每轮只保留最后一个 "fin" 为真 fin
        for _t, _rows in turn_rows.items():
            _fin_positions = [_p for _p, (_ci, _rt) in enumerate(_rows) if _rt == "fin"]
            if len(_fin_positions) > 1:
                for _p in _fin_positions[:-1]:
                    _rows[_p] = (_rows[_p][0], "thought")

        # Phase 2: 逐 turn 做角色队列匹配
        replaced = 0
        skipped = 0

        for turn_num, rows in turn_rows.items():
            if turn_num < 0:
                continue

            # 1. 话题等级
            topic_grade = topic_mgr.get_turn_grade(turn_num + 1) if topic_mgr else TopicGrade.ACT

            # 2. 行等级映射
            thought_grade = Grade.from_topic_grade(topic_grade)
            user_grade = self._USER_FIN_MAP.get(topic_grade)

            # 3. 读 DB
            ca_rows = get_turn_ca_rows(store, sid, turn_num + 1)
            if not ca_rows:
                skipped += sum(1 for _, t in rows)
                continue

            logger.info("[CA_v5_mutate] turn=%d topic_grade=%s thought_grade=%s user_grade=%s",
                       turn_num + 1, topic_grade, thought_grade, user_grade)

            # 4. 按需选列填充队列
            ca_users: list = []
            ca_fins: list = []
            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role, finish_reason, tc_json, elm_text, fct, hdl in ca_rows:
                if role == "user":
                    if user_grade == Grade.FCT and fct:
                        ca_users.append(fct)
                    elif user_grade == Grade.HDL:
                        ca_users.append((hdl or "")[:150])
                    # ELM: 原文在 conv 中，不填队列
                elif role == "assistant" and finish_reason == "stop":
                    if user_grade == Grade.FCT and fct:
                        ca_fins.append(fct)
                    elif user_grade == Grade.HDL:
                        ca_fins.append((hdl or "")[:150])
                elif role == "assistant":
                    if thought_grade == Grade.FCT and fct:
                        ca_thoughts.append(fct)
                    elif thought_grade == Grade.HDL:
                        ca_thoughts.append((hdl or "")[:150])
                elif role == "tool":
                    if thought_grade == Grade.FCT and fct:
                        ca_tools.append(fct)
                    elif thought_grade == Grade.HDL:
                        ca_tools.append((hdl or "")[:150])

            # 5. 替换
            ui, fi, ti, tj = 0, 0, 0, 0
            for conv_idx, row_type in rows:
                if row_type == "user":
                    if user_grade == Grade.ELM:
                        pass
                    elif ui < len(ca_users):
                        conversation_history[conv_idx]["content"] = ca_users[ui]
                        ui += 1
                    replaced += 1
                elif row_type == "fin":
                    if user_grade == Grade.ELM:
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    elif fi < len(ca_fins):
                        conversation_history[conv_idx]["content"] = ca_fins[fi]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        fi += 1
                    else:
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    replaced += 1
                elif row_type == "thought":
                    if thought_grade is None:
                        conversation_history[conv_idx]["content"] = "略"
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    elif ti < len(ca_thoughts):
                        conversation_history[conv_idx]["content"] = ca_thoughts[ti]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        ti += 1
                    else:
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    replaced += 1
                elif row_type == "tool":
                    if thought_grade is None:
                        conversation_history[conv_idx]["content"] = "略"
                    elif tj < len(ca_tools):
                        conversation_history[conv_idx]["content"] = ca_tools[tj]
                        tj += 1
                    replaced += 1

        last_turn = max(turn_rows, default=0)
        logger.info("[CA_v5] simple_mutation: replaced=%d + skipped=%d (tail=%d, turns=%d)",
                    replaced, skipped, tail_boundary, last_turn)

        # Debug dump
        if Config.DEBUG_MODE:
            dump_dir = os.environ.get("CA_DEBUG_DUMP", "")
            if dump_dir:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dump_path = os.path.join(dump_dir, f"ca_mutation_{sid}_{ts}.json")
                try:
                    os.makedirs(dump_dir, exist_ok=True)
                    with open(dump_path, "w", encoding="utf-8") as f:
                        json.dump(conversation_history, f, ensure_ascii=False, indent=2)
                    logger.info("[CA_v5] mutation dump written: %s (%d msgs, %d KB)",
                                dump_path, len(conversation_history),
                                os.path.getsize(dump_path) // 1024)
                except Exception as e:
                    logger.warning("[CA_v5] failed to write mutation dump: %s", e)

        # 写缓存
        self._A_stable_cache = [{**m} for m in conversation_history[0:tail_boundary]]
        self._A_cache_turns = sum(1 for m in self._A_stable_cache if m.get("role") == "user")
        self._A_cache_is_stale = False
        logger.debug("[CA_v5] full_mutation: cache written, stable_turns=%d", self._A_cache_turns)

        return None

    # 别名
    _full_mutation = _simple_mutation_mode_v5

    def _incremental_mutation(self, topic_mgr: Any, conversation_history: list) -> Optional[str]:
        """增量路径：缓存稳定区 → delta 替换 → 尾部保留。"""
        if not conversation_history:
            return None

        self._saved_history_snapshot = [{**m} for m in conversation_history]
        store = self.store
        sid = self._session_id
        cache = self._A_stable_cache
        if cache is None:
            return self._full_mutation(topic_mgr, conversation_history)

        from .store import get_turn_ca_rows

        # Step 1: 尾部边界
        tail_boundary = 0
        _user_count = 0
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        # Step 2: 并行扫描缓存 → 覆盖稳定区
        cache_idx = 0
        need_fallback = False
        break_point = 0
        for i, msg in enumerate(conversation_history):
            if cache_idx >= len(cache):
                break_point = i
                break
            role = msg.get("role", "")
            c_msg = cache[cache_idx]
            if role == c_msg.get("role"):
                msg["content"] = c_msg.get("content", "")
                msg.pop("reasoning_content", None)
                msg.pop("tool_calls", None)
                cache_idx += 1
            elif role == "system":
                continue
            else:
                need_fallback = True
                break

        if need_fallback:
            logger.info("[CA_v5] cache alignment mismatch, falling back to full mutation")
            self._A_stable_cache = None
            self._A_cache_turns = 0
            return self._full_mutation(topic_mgr, conversation_history)

        # Step 3: Delta 区
        turn_rows: dict = {}
        current_turn = self._A_cache_turns - 1
        for idx in range(break_point, len(conversation_history)):
            msg = conversation_history[idx]
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                current_turn += 1
                continue
            if idx >= tail_boundary:
                continue

            if role == "assistant":
                next_msg = conversation_history[idx + 1] if idx + 1 < len(conversation_history) else None
                if next_msg and next_msg.get("role") == "tool":
                    row_type = "thought"
                else:
                    row_type = "fin"
            elif role == "tool":
                row_type = "tool"
            else:
                continue
            turn_rows.setdefault(current_turn, []).append((idx, row_type))

        # Step 4: Delta 替换
        replaced = 0
        skipped = 0
        turn_pending = False

        for turn_num, rows in turn_rows.items():
            if turn_num < 0:
                continue
            ca_rows = get_turn_ca_rows(store, sid, turn_num + 1)
            if not ca_rows:
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            user_fct = next((r[5] for r in ca_rows if r[1] == "user"), None)
            if not user_fct:
                turn_pending = True
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            topic_grade = topic_mgr.get_turn_grade(turn_num + 1) if topic_mgr else TopicGrade.ACT
            thought_grade = Grade.from_topic_grade(topic_grade)

            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role_, finish_reason, tc_json, elm_text, fct, hdl in ca_rows:
                if role_ == "user" or (role_ == "assistant" and finish_reason == "stop"):
                    continue
                if role_ == "assistant":
                    if thought_grade == Grade.FCT and fct:
                        ca_thoughts.append(fct)
                    elif thought_grade == Grade.HDL:
                        ca_thoughts.append((hdl or "")[:150]) if hdl else None
                elif role_ == "tool":
                    if thought_grade == Grade.FCT and fct:
                        ca_tools.append(fct)
                    elif thought_grade == Grade.HDL:
                        ca_tools.append((hdl or "")[:150]) if hdl else None

            ti, tj = 0, 0
            for conv_idx, row_type in rows:
                if row_type == "fin":
                    continue
                if row_type == "thought":
                    if ti < len(ca_thoughts):
                        conversation_history[conv_idx]["content"] = ca_thoughts[ti]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        replaced += 1
                        ti += 1
                    else:
                        skipped += 1
                else:  # tool
                    if tj < len(ca_tools):
                        conversation_history[conv_idx]["content"] = ca_tools[tj]
                        replaced += 1
                        tj += 1
                    else:
                        skipped += 1

        if turn_pending:
            self._A_cache_is_stale = True

        logger.info("[CA_v5] incremental_mutation: replaced=%d + skipped=%d (tail=%d, delta_turns=%d, pending=%s)",
                    replaced, skipped, tail_boundary, len(turn_rows), turn_pending)

        # Step 5: 写缓存
        self._A_stable_cache = [{**m} for m in conversation_history[0:tail_boundary]]
        self._A_cache_turns = sum(1 for m in self._A_stable_cache if m.get("role") == "user")
        if not turn_pending:
            self._A_cache_is_stale = False
        logger.debug("[CA_v5] incremental_mutation: cache updated, stable_turns=%d", self._A_cache_turns)

        if Config.DEBUG_MODE:
            dump_dir = os.environ.get("CA_DEBUG_DUMP", "")
            if dump_dir:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dump_path = os.path.join(dump_dir, f"ca_incr_mutation_{sid}_{ts}.json")
                try:
                    os.makedirs(dump_dir, exist_ok=True)
                    with open(dump_path, "w", encoding="utf-8") as f:
                        json.dump(conversation_history, f, ensure_ascii=False, indent=2)
                    logger.info("[CA_v5] incr mutation dump written: %s (%d msgs, %d KB)",
                                dump_path, len(conversation_history),
                                os.path.getsize(dump_path) // 1024)
                except Exception as e:
                    logger.warning("[CA_v5] failed to write incr mutation dump: %s", e)

        return None

    @staticmethod
    def _estimate_conv_tokens(conv_hist: list) -> int:
        """字符粗估 conv_hist 总 token（含 tool/assistant/reasoning 全部内容）。"""
        total_chars = 0
        for msg in conv_hist:
            total_chars += len(msg.get("content", "") or "")
            for tc in msg.get("tool_calls", []) or []:
                total_chars += len(tc.get("function", {}).get("name", "") or "")
                total_chars += len(str(tc.get("function", {}).get("arguments", "")) or "")
            total_chars += len(msg.get("reasoning_content", "") or "")
        return total_chars // 4  # chars → token 粗估
