"""
ca/a_planner.py — A-stage 规划模块 (v5.0)

逐行确定注入内容的摘要等级，两级决策：
  1. turn_level: 根据对话轮位置确定粗粒度等级
  2. seq_level:  根据 turn_level + 消息角色确定每条消息的精粒度等级

输出 AStagePlan 供 a_injector.py 消费。
"""

from typing import Dict, List, Tuple


def summarize_plan(plan: "AStagePlan") -> str:
    """调试用：压缩 plan 为单行字符串。"""
    turn_levels = ",".join(f"{t}:{l}" for t, l in sorted(plan.turn_levels.items()))
    return f"turns[{turn_levels}]"


# ── 等级常量 ──
ELM = "elm"   # 原始数据，不替换
FCT = "fct"   # 事实摘要，用 Fct 替换
HDL = "hdl"   # 标题，用 Hdl 替换

# ── 保护配置 ──
TAIL_PROTECT_TURNS = 2  # 最后 N 个 user 消息保护为 Elm


class AStagePlan:
    """
    注入计划数据结构。

    turn_level[turn] → 该轮的基础等级
    seq_level[(turn,seq)] → 该消息的最终等级（默认继承 turn_level）
    """

    def __init__(self):
        self.turn_levels: Dict[int, str] = {}
        self.seq_levels: Dict[Tuple[int, int], str] = {}

    def get_seq_level(self, turn: int, seq: int) -> str:
        """获取消息的最终等级，未显式设置则继承 turn_level。"""
        return self.seq_levels.get((turn, seq), self.turn_levels.get(turn, FCT))


def compute_plan(conversation_history: list) -> AStagePlan:
    """
    生成注入计划。

    输入：conversation_history（Hermes 消息列表）
    输出：AStagePlan（逐消息等级映射）

    决策逻辑：
      1. 从尾向前标记保护轮（最后 TAIL_PROTECT_TURNS 个 user + 其全部 seq → elm）
      2. 其余轮 turn_level = fct
      3. seq_level 继承 turn_level（tool 行外，后续策略可扩展）
    """
    plan = AStagePlan()

    # ── Step 1: 确定 turn 等级 ──
    turn_count = 0
    for msg in conversation_history:
        if msg.get("role") == "user":
            turn_count += 1

    # 尾区保护：最后 N 个 user 所在轮次 = elm
    tail_start_turn = max(1, turn_count - TAIL_PROTECT_TURNS + 1)
    for t in range(1, turn_count + 1):
        plan.turn_levels[t] = ELM if t >= tail_start_turn else FCT

    # ── Step 2: 确定各 seq 等级（当前全部继承 turn_level，不作细分）──
    # 后续可扩展为：同一轮中 tool 行 = fct, user/assistant = elm 等细分规则
    # plan 在 injector 消费时按 turn_level 查，seq_levels 为空时自动回退 turn_level

    return plan
