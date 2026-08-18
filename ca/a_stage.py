"""ca/a_stage.py — A-stage 方向 B: 从 turn_stream DB 重建 conv_history (v6)

方向 B 取代了旧的方向 A 原地 mutation（_simple_mutation_mode_v5 / _incremental_mutation），
已于 2026-06-28 CE 管线停用时清理。

viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-a-stage-同步上下文组装
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

from .config import Config
from .grade import Grade, TopicGrade
from .post_process import _safe_truncate

logger = logging.getLogger(__name__)


def _merge_same_request_assistant_rows(rows: List[Dict]) -> List[Dict]:
    """合并同一 request_id 的相邻 assistant 行（决策44 E-stage 拆块配套）。

    决策44 E-stage v7 把同一次 API 响应拆成多行写入 turn_stream：
      - THINKING 行（reasoning + tool_calls，block_type=thinking）
      - AGENT_REPLY 行（content，block_type=agent_reply，同一 request_id）
    若 A-stage 逐行输出，会产生
      assistant(tool_calls) → assistant(无 tool_calls) → tool×N
    非法序列 —— OpenAI/DeepSeek 拒绝："An assistant message with 'tool_calls'
    must be followed by tool messages responding to each 'tool_call_id'"
    （2026-08-17 线上 400；Hermes pre-call sanitizer 只修配对不修顺序）。

    合并规则（同一 turn 内相邻 assistant 行）：
      - request_id 非空且相同 → 合并
      - request_id 均为空（旧数据/测试行）→ 合并（相邻 assistant 行只可能
        来自同一响应的拆块；v5 时代每次响应只写一行，不会相邻）
      - 其余（request_id 一个空一个非空 / 不同 request）→ 不合并

    合并结果：Elm/Fct/Hdl 文本拼接（保留 reasoning + content 双块信息），
    tool_calls_json 取非空者，finish_reason/block_type 取首行。
    """
    if len(rows) < 2:
        return rows
    merged: List[Dict] = []
    for row in rows:
        if (merged
                and merged[-1].get("role") == "assistant"
                and row.get("role") == "assistant"):
            prev = merged[-1]
            r1 = prev.get("request_id") or ""
            r2 = row.get("request_id") or ""
            same_req = (r1 and r2 and r1 == r2) or (not r1 and not r2)
            if same_req:
                for key in ("Elm", "Fct", "Hdl"):
                    a = prev.get(key) or ""
                    b = row.get(key) or ""
                    prev[key] = (a + "\n\n" + b).strip() if a and b else (a or b)
                if not prev.get("tool_calls_json") and row.get("tool_calls_json"):
                    prev["tool_calls_json"] = row["tool_calls_json"]
                # finish_reason / block_type / ooda_stage 取首行（保持 tool_calls 判定）
                continue
        merged.append(dict(row))
    return merged


def _normalize_arguments_str(arguments: Any) -> str:
    """将 tool_calls[].function.arguments 归一化为 JSON string。

    兼容历史 DB 行（arguments 为 dict 等非 str 值）：
      - 已有合法 JSON str → 原样保留
      - 空串或非法 JSON str → "{}"
      - None → "{}"
      - 其他非 str → json.dumps(arg, ensure_ascii=False)
    """
    if isinstance(arguments, str):
        # 合法 JSON string → 原样保留；空串 / 非法 JSON string → "{}"
        if not arguments.strip():
            return "{}"
        try:
            json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return "{}"
        return arguments
    if arguments is None:
        return "{}"
    try:
        # allow_nan=False：NaN/Infinity 不是合法 JSON，归一化为 "{}" 而非写入
        # 非严格 JSON 串，避免 strict provider 拒收。
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        # 不可 JSON 序列化的历史/provider 值（object/set/bytes/循环引用）→ "{}"
        return "{}"


class AStageMixin:
    """A-stage 混合类（方向 B）。由 ContextAssembler 通过多重继承引入。

    仅保留 v6 方向 B 的 _build_conv_history_v6 方法族。
    旧方向 A 的 _simple_mutation_mode_v5 / _incremental_mutation / 增量缓存属性已于 2026-06-28 清理。
    """

    # ── 话题等级 → 行等级映射（user/fin 不降级） ──
    _USER_FIN_MAP = {
        TopicGrade.ACT: Grade.ELM,   # 原文保留
        TopicGrade.REL: Grade.FCT,   # 完整摘要
        TopicGrade.FAR: Grade.HDL,   # Hdl[:150]
    }

    # ── 方向 B: 从 turn_stream DB 构造 conv_history ──

    _THOUGHT_TOOL_MAP = {
        TopicGrade.ACT: Grade.FCT,   # 工具/思维 → Fct
        TopicGrade.REL: Grade.HDL,   # 工具/思维 → Hdl
        # FAR: 不保留 thought/tool 行（完全删除）
    }

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

        # 2. 按 turn 分组（过滤掉 bg 行）
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
            rows = _merge_same_request_assistant_rows(turns[turn_num])
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

        # ── debug dump（v6.2: 受 DEBUG_MODE 控制，避免无条件写 /tmp）──
        try:
            sid = getattr(self, '_session_id', '') or ''
            if sid and Config.DEBUG_MODE:
                import os, time
                _dump = {
                    "session_id": sid,
                    "timestamp": time.time(),
                    "tail_protected_turns": sorted(tail_set),
                    "n_turns": len(turns),
                    "n_messages": len(conv_hist),
                    "topic_grades": {
                        str(tn): str(topic_mgr.get_turn_grade(tn))
                        for tn in sorted(turns)
                    } if topic_mgr else {},
                    "messages": conv_hist,
                }
                _ts = time.strftime("%Y%m%d_%H%M%S")
                _path = f"/tmp/ca_conv_hist_{sid}_{_ts}.json"
                with open(_path, "w") as _f:
                    json.dump(_dump, _f, ensure_ascii=False, indent=2)
                logger.debug("[CA_build] debug dump written: %s (%d msgs)", _path, len(conv_hist))
        except Exception as exc:
            logger.warning("[CA_build] debug dump failed: %s", exc)

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
        elif role == "assistant":
            if finish_reason == "stop":
                row_type = "fin"
            else:
                row_type = "thought"
        elif role == "tool":
            row_type = "tool"
        else:
            return None

        # ── 内容等级选择 ──
        if in_tail:
            # 尾部保护区：全 Elm
            target_grade = Grade.ELM
        elif row_type in ("user", "fin"):
            target_grade = self._USER_FIN_MAP.get(topic_grade, Grade.ELM)
        else:  # thought / tool
            tr = self._THOUGHT_TOOL_MAP.get(topic_grade)
            if tr is None:
                return None  # FAR → 删除 thought/tool 行
            target_grade = tr

        content = self._select_content(row, target_grade)
        msg: Dict[str, Any] = {
            "role": role,
            "content": content,
        }

        # ── tool 专用字段 ──
        if role == "tool":
            tool_call_id = row.get("tool_call_id")
            tool_name = row.get("tool_name")
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["tool_name"] = tool_name

        # ── assistant thought 保留 tool_calls（arguments 归一化为 JSON string）──
        if role == "assistant" and finish_reason != "stop":
            tc_json = row.get("tool_calls_json")
            if tc_json:
                try:
                    tool_calls = json.loads(tc_json)
                except (json.JSONDecodeError, TypeError):
                    pass
                else:
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if isinstance(tc, dict):
                                fn = tc.get("function")
                                # function 为 dict 时无条件补齐 arguments 键，
                                # 缺键/None → "{}"，防止下游 _canonicalize_api_tool_calls
                                # 两次访问 tc["function"]["arguments"] 抛 KeyError（BUG-2）
                                if isinstance(fn, dict):
                                    fn["arguments"] = _normalize_arguments_str(
                                        fn.get("arguments"))
                        msg["tool_calls"] = tool_calls
                    # 非 list（dict/str 等脏数据）→ 不设置 tool_calls，避免输出
                    # OpenAI schema 非法消息；宿主 orphan 清理会兜底。

        # thought 行不携带 finish_reason（Hermes 正常路径上线前会 pop，
        # select_context 替换结果不再经过该 pop，strict provider 会拒绝 schema 外字段）
        return msg

    @staticmethod
    def _select_content(row: Dict, target_grade: Grade) -> str:
        """按选定等级从行数据提取内容文本。"""
        if target_grade == Grade.ELM:
            return row.get("Elm", "") or row.get("elm_text", "") or ""

        elif target_grade == Grade.FCT:
            fct_raw = row.get("Fct", "") or row.get("fct_text", "") or ""
            try:
                fct_dict = json.loads(fct_raw)
                if isinstance(fct_dict, dict):
                    # Tool Fct 空字段过滤（v6.0.3）
                    if row.get("role") == "tool":
                        cleaned = {
                            k: v for k, v in fct_dict.items()
                            if v is not None and v != [] and v != "" and v != 0
                        }
                        if cleaned:
                            return _format_fct_readable(cleaned)
                        return fct_raw
                    # 非 tool 行：Fct 渲染为可读格式
                    return _format_fct_readable(fct_dict)
                return fct_raw
            except (json.JSONDecodeError, TypeError):
                return fct_raw

        elif target_grade == Grade.HDL:
            hdl_raw = row.get("Hdl", "") or row.get("hdl_text", "") or ""
            return _safe_truncate(hdl_raw, max_len=150) or "略"

        return ""

    # 别名（兼容旧引用）
    _full_mutation = None  # 方向 A 已清理


def _format_fct_readable(fct_dict: dict) -> str:
    """将 Fct dict 渲染为可读文本，按 OODA 四段分组。

    多事务 Fct（决策 45）：按事务 hdl + OODA 阶段渲染，
    OODA 阶段项即变更记录，无【已完成】等状态标签。

    旧单事务格式:
        # 现象与问题
        - 发现连接池耗尽

        # 决策与方案
        - [已实施] 连接池扩容

        # 后续行动
        - [评估中] 监控超时命中率
    """
    affairs = fct_dict.get("affairs")
    if isinstance(affairs, list) and affairs:
        readable_lines = []
        for idx, affair in enumerate(affairs, start=1):
            if not isinstance(affair, dict):
                continue
            hdl = str(affair.get("hdl") or f"事务{idx}").strip()
            readable_lines.append(f"# 事务{idx} {hdl}")
            ooda = affair.get("ooda") if isinstance(affair.get("ooda"), dict) else {}
            for label in ("现象与问题", "背景与约束", "决策与方案", "后续行动"):
                items = ooda.get(label) or []
                for item in items:
                    item_text = str(item).strip()
                    if item_text:
                        readable_lines.append(f"- {label}：{item_text}")
        if readable_lines:
            return "\n".join(readable_lines)

    sections = {
        "现象与问题": [],
        "背景与约束": [],
        "决策与方案": [],
        "后续行动": [],
    }
    uncategorized = []

    changes = fct_dict.get("changes", [])
    for c in changes:
        if not isinstance(c, dict):
            continue
        core = (c.get("core_change") or c.get("change", "")).strip()
        if not core:
            continue
        stag = c.get("stage_tag", "").strip()
        ooda = c.get("ooda", "").strip()
        if stag and stag != "待分类":
            line = f"- [{stag}] {core}"
        else:
            line = f"- {core}"

        if ooda in sections:
            sections[ooda].append(line)
        else:
            uncategorized.append(line)

    lines = []
    for sec_name in ["现象与问题", "背景与约束", "决策与方案", "后续行动"]:
        items = sections[sec_name]
        if items:
            if lines:
                lines.append("")
            lines.append(f"# {sec_name}")
            lines.extend(items)
    if uncategorized:
        if lines:
            lines.append("")
        lines.append("# 其他")
        lines.extend(uncategorized)

    # Fallback: 无 sections 归类时用 core_change 兜底
    if not lines:
        core = fct_dict.get("core_change", "").strip()
        if core and core != "本轮无新内容":
            lines.append(core)

    return "\n".join(lines) if lines else json.dumps(fct_dict, ensure_ascii=False)
