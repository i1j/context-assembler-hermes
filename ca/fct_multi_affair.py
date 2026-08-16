"""ca/fct_multi_affair.py — Fct 多事务 OODA 输出解析 + think 卡输入筛选（决策 44 续 / 45）。

职责：
1. 解析 F-stage 4B 的 `affairs[]` JSON 输出。
   - v3（当前写入契约）：每事务 `{hdl, turns, ooda}`；OODA 四段数组项
     **就是该阶段的变更记录**，不再有 changes/stage_tag（无【已完成】等标签）。
   - v2（旧库只读兼容）：`{hdl, turns, ooda, changes[{stage_tag, core_change}]}`，
     解析时保留 changes 供旧渲染路径展示。
2. `build_fct_multi_affair` 产出单一数据源 Fct JSON（仅 affairs +
   装配元数据），不再写 legacy 扁平字段；旧消费者的扁平视图由读路径现场派生。
3. 代码筛选当前事务的 think 卡（orient 优先 → decision），截断 + 总预算，
   拼入 Fct 输入，不把全部思考原文灌给 4B。

边界：不改变 strand_summaries.ooda_json 四段契约（决策 28/35）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .post_process import MEANINGLESS_CORE, VALID_STATES

FCT_OODA_KEYS = ("现象与问题", "背景与约束", "决策与方案", "后续行动")
FCT_FORMAT_V2 = "v2-multi-affair"          # 旧库只读兼容
FCT_FORMAT_V3 = "v3-multi-affair-ooda"     # 当前写入契约（决策 45）

# think 卡输入筛选默认预算（代码过滤，宁缺勿滥）
THINK_CARD_KIND_PRIORITY = {"orient": 0, "decision": 1}
DEFAULT_MAX_THINK_CARDS = 2
DEFAULT_MAX_CHARS_PER_CARD = 800
DEFAULT_TOTAL_THINK_BUDGET = 1600


def _strip_json_fence(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fence:
        s = fence.group(1).strip()
    return s


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    s = _strip_json_fence(text)
    if not s:
        return None
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value
                if item is not None and str(item).strip()]
    return []


def _normalize_changes(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage_tag") or "").strip()
        core = str(item.get("core_change") or "").strip()
        if stage in VALID_STATES and core and core not in MEANINGLESS_CORE:
            out.append({"stage_tag": stage, "core_change": core})
    return out


def parse_fct_multi_affair(text: str) -> Optional[Dict[str, Any]]:
    """解析 `{"affairs": [...]}` 输出；无合法 affairs → None（走 legacy 解析）。"""
    obj = _parse_json_object(text)
    if not obj:
        return None
    raw_affairs = obj.get("affairs")
    if not isinstance(raw_affairs, list) or not raw_affairs:
        return None

    affairs: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_affairs, start=1):
        if not isinstance(item, dict):
            continue
        raw_ooda = item.get("ooda")
        if not isinstance(raw_ooda, dict):
            raw_ooda = {}
        ooda = {key: _normalize_str_list(raw_ooda.get(key)) for key in FCT_OODA_KEYS}
        turns = item.get("turns")
        if not isinstance(turns, list):
            turns = []
        turns = [int(t) for t in turns if isinstance(t, int) or str(t).isdigit()]
        hdl = str(item.get("hdl") or "").strip()
        affair: Dict[str, Any] = {
            "hdl": hdl,
            "turns": turns,
            "ooda": ooda,
            "_idx": idx,
        }
        # v2 旧库兼容：保留显式 changes；v3 新契约不再输出该字段
        legacy_changes = _normalize_changes(item.get("changes"))
        if legacy_changes:
            affair["changes"] = legacy_changes
        affairs.append(affair)
    if not affairs:
        return None
    return {"affairs": affairs}


def _fallback_hdl(affair: Dict[str, Any], idx: int) -> str:
    hdl = (affair.get("hdl") or "").strip()
    if hdl:
        return hdl[:30]
    if affair.get("changes"):
        return affair["changes"][0]["core_change"][:30]
    for key in ("现象与问题", "决策与方案", "后续行动", "背景与约束"):
        items = affair.get("ooda", {}).get(key) or []
        if items:
            return items[0][:30]
    return f"事务{idx}"


def build_fct_multi_affair(
    affairs: List[Dict[str, Any]],
    assemble_status: Any,
) -> Dict[str, Any]:
    """affairs[] → 单一数据源 Fct JSON（决策 45 v3 落库契约）。

    只写 `affairs` + `_assemble_status` + `_fct_format`，**不再写任何
    legacy 扁平字段**（changes/core_change/四段字段）。旧消费者需要的
    扁平视图由读路径（`collect_turn_fcts`）从 affairs 现场派生。
    """
    normalized: List[Dict[str, Any]] = []
    for idx, affair in enumerate(affairs, start=1):
        item = dict(affair)
        item["hdl"] = _fallback_hdl(item, idx)
        item.pop("_idx", None)
        normalized.append(item)
    return {
        "affairs": normalized,
        "_assemble_status": assemble_status,
        "_fct_format": FCT_FORMAT_V3,
    }


def _truncate_reasoning(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[截断]"


def build_fct_think_context(
    store,
    session_id: str,
    turn: int,
    *,
    max_cards: int = DEFAULT_MAX_THINK_CARDS,
    max_chars_per_card: int = DEFAULT_MAX_CHARS_PER_CARD,
    total_budget: int = DEFAULT_TOTAL_THINK_BUDGET,
) -> str:
    """代码筛选 Fct 输入思考卡。

    - 只取当前 turn 的 orient/decision 卡（排除历史 turn 噪声）；
    - orient（事务划分线索）优先于 decision；
    - 每卡截断 max_chars_per_card，总预算 total_budget，宁缺勿滥；
    - reasoning 原文从 turn_stream THINKING 行读（L2），卡内 preview 只兜底。
    """
    try:
        from .store import (
            read_think_cards_v1,
            read_turn_thinking_rows_v1,
            read_turn_user_question_v1,
        )
    except Exception:
        return ""

    try:
        cards = read_think_cards_v1(store, session_id)
        cards = [
            c for c in cards
            if c.get("turn") == turn and c.get("card_kind") in THINK_CARD_KIND_PRIORITY
        ]
        cards.sort(key=lambda c: (
            THINK_CARD_KIND_PRIORITY.get(c.get("card_kind"), 9),
            c.get("seq") or 0,
        ))
        if not cards:
            return ""
        thinking_rows = read_turn_thinking_rows_v1(store, session_id, turn)
        thinking_by_seq = {r["seq"]: r for r in thinking_rows}
        question = read_turn_user_question_v1(store, session_id, turn)

        lines: List[str] = []
        used = 0
        for card in cards[:max_cards]:
            seq = card.get("seq")
            reasoning = (thinking_by_seq.get(seq) or {}).get("Elm") or \
                card.get("preview") or ""
            if not reasoning:
                continue
            chunk = _truncate_reasoning(reasoning, max_chars_per_card)
            if used + len(chunk) > total_budget and lines:
                break
            kind = card.get("card_kind") or "think"
            lines.append(f"[思考卡 {kind}|seq={seq}]")
            if question:
                lines.append(f"问题: {question}")
            lines.append(f"思考: {chunk}")
            lines.append("")
            used += len(chunk)
            if used >= total_budget:
                break
        return "\n".join(lines).rstrip()
    except Exception:
        return ""
