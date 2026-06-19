"""
ca/grade.py — Grade 常量和定级辅助 (v5.10)

设计决策: TP-002 (话题定级 ACT/REL/FAR → Grade 映射)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-话题拣选重构-v460-已实现
  定级规则: 话题半径 → TopicGrade, Grade.from_topic_grade() 降一级映射

术语强制：
  Elm = 原始对话轮
  Fct = 摘要
  Hdl = 历元摘要

严禁 L2/L1/L0 出现在文档和代码中（2026-06-17 约定）。
"""

from enum import Enum


class TopicGrade(str, Enum):
    """话题等级——按 query 到形心的半径定级。

    设计决策: TP-002
      viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-话题拣选重构-v460-已实现

    ACT = Active（密切关联/内球）
    REL = Related（关联/外球）
    FAR = Far（无关联/远距）
    """
    ACT = "Act"
    REL = "Rel"
    FAR = "Far"


class Grade(str, Enum):
    """对话轮摘要等级——对应 turn_stream Fct/Hdl 列的三级体系。

    设计决策: TP-002 (Grade.from_topic_grade 降一级映射)
      viking://resources/projects/context-assembler/design/decision-points-wiki.md
      话题等级 → 行等级: ACT→FCT, REL→HDL, FAR→清空(thought/tool)
    """

    ELM = "Elm"  # 原始对话轮（不可变数据）
    FCT = "Fct"  # 摘要（可代码级或 LLM 级）
    HDL = "Hdl"  # 历元摘要（精简版 Fct）

    @classmethod
    def from_topic_grade(cls, tg: "TopicGrade") -> "Grade":
        """从话题等级映射到行摘要等级（降一级）。

        设计决策: TP-002
          ACT→FCT, REL→HDL, FAR→ELM(fallback)
        """
        mapping = {TopicGrade.ACT: cls.FCT, TopicGrade.REL: cls.HDL}
        return mapping.get(tg, cls.ELM)
