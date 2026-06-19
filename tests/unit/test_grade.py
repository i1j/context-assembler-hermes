"""Grade 定级体系测试 — Grade / TopicGrade 枚举 + 映射。

术语强制：Elm/Fct/Hdl，严禁 L2/L1/L0。

设计决策对照:
  → TP-002: from_topic_grade ACT→FCT, REL→HDL, FAR→ELM
Wiki: design/decision-points/TP-002.md

  → tests/INDEX.md — 测试套件总览"""

import pytest
from ca.grade import Grade, TopicGrade


class TestTopicGrade:
    """TopicGrade 枚举常量"""

    def test_has_three_levels(self):
        assert TopicGrade.ACT == "Act"
        assert TopicGrade.REL == "Rel"
        assert TopicGrade.FAR == "Far"

    def test_values_are_unique(self):
        vals = [v.value for v in TopicGrade]
        assert len(vals) == len(set(vals))

    def test_act_is_active_related(self):
        assert TopicGrade.ACT is TopicGrade("Act")


class TestGrade:
    """Grade 枚举常量"""

    def test_has_three_levels(self):
        assert Grade.ELM == "Elm"
        assert Grade.FCT == "Fct"
        assert Grade.HDL == "Hdl"

    def test_values_are_unique(self):
        vals = [v.value for v in Grade]
        assert len(vals) == len(set(vals))

    def test_elm_is_elm(self):
        assert Grade.ELM is Grade("Elm")


class TestFromTopicGrade:
    """TopicGrade → Grade 降一级映射"""

    def test_act_maps_to_fct(self):
        """密切关联 → 保留摘要"""
        assert Grade.from_topic_grade(TopicGrade.ACT) == Grade.FCT

    def test_rel_maps_to_hdl(self):
        """关联 → 降为历元摘要"""
        assert Grade.from_topic_grade(TopicGrade.REL) == Grade.HDL

    def test_far_maps_to_elm(self):
        """无关联 → 降为原文（与 topic 无关的 thought/tool 清空）"""
        assert Grade.from_topic_grade(TopicGrade.FAR) == Grade.ELM

    def test_unknown_fallback_to_elm(self):
        """未知话题等级 → 安全降为原文"""
        assert Grade.from_topic_grade(None) == Grade.ELM


class TestGradeConstantsInvariant:
    """Grade 和 TopicGrade 不混用同一枚举"""

    def test_topic_grade_not_interchangeable_with_grade(self):
        """两个枚举没有公共值"""
        grade_vals = {g.value for g in Grade}
        topic_vals = {t.value for t in TopicGrade}
        assert grade_vals.isdisjoint(topic_vals), \
            f"Grade and TopicGrade share values: {grade_vals & topic_vals}"

    def test_module_level_constants_exist(self):
        """依赖这些常量的导入不失败"""
        from ca.grade import TopicGrade as TG, Grade as G
        assert TG.ACT and TG.REL and TG.FAR
        assert G.ELM and G.FCT and G.HDL
