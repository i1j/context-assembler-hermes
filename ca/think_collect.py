"""ca/think_collect.py — DSH ca-v7 think-collect.js 的 Hermes 移植（决策 44）。

对齐 DSH 7.2 K0，并按 Hermes 事务边界补一类用户裁定优先级卡：
- L2 原文 = turn_stream THINKING 行 Elm；think_trace 只存 raw_len 指针，
  preview 仅调试展示（≤160 字符），不复制 reasoning 全文。
- decision 卡：含 tool_calls 的 reasoning（任意长度）。
- orient 卡（决策 44 增补）：**事务内首段 think 且无 tool_calls → 零门槛入卡**——
  提问后首轮 think 是事务划分的关键线索，宁多勿少。
- conclusion 卡：事务 fin 轮 reasoning 且（raw_len≥800 或 修正词表命中
  或 同 turn 存在工具错误）。
- 其余短/非 fin reasoning 不入卡（捡选纪律，宁缺勿错）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

THINK_MIN_REASONING_CHARS = 800
THINK_PREVIEW_CHARS = 160
THINK_TOOL_NAME_MAX = 5
THINK_SOURCE_KIND = "cloud_think"
THINK_CARD_KIND_ORIENT = "orient"

THINK_CORRECTION_RE = (
    r"(修正|纠正|更正|推翻|误解|误判|不对|错误诊断|失败原因|根因|修复|"
    r"fixed|incorrect|wrong|root cause|fix)"
)

import re  # noqa: E402

_CORRECTION_RE = re.compile(THINK_CORRECTION_RE, re.IGNORECASE)


def has_correction_signal(text: Any) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return bool(_CORRECTION_RE.search(text))


def has_tool_error_signal(tool_rows: Any, turn: Any) -> bool:
    if turn is None:
        return False
    for row in tool_rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("turn") != turn:
            continue
        if row.get("status") not in (None, "ok", "pending"):
            return True
        if row.get("error_text"):
            return True
    return False


def classify_card_kind(
    *,
    raw_len: int,
    tool_calls: List[Dict[str, Any]],
    is_fin: bool,
    tool_error: bool = False,
    reasoning_text: str = "",
    is_first_think: bool = False,
) -> Optional[str]:
    """入卡门槛：decision > conclusion > orient > None。

    orient（决策 44 增补）：事务内首段 think 且无 tool_calls 时零门槛入卡。
    长首段 fin think 仍优先归 conclusion（信息更完整），短首段 fin think
    不再被 800 字门槛丢弃。
    """
    if raw_len <= 0:
        return None
    if tool_calls:
        return "decision"
    if is_fin and (
        raw_len >= THINK_MIN_REASONING_CHARS
        or tool_error
        or has_correction_signal(reasoning_text)
    ):
        return "conclusion"
    if is_first_think:
        return THINK_CARD_KIND_ORIENT
    return None


def make_think_card(
    *,
    session_id: str,
    turn: int,
    seq: int,
    reasoning_text: str,
    tool_calls: List[Dict[str, Any]],
    card_kind: str,
    question_text: str = "",
    step: Optional[int] = None,
    topic_id: Optional[int] = None,
) -> Dict[str, Any]:
    """构造 think_trace 行（snake_case，与 DSH 表一致；不含 reasoning 正文）。"""
    names: List[str] = []
    for tc in tool_calls or []:
        name = (tc or {}).get("name") or (tc or {}).get("function", {}).get("name") or ""
        if name:
            names.append(name)
    unique_names: List[str] = []
    for name in names:
        if name not in unique_names:
            unique_names.append(name)
    tool_name = ",".join(unique_names[:THINK_TOOL_NAME_MAX])
    first_call_id = (tool_calls or [{}])[0].get("id", "") if tool_calls else None
    return {
        "session_id": session_id,
        "turn": turn,
        "step": step,
        "seq": seq,
        "txn_id": turn,
        "topic_id": topic_id,
        "source_kind": THINK_SOURCE_KIND,
        "card_kind": card_kind,
        "call_id": first_call_id or None,
        "tool_name": tool_name,
        "question_text": question_text or "",
        "l0_abstract": None,
        "l1_json": None,
        "entities_json": None,
        "embedding_json": None,
        "raw_len": len(reasoning_text),
        "preview": reasoning_text[:THINK_PREVIEW_CHARS],
        "status": "raw",
    }
