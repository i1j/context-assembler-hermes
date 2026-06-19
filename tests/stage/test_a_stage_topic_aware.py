"""A-stage 话题感知集成测试 — TopicGrade 驱动替换管线。

验证 topic_grade (ACT/REL/FAR) 通过映射表正确控制 conv_hist 各行替换行为：

- ACT: thought/tool→Fct, fin→Elm(原文保留), user→Elm(原文保留)
- REL: thought/tool→Hdl[:150], fin→Fct
- FAR: thought/tool→""(清空), fin→Hdl[:150]

同时验证 _topic_mgr = None 时的防护分支（全线 ACT）。

conv 构建规则：3 个 user turn，第 1 个在保护区外（可被处理），后 2 个在保护区内。
TAIL 边界 = idx of 倒数第 2 个 user → idx 4。
所以 turn 1 (conv 中首个 user, current_turn=0) 被处理。

设计决策对照:
  → TP-002: 替换映射表 test_*_replaced_with_*, test_*_replaces_*
  → TP-004: thought/tool ACT→FCT (test_thought_replaced_with_full_fct)
  → TP-007: _topic_mgr=None 全线 ACT (test_no_topic_mgr_all_act)
Wiki: design/decision-points/TP-{002,004,007}.md

  → tests/INDEX.md — 测试套件总览"""
import json
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

from ca.grade import Grade, TopicGrade


def _make_plugin(ca_engine, session_id: str = "test"):
    """加载插件模块并绑定引擎（与 test_a_stage.py 保持一致）。"""
    import importlib, sys
    from pathlib import Path
    _f = Path(__file__).resolve().parent.parent.parent / "__init__.py"
    _spec = importlib.util.spec_from_file_location("ca_assembler_topic_test", _f)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["ca_assembler_topic_test"] = _mod
    _spec.loader.exec_module(_mod)
    plugin = _mod.CAContextAssemblerPlugin()
    plugin._engine = ca_engine
    plugin._session_id = session_id
    plugin._saved_history = None
    plugin._saved_history_snapshot = None
    plugin._A_stable_cache = None
    plugin._A_cache_turns = 0
    plugin._A_cache_is_stale = False
    return plugin


def _padded_conv(target, dummy_turns=2):
    """构建带 padding 的 conv_history，target 在第 1 个 user turn（保护区外）。

    3 user turns total → tail_boundary=4 → user1(idx 0-3) 被处理，user2/3 在尾巴内。

    target: [(role, content, {extra_kwargs}), ...] 第 1 个 user turn 的条目。
    dummy_turns: 尾巴保护区的 user 轮数（默认 2 轮）。
    """
    conv = []
    for role, content, *rest in target:
        msg = {"role": role, "content": content}
        if rest:
            extra = rest[0]
            if isinstance(extra, dict):
                msg.update(extra)
        conv.append(msg)

    # 添加 dummy turns（尾巴保护区）
    for i in range(dummy_turns):
        conv.append({"role": "user", "content": f"dummy_q{i+1}"})
        conv.append({"role": "assistant", "content": f"dummy_a{i+1}"})

    return conv


# ============================================================================
# ACT 路径
# ============================================================================


class TestACTGrade:
    """TopicGrade.ACT → thought/tool→Fct, fin→Elm, user→Elm"""

    def _setup_data(self, store, turn, user_fct, thought_fct, tool_fct, fin_fct):
        from ca.store import write_turn_v5
        sid = "test"
        write_turn_v5(store, sid, turn, 0, role="user", content="user_q",
                      fct_text=user_fct)
        write_turn_v5(store, sid, turn, 1, role="assistant", content="",
                      tool_calls_json='[{"id":"tc1"}]',
                      fct_text=thought_fct)
        write_turn_v5(store, sid, turn, 2, role="tool", content="tool_r",
                      tool_call_id="tc1",
                      fct_text=tool_fct)
        write_turn_v5(store, sid, turn, 3, role="assistant", content="fin",
                      finish_reason="stop",
                      fct_text=fin_fct)

    def _make_topic_mgr(self, turn_grades):
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: turn_grades.get(tn, TopicGrade.ACT)
        return mock

    def test_thought_replaced_with_full_fct(self, ca_engine):
        self._setup_data(ca_engine.store, 1,
                         user_fct='{"core_change":"UFct"}',
                         thought_fct='{"core_change":"TFct"}',
                         tool_fct='{"core_change":"ToFct"}',
                         fin_fct='{"core_change":"FinFct"}')
        plugin = _make_plugin(ca_engine)
        plugin._topic_mgr = self._make_topic_mgr({1: TopicGrade.ACT})
        conv = _padded_conv([
            ("user", "原始用户消息"),
            ("assistant", "原始思考", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "原始工具结果"),
            ("assistant", "原始回复"),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # thought → Fct（完整）
        assert conv[1]["content"] == '{"core_change":"TFct"}'
        assert "tool_calls" not in conv[1]
        # tool → Fct（完整）
        assert conv[2]["content"] == '{"core_change":"ToFct"}'
        # fin → Elm（原文保留）
        assert conv[3]["content"] == "原始回复"
        assert "tool_calls" not in conv[3]
        # user → 原文保留
        assert conv[0]["content"] == "原始用户消息"

    def test_act_fin_cleans_reasoning(self, ca_engine):
        self._setup_data(ca_engine.store, 1,
                         user_fct='{}', thought_fct='{"core_change":"T"}',
                         tool_fct='{}', fin_fct='{"core_change":"F"}')
        plugin = _make_plugin(ca_engine)
        plugin._topic_mgr = self._make_topic_mgr({1: TopicGrade.ACT})
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "思考", {"tool_calls": [{"id": "tc1"}],
                                   "reasoning_content": "深度推理"}),
            ("tool", "R"),
            ("assistant", "回复", {"reasoning_content": "最终推理"}),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # thought: cleaned（替换为 Fct，移除 reasoning/tool_calls）
        assert "reasoning_content" not in conv[1]
        assert "tool_calls" not in conv[1]
        # fin: reasoning cleaned
        assert "reasoning_content" not in conv[3]
        assert "tool_calls" not in conv[3]


# ============================================================================
# REL 路径
# ============================================================================


class TestRELGrade:
    """TopicGrade.REL → thought/tool→Hdl[:150], fin→Fct"""

    def _setup_with_long_fct(self, store, turn):
        from ca.store import write_turn_v5
        write_turn_v5(store, "test", turn, 0, role="user",
                      content="user_q",
                      fct_text='{"core_change":"UFct"}')
        write_turn_v5(store, "test", turn, 1, role="assistant", content="",
                      tool_calls_json='[{"id":"tc1"}]',
                      fct_text='{"core_change":"思考Fct_" + "x" * 200}',
                      hdl_text='思考Hdl_' + 'x' * 50)
        write_turn_v5(store, "test", turn, 2, role="tool", content="tool_r",
                      tool_call_id="tc1",
                      fct_text='{"core_change":"工具Fct_" + "y" * 200}',
                      hdl_text='工具Hdl_' + 'y' * 50)
        write_turn_v5(store, "test", turn, 3, role="assistant",
                      content="fin", finish_reason="stop",
                      fct_text='{"core_change":"回复Fct"}')

    def _make_topic_mgr(self, turn_grades):
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: turn_grades.get(tn, TopicGrade.ACT)
        return mock

    def test_thought_tool_hdl_fin_fct(self, ca_engine):
        from ca.store import write_turn_v5
        self._setup_with_long_fct(ca_engine.store, 1)
        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: {1: TopicGrade.REL}.get(tn, TopicGrade.ACT)
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "long thought", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "long tool result"),
            ("assistant", "fin response"),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # thought → Hdl[:150]
        assert len(conv[1]["content"]) <= 150
        assert "思考Hdl" in conv[1]["content"][:50]
        # tool → Hdl[:150]
        assert len(conv[2]["content"]) <= 150
        assert "工具Hdl" in conv[2]["content"][:50]
        # fin → Fct（完整）
        assert conv[3]["content"] == '{"core_change":"回复Fct"}'

    def test_rel_fin_replaces_original(self, ca_engine):
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user",
                      content="Q", fct_text='{}')
        write_turn_v5(ca_engine.store, "test", 1, 1, role="assistant",
                      content="", tool_calls_json='[{"id":"tc1"}]',
                      fct_text='{"core_change":"T"}')
        write_turn_v5(ca_engine.store, "test", 1, 2, role="tool",
                      content="R", tool_call_id="tc1",
                      fct_text='{}')
        write_turn_v5(ca_engine.store, "test", 1, 3, role="assistant",
                      content="", finish_reason="stop",
                      fct_text='{"core_change":"REL Fin Fct"}')
        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: {1: TopicGrade.REL}.get(tn, TopicGrade.ACT)
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "T", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "R"),
            ("assistant", "original fin"),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # fin 被替换为 Fct，覆盖原文
        assert conv[3]["content"] == '{"core_change":"REL Fin Fct"}'


# ============================================================================
# FAR 路径
# ============================================================================


class TestFARGrade:
    """TopicGrade.FAR → thought/tool→""(清空), fin→Hdl[:150]"""

    def test_thought_tool_cleared_fin_hdl(self, ca_engine):
        from ca.store import write_turn_v5
        store = ca_engine.store
        write_turn_v5(store, "test", 1, 0, role="user", content="Q",
                      fct_text='{}')
        write_turn_v5(store, "test", 1, 1, role="assistant", content="",
                      tool_calls_json='[{"id":"tc1"}]',
                      fct_text='{"core_change":"ThoughtFct"+ "z" * 200}')
        write_turn_v5(store, "test", 1, 2, role="tool", content="R",
                      tool_call_id="tc1",
                      fct_text='{"core_change":"ToolFct"}')
        write_turn_v5(store, "test", 1, 3, role="assistant", content="",
                      finish_reason="stop",
                      fct_text='{"core_change":"FinFct_long" + "x" * 200}',
                      hdl_text='FinHdl_long_' + 'z' * 50)

        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: {1: TopicGrade.FAR}.get(tn, TopicGrade.ACT)
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "thought", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "tool result"),
            ("assistant", "fin reply", {"reasoning_content": "chain"}),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # thought → 清空
        assert conv[1]["content"] == "", f"thought should be empty, got: {conv[1]['content'][:50]}"
        # tool → 清空
        assert conv[2]["content"] == "", f"tool should be empty"
        # fin → Hdl[:150]
        assert len(conv[3]["content"]) <= 150
        assert "FinHdl" in conv[3]["content"][:50]
        assert "reasoning_content" not in conv[3]


# ============================================================================
# _topic_mgr = None 防护
# ============================================================================


class TestTopicMgrNoneGuard:
    """_topic_mgr = None → 全线 ACT → thought/tool→Fct, fin→Elm"""

    def test_no_topic_mgr_all_act(self, ca_engine):
        from ca.store import write_turn_v5
        store = ca_engine.store
        write_turn_v5(store, "test", 1, 0, role="user", content="Q",
                      fct_text='{"core_change":"UFct"}')
        write_turn_v5(store, "test", 1, 1, role="assistant", content="",
                      tool_calls_json='[{"id":"tc1"}]',
                      fct_text='{"core_change":"TFct"}')
        write_turn_v5(store, "test", 1, 2, role="tool", content="R",
                      tool_call_id="tc1",
                      fct_text='{"core_change":"ToFct"}')
        write_turn_v5(store, "test", 1, 3, role="assistant", content="",
                      finish_reason="stop",
                      fct_text='{"core_change":"FinFct"}')

        plugin = _make_plugin(ca_engine)
        plugin._topic_mgr = None  # 关键：_topic_mgr = None
        conv = _padded_conv([
            ("user", "用户原文"),
            ("assistant", "思考原文", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "工具原文"),
            ("assistant", "回复原文"),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # ACT: thought/tool→Fct, fin→Elm
        assert conv[1]["content"] == '{"core_change":"TFct"}', \
            f"no mgr thought should be Fct, got: {conv[1]['content'][:50]}"
        assert conv[2]["content"] == '{"core_change":"ToFct"}'
        assert conv[3]["content"] == "回复原文"


# ============================================================================
# 用户行永不替换 + 尾巴保护区
# ============================================================================


class TestUserInvariants:
    """用户行：ACT 保留原文，REL 替换为 Fct，FAR 替换为 Hdl[:150]"""

    def test_user_never_replaced_act(self, ca_engine):
        """ACT: user 保留原文"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user",
                      content="用户消息_ACT",
                      fct_text='{"core_change":"Fct_ACT"}')
        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.return_value = TopicGrade.ACT
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "用户消息_ACT"),
            ("assistant", "dummy"),
        ])
        plugin._simple_mutation_mode_v5(conv)
        # ACT → user_fin_map ACT→ELM → 原文保留
        assert conv[0]["content"] == "用户消息_ACT"

    def test_user_replaced_with_fct_in_rel(self, ca_engine):
        """REL: user 被替换为 Fct（预期行为：user_fin_map REL→FCT）"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user",
                      content="用户消息_REL",
                      fct_text='{"core_change":"Fct_REL"}')
        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.return_value = TopicGrade.REL
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "用户消息_REL"),
            ("assistant", "dummy"),
        ])
        plugin._simple_mutation_mode_v5(conv)
        # REL → user_fin_map REL→FCT → 替换为 Fct
        assert conv[0]["content"] == '{"core_change":"Fct_REL"}'

    def test_user_replaced_with_hdl_in_far(self, ca_engine):
        """FAR: user 被替换为 Hdl[:150]（预期行为：user_fin_map FAR→HDL）"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user",
                      content="用户消息_FAR",
                      fct_text='{"core_change":"Fct_FAR_long_' + 'x' * 200 + '"}',
                      hdl_text='Hdl_FAR_long_' + 'z' * 50)
        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.return_value = TopicGrade.FAR
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "用户消息_FAR"),
            ("assistant", "dummy"),
        ])
        plugin._simple_mutation_mode_v5(conv)
        # FAR → user_fin_map FAR→HDL → Hdl[:150]
        assert len(conv[0]["content"]) <= 150
        assert "Hdl_FAR" in conv[0]["content"][:50]

    def test_tail_protects_last_two_user_turns(self, ca_engine):
        """确认尾巴保护区的 user/assistant 行不被替换"""
        from ca.store import write_turn_v5
        store = ca_engine.store
        # Turn 1（保护区外）
        write_turn_v5(store, "test", 1, 0, role="user", content="Q1_out",
                      fct_text='{}')
        write_turn_v5(store, "test", 1, 1, role="assistant", content="",
                      finish_reason="stop",
                      fct_text='{"core_change":"Replaced"}')
        # Turn 2（保护区内）
        write_turn_v5(store, "test", 2, 0, role="user", content="Q2_in",
                      fct_text='{}')
        write_turn_v5(store, "test", 2, 1, role="assistant", content="",
                      finish_reason="stop",
                      fct_text='{"core_change":"InTail"}')
        # Turn 3（保护区内）
        write_turn_v5(store, "test", 3, 0, role="user", content="Q3_in",
                      fct_text='{}')
        write_turn_v5(store, "test", 3, 1, role="assistant", content="",
                      finish_reason="stop",
                      fct_text='{"core_change":"AlsoInTail"}')

        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.return_value = TopicGrade.REL
        plugin._topic_mgr = mock
        conv = [
            {"role": "user", "content": "Q1_out_original"},
            {"role": "assistant", "content": "F1_out_original"},
            {"role": "user", "content": "Q2_in_original"},
            {"role": "assistant", "content": "F2_in_original"},
            {"role": "user", "content": "Q3_in_original"},
            {"role": "assistant", "content": "F3_in_original"},
        ]
        plugin._simple_mutation_mode_v5(conv)

        # Turn 1（3 user 中第 1 个 → 保护区外）→ fin 被替换
        assert conv[1]["content"] == '{"core_change":"Replaced"}', \
            f"T1 should be replaced, got: {conv[1]['content'][:50]}"
        # Turn 2（保护区内）→ 原文保留
        assert conv[3]["content"] == "F2_in_original"
        # Turn 3（保护区内）→ 原文保留
        assert conv[5]["content"] == "F3_in_original"


# ============================================================================
# Multi-turn mixed grades
# ============================================================================


class TestMultiTurnMixedGrades:
    """多轮混合等级"""

    def test_mixed_grades(self, ca_engine):
        from ca.store import write_turn_v5
        store = ca_engine.store

        # 3 轮数据, 各自写入
        for turn, (ufct, tfct, tofct, ffct, fhdl) in {
            1: ("U1_FAR", "T1_FAR", "To1_FAR", "F1_FAR", "F1_Hdl"),
            2: ("U2_ACT", "T2_ACT", "To2_ACT", "F2_ACT", ""),
            3: ("U3_REL", "T3_REL", "To3_REL", "F3_REL", "F3_Hdl"),
        }.items():
            write_turn_v5(store, "test", turn, 0, role="user",
                          content=f"Q{turn}",
                          fct_text=f'{{"core_change":"{ufct}"}}')
            write_turn_v5(store, "test", turn, 1, role="assistant",
                          content="",
                          tool_calls_json=f'[{{"id":"tc{turn}"}}]',
                          fct_text=f'{{"core_change":"{tfct}"}}')
            write_turn_v5(store, "test", turn, 2, role="tool",
                          content=f"R{turn}",
                          tool_call_id=f"tc{turn}",
                          fct_text=f'{{"core_change":"{tofct}"}}')
            write_turn_v5(store, "test", turn, 3, role="assistant",
                          content=f"Fin{turn}",
                          finish_reason="stop",
                          fct_text=f'{{"core_change":"{ffct}"}}',
                          hdl_text=fhdl)

        plugin = _make_plugin(ca_engine)
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: {
            1: TopicGrade.FAR,
            2: TopicGrade.ACT,
            3: TopicGrade.REL,
        }.get(tn, TopicGrade.ACT)
        plugin._topic_mgr = mock
        conv = _padded_conv([
            ("user", "Q1原"),
            ("assistant", "T1原", {"tool_calls": [{"id": "tc1"}]}),
            ("tool", "R1原"),
            ("assistant", "Fin1原"),
        ])
        plugin._simple_mutation_mode_v5(conv)

        # Turn 1 (FAR): thought→"", tool→"", fin→Hdl
        assert conv[1]["content"] == "", f"FAR thought should be ''"
        assert conv[2]["content"] == "", f"FAR tool should be ''"
        assert len(conv[3]["content"]) <= 150
        assert "F1_Hdl" in conv[3]["content"][:50]


# ============================================================================
# Embed 失败 + 完整管线
# ============================================================================


class TestEmbedFailurePipeline:
    """embed 失败时 TopicGradeManager 降级行为"""

    def test_embed_failure_rel_then_mutation(self, ca_engine):
        from ca.store import write_turn_v5
        from topic_manager import TopicGradeManager
        store = ca_engine.store

        write_turn_v5(store, "test", 1, 0, role="user", content="Q1",
                      fct_text='{"core_change":"U1Fct"}')
        write_turn_v5(store, "test", 1, 1, role="assistant", content="T1",
                      finish_reason="stop",
                      fct_text='{"core_change":"F1Fct"}')

        # TopicGradeManager with failing embed
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

        # Apply through plugin
        plugin = _make_plugin(ca_engine)
        plugin._topic_mgr = mgr
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "T", {"tool_calls": [{"id": "t1"}]}),
            ("tool", "R"),
            ("assistant", "原始回复"),
        ])
        plugin._simple_mutation_mode_v5(conv)
        # REL: fin→Fct
        assert conv[3]["content"] == '{"core_change":"F1Fct"}', \
            f"fin with REL should be Fct, got: {conv[3]['content'][:50]}"

    def test_plugin_mgr_none_safe(self, ca_engine):
        """_topic_mgr = None 时 mutation 不 crash"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user",
                      content="Q", fct_text='{}')
        write_turn_v5(ca_engine.store, "test", 1, 1, role="assistant",
                      content="", finish_reason="stop",
                      fct_text='{"core_change":"F"}')
        plugin = _make_plugin(ca_engine)
        plugin._topic_mgr = None
        conv = _padded_conv([
            ("user", "Q"),
            ("assistant", "原始回复"),
        ])
        result = plugin._simple_mutation_mode_v5(conv)
        assert result is None
        assert conv[1]["content"] == "原始回复"
