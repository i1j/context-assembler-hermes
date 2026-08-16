"""ca/blocks.py — EFLR EnvelopeBlocker + DSH OODA_RULES 的 Hermes 移植（决策 44）。

纯函数、零 LLM、零 IO：
- BlockType 语义迁移自 EFLR models.py / envelope_blocker.py 的 _detect_type。
- ooda_stage 迁移自 DSH ca-v7 lib/ooda.js 的 8 行确定性规则，并按 EFLR
  更细的块类型做等价映射（user=orient / thinking=decide / agent_reply=decide /
  tool_call_request=act / tool_call_result=observe / 未知=observe）。
- format_transaction_frames 供 F-stage 消费：旧行无 block_type 时回退 legacy 文本，
  保证历史 DB 兼容（决策 44 R5.1）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

USER_MESSAGE = "user_message"
AGENT_REPLY = "agent_reply"
THINKING = "thinking"
TOOL_CALL_REQUEST = "tool_call_request"
TOOL_CALL_RESULT = "tool_call_result"
SYSTEM_MESSAGE = "system"
API_METADATA = "api_metadata"
UNKNOWN = "unknown"

_OODA_STAGE_BY_BLOCK = {
    USER_MESSAGE: "orient",
    THINKING: "decide",
    AGENT_REPLY: "decide",
    TOOL_CALL_REQUEST: "act",
    TOOL_CALL_RESULT: "observe",
    API_METADATA: None,
    SYSTEM_MESSAGE: None,
    UNKNOWN: "observe",
}


def detect_block_type(
    role: str,
    *,
    has_tool_calls: bool = False,
    has_reasoning: bool = False,
    is_tool_result: bool = False,
) -> str:
    """EFLR _detect_type 的纯函数移植（envelope_blocker.py:99-131）。

    优先级：user → system → tool → api_metadata → assistant(+tool_calls)
    → assistant(+reasoning) → assistant 其他 → unknown。
    """
    normalized = (role or "").strip().lower()
    if normalized == "user":
        return USER_MESSAGE
    if normalized == "system":
        return SYSTEM_MESSAGE
    if normalized == "tool":
        return TOOL_CALL_RESULT
    if normalized == "api_metadata":
        return API_METADATA
    if normalized == "assistant":
        if has_tool_calls:
            return TOOL_CALL_REQUEST
        if has_reasoning:
            return THINKING
        return AGENT_REPLY
    return UNKNOWN


def map_ooda_stage(block_type: str) -> Optional[str]:
    """DSH OODA_RULES 的 Hermes 块级等价映射（零 LLM、零 HTTP）。"""
    return _OODA_STAGE_BY_BLOCK.get(block_type, "observe")


def _row_get(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return default


def _legacy_line(row: Dict[str, Any]) -> str:
    role = str(row.get("role") or "")
    content = str(row.get("Elm") or row.get("elm_text") or "")
    if role == "user":
        return f"User: {content}"
    if role == "tool":
        tool_name = row.get("tool_name") or ""
        return f"Tool({tool_name}): {(content or '')[:200]}"
    return f"Assistant: {content}"


def _structured_line(row: Dict[str, Any]) -> str:
    stage = row.get("ooda_stage") or map_ooda_stage(row.get("block_type") or UNKNOWN)
    block_type = row.get("block_type") or UNKNOWN
    content = str(row.get("Elm") or row.get("elm_text") or "")
    if block_type == TOOL_CALL_RESULT:
        tool_name = row.get("tool_name") or ""
        prefix = f"{tool_name}: " if tool_name else ""
        return f"[{stage}|{block_type}] {prefix}{content[:200]}"
    if block_type == TOOL_CALL_REQUEST:
        tool_name = row.get("tool_name") or ""
        return f"[{stage}|{block_type}] {tool_name}"
    suffix = ""
    if block_type == AGENT_REPLY and row.get("is_fin"):
        suffix = " (fin)"
    return f"[{stage}|{block_type}] {content}{suffix}"


def format_transaction_frames(rows: Iterable[Dict[str, Any]]) -> str:
    """把 turn_stream 增量行渲染成 F-stage 的事务帧文本。

    - 有 turn 字段 → 每 turn 一个 `[事务 #N]` 组（多事务可见）。
    - 有 block_type → `[ooda_stage|block_type]` 结构化前缀。
    - 无 block_type（历史库）→ legacy `User:/Assistant:/Tool(...)` 格式。
    """
    lines: List[str] = []
    current_turn: Optional[int] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        turn = row.get("turn")
        if turn is not None and turn != current_turn:
            lines.append(f"[事务 #{turn}]")
            current_turn = turn
        if row.get("block_type"):
            lines.append(_structured_line(row))
        else:
            lines.append(_legacy_line(row))
    return "\n".join(lines)
