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
            elif role == "assistant":
                # A-stage 已 pop 所有 thought 行的 tool_calls，无法用 msg.get("tool_calls") 区分
                # thought/fin。改为检查下一条消息是否为 tool 行。
                next_msg = conversation_history[i + 1] if i + 1 < len(conversation_history) else None
                if next_msg and next_msg.get("role") == "tool":
                    row_type = "thought"
                else:
                    row_type = "fin"
            elif role == "tool":
                row_type = "tool"
            else:
                continue

            turn_rows.setdefault(current_turn, []).append((i, row_type))
        # 边界保护：没有 user 时 current_turn 为 -1，
        # 防止 Phase 2 中 turn_num < 0 的 skip 导致行泄漏（永远不会被替换）
        if -1 in turn_rows:
            logger.warning("[CA_v5] Phase 1: %d rows assigned to turn=-1 (no user msg before), removing from replace plan",
                           len(turn_rows[-1]))
            del turn_rows[-1]

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

    # ── 方向 B: 从 turn_stream DB 构造 conv_history ──

    _THOUGHT_TOOL_MAP = {
        TopicGrade.ACT: Grade.FCT,   # 工具/思维 → Fct
        TopicGrade.REL: Grade.HDL,   # 工具/思维 → Hdl
        # FAR: 不保留 thought/tool 行（完全删除）
    }

    def _build_conv_history_v6(
        self,
        topic_mgr: Any,
        system_message: Optional[Dict] = None,
    ) -> List[Dict]:
        """v6：从 turn_stream DB 构造 conv_history（方向 B）。替代 mutation 逻辑。

        尾部保护区（最后 2 个 user turn）全 Elm，
        保护区外按话题级别 + 行类型定级：

            ┌──────────┬──────────┬─────────────┐
            │ 位置     │ user/fin │ thought/tool │
            ├──────────┼──────────┼─────────────┤
            │ 尾部     │ Elm      │ Elm         │
            │ ACT 外   │ Elm      │ Fct         │
            │ REL 外   │ Fct      │ Hdl         │
            │ FAR 外   │ Hdl      │ 删除        │
            └──────────┴──────────┴─────────────┘

        FAR 的 thought/tool 行不保留（完全删除）。
        user/fin 行保留 Hdl。

        参数：
            topic_mgr    — TopicGradeManager 实例
            system_message — 可选的 system 消息（从 Hermes conversation_history[0] 保留）
        返回：
            OpenAI 格式消息列表（含可能的 system 首条）
        """
        from .store import read_turn_stream_all

        store = self.store
        sid = self._session_id

        # 1. 读全量 turn_stream
        all_rows = read_turn_stream_all(store, sid)
        if not all_rows:
            return [system_message] if system_message else []

        # 2. 按 turn 分组（过滤掉 bg 行——biz_category='bg_review' 不应存在因 bg 已跳过，
        #    但作为保险防御，显式过滤以防 DB 污染或残留数据）
        turns: Dict[int, List[Dict]] = {}
        for row in all_rows:
            if row.get("biz_category") == "bg_review":
                continue
            t = row["turn"]
            turns.setdefault(t, []).append(row)

        # 3. 尾部保护区：最后 2 个 user turn（1-indexed）
        user_turns = sorted([t for t in turns.keys() if any(
            r["role"] == "user" for r in turns[t]
        )])
        tail_set: Set[int] = set()
        if len(user_turns) >= 2:
            tail_set = set(user_turns[-2:])
        elif len(user_turns) == 1:
            tail_set = set(user_turns)

        # 4. 逐 turn 构造消息
        conv_hist: List[Dict] = []
        if system_message:
            conv_hist.append(dict(system_message))

        for turn_num in sorted(turns.keys()):
            rows = turns[turn_num]
            in_tail = turn_num in tail_set

            # 话题等级（turn_num 是 1-indexed，与 topic_mgr 一致）
            topic_grade = (
                topic_mgr.get_turn_grade(turn_num)
                if topic_mgr else TopicGrade.ACT
            )

            logger.debug(
                "[CA_build] turn=%d grade=%s in_tail=%s n_rows=%d",
                turn_num, topic_grade, in_tail, len(rows),
            )

            for row in rows:
                msg = self._row_to_message(
                    row, topic_grade, in_tail,
                )
                if msg is not None:
                    conv_hist.append(msg)

        return conv_hist

    def _row_to_message(
        self,
        row: Dict,
        topic_grade: TopicGrade,
        in_tail: bool,
    ) -> Optional[Dict]:
        """将一条 turn_stream 行转换为 OpenAI 格式消息。"""
        role = row["role"]
        finish_reason = row.get("finish_reason", "")

        # ── 行类型判定 ──
        if role == "system":
            return None  # system 不应在 turn_stream 中
        elif role == "user":
            row_type = "user"
        elif role == "assistant" and finish_reason == "stop":
            row_type = "fin"
        elif role == "assistant":
            row_type = "thought"
        elif role == "tool":
            row_type = "tool"
        else:
            logger.debug("[CA_build] unknown role=%s, skipping", role)
            return None

        # ── 定级 ──
        if in_tail:
            # 尾部保护区：全 Elm（原始数据）
            target_grade = Grade.ELM
        elif row_type in ("user", "fin"):
            # user/fin：话题级别决定基数
            target_grade = self._USER_FIN_MAP.get(topic_grade, Grade.ELM)
        else:
            # thought/tool：基数降一级；FAR 无映射 → 删除整行
            target_grade = self._THOUGHT_TOOL_MAP.get(topic_grade)

        # FAR thought/tool → 完全删除
        if target_grade is None and row_type in ("thought", "tool"):
            logger.debug(
                "[CA_build]  turn=%d seq=%d role=%s type=%s → SKIP (FAR)",
                row["turn"], row["seq"], role, row_type,
            )
            return None

        logger.debug(
            "[CA_build]  turn=%d seq=%d role=%s type=%s grade=%s",
            row["turn"], row["seq"], role, row_type, target_grade,
        )

        # ── 按 grade 取 content ──
        content = self._select_content(row, target_grade)

        # ── 构造消息 dict ──
        if row_type == "user":
            return {"role": "user", "content": content}

        elif row_type == "fin":
            return {"role": "assistant", "content": content}

        elif row_type == "thought":
            msg: Dict = {"role": "assistant", "content": content}
            # 保留 tool_calls（如有）
            tc_json = row.get("tool_calls_json", "")
            if tc_json:
                try:
                    msg["tool_calls"] = json.loads(tc_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            return msg

        elif row_type == "tool":
            return {
                "role": "tool",
                "content": content,
                "tool_call_id": row.get("tool_call_id", ""),
                "name": row.get("tool_name", ""),
            }

        return None

    def _select_content(self, row: Dict, target_grade: Optional[Grade]) -> str:
        """按目标等级从行数据中选取 content 字符串。"""
        if target_grade is None:
            # FAR thought/tool 触底 → "略"
            return "略"

        if target_grade == Grade.ELM:
            return row.get("Elm", "") or ""

        if target_grade == Grade.FCT:
            fct = row.get("Fct", "") or ""
            return fct if fct else (row.get("Elm", "") or "")

        if target_grade == Grade.HDL:
            hdl = row.get("Hdl", "") or ""
            return (hdl[:150]) if hdl else "略"

        return row.get("Elm", "") or ""
