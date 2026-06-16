"""
ca/a_injector.py — A-stage 写入替换模块 (v5.0)

消费 a_planner.AStagePlan，从 DB 提取对应摘要等级的内容，替换历史记忆表。

替换规则：
  elm —— 不替换，保留原文
  fct —— 从 turn_stream.Fct 读取结构化摘要，经 ToolPlan 裁剪后注入
  hdl —— 从 turn_stream.Hdl 读取标题注入
"""

import json
import logging
from typing import Any, Dict, List, Optional

from ca.store import read_fct_v5, read_hdl_v5
from ca.tool_plan import ToolPlan
from ca.a_planner import AStagePlan, ELM, FCT, HDL

logger = logging.getLogger(__name__)


def inject(
    conversation_history: list,
    plan: AStagePlan,
    store: Any,
    session_id: str,
) -> int:
    """
    按 plan 注入摘要到 conversation_history。

    Args:
        conversation_history: 待修改的消息列表（就地修改）
        plan: 规划摘要等级
        store: DB 访问对象
        session_id: 当前会话 ID

    Returns:
        成功注入的消息数（不包括 elm 跳过和静默跳过）
    """
    turn = 0
    seq = 0
    replaced = 0

    for i, msg in enumerate(conversation_history):
        role = msg.get("role", "")

        # ── 轮次/序列计数 ──
        if role == "system":
            continue
        if role == "user":
            turn += 1
            seq = 0
            continue
        seq += 1

        # ── 按等级处理 ──
        level = plan.get_seq_level(turn, seq)

        if level == ELM:
            continue

        if role == "assistant" and not msg.get("tool_calls"):
            continue

        # ── Fct 注入：从 fct 读取，经 ToolPlan 裁剪 ──
        if level == FCT:
            raw = read_fct_v5(store, session_id, turn, seq)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                pruned = ToolPlan.filter(data, FCT)
                msg["content"] = json.dumps(pruned, ensure_ascii=False)
                replaced += 1
            except (json.JSONDecodeError, TypeError):
                msg["content"] = raw
                replaced += 1

        # ── Hdl 注入：从 hdl 读取 ──
        elif level == HDL:
            hdl = read_hdl_v5(store, session_id, turn, seq)
            if hdl:
                msg["content"] = hdl
                replaced += 1

    return replaced
