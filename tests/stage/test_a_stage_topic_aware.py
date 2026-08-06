"""A-stage 话题感知测试 — TopicGrade 驱动 _build_conv_history_v6（v6.2 方向 B）。

覆盖（真实 TopicGradeManager 集成，非 mock）：
- TestEmbedFailurePipeline: embed 失败 → TopicGradeManager REL 降级
- TestFullPipelineRealTopicMgr: 真实定级（detect×N → grade_on_switch）→
  _build_conv_history_v6 应用真实等级

v5 的 _simple_mutation_mode_v5 / _incremental_mutation / 增量缓存已于
v6.0 (2026-06-28) 清理（a_stage.py 注释 + changelog v6.0.0）。对应的
映射表/尾部保护/Fct null 降级等基础覆盖由 test_build_conv_history_v6.py
(19 项) 承担，本文件仅保留真实 topic_mgr 的集成场景。
"""
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from ca.grade import TopicGrade


def _write_turn_rows(store, sid, rows):
    """批量写 turn_stream 行（v6 格式：Elm/Fct/Hdl 字段名）。"""
    from ca.store import write_turn_v5
    for r in rows:
        write_turn_v5(
            store, sid, r["turn"], r["seq"],
            role=r["role"],
            elm_text=r.get("Elm", ""),
            fct_text=r.get("Fct", ""),
            hdl_text=r.get("Hdl", ""),
            finish_reason=r.get("finish_reason", "stop" if r["role"] == "assistant" else ""),
            tool_calls_json=r.get("tool_calls_json"),
            tool_call_id=r.get("tool_call_id"),
            tool_name=r.get("tool_name"),
        )


def _axis_embed(text):
    """确定性伪嵌入：主题类文本 → axis0，尾部/其他 → axis1（用于可控定级）。"""
    t = text or ""
    if any(k in t for k in ("主题A", "思路A", "工具结果A", "结论A", "主题A讨论")):
        return [1.0] + [0.0] * 767
    if "Python" in t:
        return [1.0] + [0.0] * 767
    if "数据库" in t:
        return [0.0] + [1.0] + [0.0] * 766
    return [0.0] + [1.0] + [0.0] * 766  # 尾部/默认 → axis1


class TestEmbedFailurePipeline:
    """embed 失败时 TopicGradeManager 降级行为（REL fallback）。"""

    def test_embed_failure_falls_back_to_rel(self, ca_engine):
        """embed 抛异常 → grade_on_switch 降级为 REL（v6 定级路径）。"""
        from ca.store import write_turn_v5
        from topic_manager import TopicGradeManager
        store = ca_engine.store

        write_turn_v5(store, "test", 1, 0, role="user", elm_text="Q1",
                      fct_text='{"core_change":"U1Fct"}')
        write_turn_v5(store, "test", 1, 1, role="assistant", elm_text="T1",
                      finish_reason="stop",
                      fct_text='{"core_change":"F1Fct"}')

        bad_embed = SimpleNamespace(embed=MagicMock(
            side_effect=RuntimeError("Ollama timeout")))
        mgr = TopicGradeManager(store, bad_embed)

        mgr.detect(1, [], "你好")
        mgr.detect(2, [], "换个话题")

        q_emb = [0.5] * 768
        store.session_id = "test"
        mgr.grade_on_switch(q_emb, "换个话题")

        # Verify REL fallback
        assert mgr.get_turn_grade(1) == TopicGrade.REL

    def test_no_topic_mgr_all_act(self, ca_engine):
        """topic_mgr=None → _build_conv_history_v6 全线 ACT（有数据时）。"""
        ca_engine._session_id = "test"
        _write_turn_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q1",
             "Fct": '{"core_change":"U1Fct"}'},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "T1", "Fct": '{"core_change":"F1Fct"}'},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        result = ca_engine._build_conv_history_v6(None)
        # turn 1 保护区外（3 user turn，tail 保护 2-3）：ACT → thought=Fct（可读格式）
        assert result[0]["content"] == "Q1"
        assert result[1]["content"] == "F1Fct"  # thought → Fct 渲染为可读 core


class TestFullPipelineRealTopicMgr:
    """真实 TopicGradeManager 定级 → _build_conv_history_v6 应用真实等级。"""

    def test_act_turn_replaced_with_fct(self, ca_engine):
        """TopicGradeManager 定级一个 topic → ACT → thought/tool→Fct。"""
        from topic_manager import TopicGradeManager
        store = ca_engine.store
        store.session_id = "test"
        ca_engine._session_id = "test"

        _write_turn_rows(store, "test", [
            # turn 1 — 目标话题（保护区外，axis0 → ACT）
            {"turn": 1, "seq": 0, "role": "user", "Elm": "主题问题一",
             "Fct": '{"core_change":"主题A讨论"}'},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "", "Fct": '{"core_change":"思路A"}',
             "tool_calls_json": '[{"id":"t1"}]'},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "R",
             "Fct": '{"core_change":"工具结果A"}', "tool_call_id": "t1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "原始回复", "Fct": '{"core_change":"结论A"}', "Hdl": "HdlA"},
            # turn 2-3 — 尾部保护区（axis1 → 与 query 分离，不影响 topic 1 定级）
            {"turn": 2, "seq": 0, "role": "user", "Elm": "尾部问题二",
             "Fct": '{"core_change":"尾轮二"}'},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "A2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "尾部问题三",
             "Fct": '{"core_change":"尾轮三"}'},
        ])

        with patch.object(ca_engine.embed_client, 'embed',
                          side_effect=_axis_embed):
            from ca.store import get_turn_ca_rows
            mgr = TopicGradeManager(store, ca_engine.embed_client)
            for t in (1, 2, 3):
                ca_rows = get_turn_ca_rows(store, "test", t)
                mgr.detect(t, ca_rows, f"msg{t}")
            # query 沿 axis0 → topic 1 近（ACT）
            q_emb = [0.95] + [0.0] * 767
            mgr.grade_on_switch(q_emb, "主题查询")

        grade1 = mgr.get_turn_grade(1)
        assert grade1 == TopicGrade.ACT, f"topic 1 expected ACT, got {grade1}"

        result = ca_engine._build_conv_history_v6(mgr)
        # turn 1 保护区外：ACT → user/fin=Elm, thought/tool=Fct
        assert result[0]["content"] == "主题问题一"
        assert result[1]["content"] == "思路A", \
            f"ACT thought should be Fct, got: {result[1]['content'][:50]}"
        assert result[2]["content"] == "工具结果A", \
            f"ACT tool should be Fct, got: {result[2]['content'][:50]}"
        assert result[3]["content"] == "原始回复", \
            f"ACT fin should be Elm, got: {result[3]['content'][:50]}"

    def test_two_topics_produce_different_grades(self, ca_engine):
        """多 topic → grade_on_switch 按 query proximity 产生 ACT vs FAR。"""
        from topic_manager import TopicGradeManager
        store = ca_engine.store
        store.session_id = "test"
        ca_engine._session_id = "test"

        # Topic 1: Python（axis0 → 靠近 query → ACT）
        _write_turn_rows(store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Python性能",
             "Fct": '{"core_change":"Python优化"}'},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "", "Fct": '{"core_change":"Python优化建议"}', "Hdl": "关于"},
            # Topic 2: 数据库（axis1 → 远离 query → FAR）
            {"turn": 2, "seq": 0, "role": "user", "Elm": "数据库索引",
             "Fct": '{"core_change":"数据库设计"}'},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "", "Fct": '{"core_change":"数据库建议"}', "Hdl": "建议"},
            # Topic 3: 当前切换话题（grade_on_switch 强制 ACT）+ 尾部
            {"turn": 3, "seq": 0, "role": "user", "Elm": "今天天气真好",
             "Fct": '{"core_change":"天气问候"}'},
            {"turn": 3, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "", "Fct": '{"core_change":"天气回复"}', "Hdl": "回复"},
        ])

        with patch.object(ca_engine.embed_client, 'embed',
                          side_effect=_axis_embed):
            from ca.store import get_turn_ca_rows
            mgr = TopicGradeManager(store, ca_engine.embed_client)
            # 无强制分割短语：三个不同话题（Python / 数据库 / 天气）依赖
            # Jaccard 自然分割。此前 Fct JSON 公共键名（core_change）会把
            # "数据库设计" vs "Python优化" 的 Jaccard 抬过 0.04 → 虚假延续，
            # 只能用 "换个话题" 强制分割绕开；TP-001 修复（_extract_turn_fct
            # 归一化为语义值）后 j=0.0 → 自然分割，此处即为回归验证。
            mgr.detect(1, get_turn_ca_rows(store, "test", 1), "Python性能优化专题")
            mgr.detect(2, get_turn_ca_rows(store, "test", 2), "数据库索引怎么建")
            mgr.detect(3, get_turn_ca_rows(store, "test", 3), "今天天气怎么样")

            # Query along axis 0 → close to topic 1 (Python), far from topic 2
            q_emb = [0.95] + [0.0] * 767
            mgr.grade_on_switch(q_emb, "Python异步")

            grade1 = mgr.get_turn_grade(1)  # Python → ACT
            grade2 = mgr.get_turn_grade(2)  # Database → FAR

        assert grade1 == TopicGrade.ACT, \
            f"Python topic expected ACT, got {grade1}"
        assert grade2 == TopicGrade.FAR, \
            f"Database topic expected FAR, got {grade2}"

        # v6 应用：turn 2 (FAR) 在尾部保护区内 → Elm 保留
        result = ca_engine._build_conv_history_v6(mgr)
        turn2_user = [m for m in result
                      if m["role"] == "user" and "数据库索引" in m["content"]]
        assert turn2_user, "turn 2 (tail 保护区) user 应保留 Elm"
