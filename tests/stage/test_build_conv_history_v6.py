"""_build_conv_history_v6 测试 — 从 turn_stream DB 重建 conv_history（方向 B）

覆盖：
- 尾部保护区（最后 2 个 user turn 全 Elm）
- 保护区外：ACT → user/fin=Elm, thought/tool=Fct
- 保护区外：REL → user/fin=Fct, thought/tool=Hdl
- 保护区外：FAR → user/fin=Hdl, thought/tool=删除
- 混合话题等级
- Fct/Hdl fallback（空 Fct→Elm, 空 Hdl→"略"）
- system_message 拼接
- bg_review 行过滤
- tool_calls_json 反序列化
- topic_mgr=None → 全 ACT 保守

设计决策对照:
  → TP-002: 三级替换（Elm/Fct/Hdl）
  → C-010: A-stage 从 DB 构建
  → ES-002: turn_stream (turn, seq) 主键
"""

import json
import pytest
from unittest.mock import MagicMock

from ca.a_stage import _normalize_arguments_str
from ca.grade import TopicGrade


# ============================================================================
# Helpers
# ============================================================================


def _make_mock_topic_mgr(grade_map: dict) -> MagicMock:
    """返回 mock TopicGradeManager，grade_map: {turn_num: TopicGrade}。"""
    mock = MagicMock()
    mock.get_turn_grade.side_effect = lambda tn: grade_map.get(tn, TopicGrade.ACT)
    return mock


def _write_rows(store, session_id, rows):
    """批量写入 turn_stream 行。

    rows: list of dict, 每行可包含 {turn, seq, role, Elm, Fct, Hdl, tool_calls_json, finish_reason, ...}
    """
    from ca.store import write_turn_v5

    for r in rows:
        write_turn_v5(
            store, session_id,
            turn=r.get("turn", 1),
            seq=r.get("seq", 0),
            role=r.get("role", "user"),
            elm_text=r.get("Elm", r.get("elm_text", "")),
            fct_text=r.get("Fct", r.get("fct_text")),
            hdl_text=r.get("Hdl", r.get("hdl_text")),
            tool_calls_json=r.get("tool_calls_json"),
            finish_reason=r.get("finish_reason"),
            tool_name=r.get("tool_name"),
            tool_call_id=r.get("tool_call_id"),
            biz_category=r.get("biz_category"),
        )


def _build(engine, topic_mgr, system_msg=None):
    """调用 _build_conv_history_v6 返回消息列表。"""
    return engine._build_conv_history_v6(topic_mgr, system_message=system_msg)


# ============================================================================
# Empty / Edge cases
# ============================================================================


class TestEmptyDB:
    def test_empty_db_returns_empty(self, ca_engine):
        result = _build(ca_engine, None)
        assert result == []

    def test_empty_db_with_system(self, ca_engine):
        result = _build(ca_engine, None, system_msg={"role": "system", "content": "you are"})
        assert result == [{"role": "system", "content": "you are"}]

    def test_no_topic_mgr_all_act(self, ca_engine):
        """topic_mgr=None → 全线 ACT 保守 tail 保护全 Elm"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "hi"},
        ])
        result = _build(ca_engine, None)
        # 1 user turn → 全在 tail 保护内
        assert result == [{"role": "user", "content": "hi"}]


# ============================================================================
# Tail protection
# ============================================================================


class TestTailProtection:
    def test_1_turn_all_tail(self, ca_engine):
        """只有 1 个 user turn → 全在 tail 保护内，全 Elm"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "早上好", "Fct": "摘要", "Hdl": "标题"},
        ])
        result = _build(ca_engine, _make_mock_topic_mgr({1: TopicGrade.FAR}))
        assert result == [{"role": "user", "content": "早上好"}]

    def test_2_turns_all_tail(self, ca_engine):
        """2 个 user turn → 都在 tail 内，全 Elm（即使 FAR）"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q1", "Fct": "f1", "Hdl": "h1"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2", "Fct": "f2", "Hdl": "h2"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.FAR, 2: TopicGrade.FAR})
        result = _build(ca_engine, mgr)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "Q1"}
        assert result[1] == {"role": "user", "content": "Q2"}

    def test_3_turns_tail_protects_last_2(self, ca_engine):
        """3 个 user turn → tail 保护后 2 轮，turn 1 按 topic grade 处理"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q1", "Fct": "摘要1"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "thinking...", "Fct": "FctThought", "tool_calls_json": '[{"id":"c1"}]'},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "result1",
             "Fct": "工具结果1", "tool_name": "read", "tool_call_id": "c1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "回答1", "Fct": "FctFin1"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        # tail 内（turn 2-3）：全 Elm
        assert result[4] == {"role": "user", "content": "Q2"}
        assert result[5] == {"role": "user", "content": "Q3"}
        # turn 1（保护区外，ACT）：user=Elm, thought=Fct, tool=Fct, fin=Elm
        assert result[0] == {"role": "user", "content": "Q1"}
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "FctThought"
        assert result[1]["tool_calls"] == [{"id": "c1"}]
        assert result[2]["role"] == "tool"
        assert result[2]["content"] == "工具结果1"
        assert result[3] == {"role": "assistant", "content": "回答1"}


# ============================================================================
# Grade-based selection (outside tail)
# ============================================================================


class TestGradeACT:
    def test_user_fin_elm_thought_tool_fct(self, ca_engine):
        """ACT：user/fin=Elm, thought/tool=Fct"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "你好", "Fct": "摘要"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "原始推理", "Fct": "推理摘要"},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "原始结果",
             "Fct": "结果摘要", "tool_name": "search", "tool_call_id": "s1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "最终回答", "Fct": "fin摘要"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "A2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT, 3: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        # turn 1 在保护区外（3 user turn，tail 保护 2-3）
        assert result[0] == {"role": "user", "content": "你好"}               # ACT user=Elm
        assert result[1]["content"] == "推理摘要"                              # ACT thought=Fct
        assert result[2]["content"] == "结果摘要"                              # ACT tool=Fct
        assert result[3] == {"role": "assistant", "content": "最终回答"}        # ACT fin=Elm
        # turn 2-3 在 tail 内
        assert result[4] == {"role": "user", "content": "Q2"}
        assert result[5] == {"role": "assistant", "content": "A2"}
        assert result[6] == {"role": "user", "content": "Q3"}


class TestGradeREL:
    def test_user_fin_fct_thought_tool_hdl(self, ca_engine):
        """REL：user/fin=Fct, thought/tool=Hdl"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "你好", "Fct": "user摘要", "Hdl": "user标题"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "推理", "Fct": "推理摘要", "Hdl": "推理标题"},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "结果",
             "Fct": "结果摘要", "Hdl": "结果标题", "tool_name": "search", "tool_call_id": "s1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "回答", "Fct": "fin摘要", "Hdl": "fin标题"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop", "Elm": "A2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.REL, 2: TopicGrade.REL, 3: TopicGrade.REL})
        result = _build(ca_engine, mgr)
        # turn 1 保护区外（3 user turn，tail 保护 2-3）
        assert result[0] == {"role": "user", "content": "user摘要"}           # REL user=Fct
        assert result[1]["content"] == "推理标题"                              # REL thought=Hdl[:150]
        assert result[2]["content"] == "结果标题"                              # REL tool=Hdl[:150]
        assert result[3] == {"role": "assistant", "content": "fin摘要"}        # REL fin=Fct
        # turn 2-3 tail 内
        assert result[4] == {"role": "user", "content": "Q2"}
        assert result[5] == {"role": "assistant", "content": "A2"}
        assert result[6] == {"role": "user", "content": "Q3"}


class TestGradeFAR:
    def test_user_fin_hdl_thought_tool_deleted(self, ca_engine):
        """FAR：user/fin=Hdl, thought/tool=删除"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "你好", "Fct": "user摘要", "Hdl": "user标题"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "推理", "Fct": "推理摘要", "Hdl": "推理标题"},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "结果",
             "Fct": "结果摘要", "Hdl": "结果标题", "tool_name": "search", "tool_call_id": "s1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "回答", "Fct": "fin摘要", "Hdl": "fin标题"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop", "Elm": "A2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.FAR, 2: TopicGrade.FAR, 3: TopicGrade.FAR})
        result = _build(ca_engine, mgr)
        # turn 1 保护区外（3 user turn，tail 保护 2-3）：user=Hdl, fin=Hdl, thought/tool 删除
        assert result[0] == {"role": "user", "content": "user标题"}           # FAR user=Hdl
        assert result[1] == {"role": "assistant", "content": "fin标题"}        # FAR fin=Hdl
        # thought(seq=1) 和 tool(seq=2) 被删除
        assert len(result) == 2 + 3  # turn 1 (2 rows) + turn 2-3 (2+1 rows tail)
        # turn 2 tail 内
        assert result[2] == {"role": "user", "content": "Q2"}
        assert result[3] == {"role": "assistant", "content": "A2"}

    def test_far_tool_in_tail_preserved(self, ca_engine):
        """FAR tool 行在尾巴保护区内的应保留（不删除）"""
        _write_rows(ca_engine.store, "test", [
            # turn 1 — FAR, 保护区外：tool 应删除
            {"turn": 1, "seq": 0, "role": "user", "Elm": "U1", "Fct": "UF1", "Hdl": "UH1"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "T1", "Fct": "TF1", "Hdl": "TH1"},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "R1", "Fct": "RF1", "Hdl": "RH1",
             "tool_name": "exec", "tool_call_id": "e1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "F1", "Fct": "FF1", "Hdl": "FH1"},
            # turn 2 — FAR, tail 内：tool 应保留
            {"turn": 2, "seq": 0, "role": "user", "Elm": "U2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "T2"},
            {"turn": 2, "seq": 2, "role": "tool", "Elm": "R2",
             "tool_name": "exec", "tool_call_id": "e2"},
            {"turn": 2, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "F2"},
            # turn 3 — FAR, tail 内：tool 应保留
            {"turn": 3, "seq": 0, "role": "user", "Elm": "U3"},
            {"turn": 3, "seq": 1, "role": "tool", "Elm": "R3",
             "tool_name": "search", "tool_call_id": "s1"},
            {"turn": 3, "seq": 2, "role": "assistant", "finish_reason": "stop",
             "Elm": "F3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.FAR, 2: TopicGrade.FAR, 3: TopicGrade.FAR})
        result = _build(ca_engine, mgr)
        # turn 1 保护区外（3 user turn，tail 保护 2-3）：user/fin=Hdl, thought/tool=删除
        assert result[0] == {"role": "user", "content": "UH1"}           # FAR user=Hdl
        assert result[1] == {"role": "assistant", "content": "FH1"}      # FAR fin=Hdl
        # thought(seq=1) 和 tool(seq=2) 应在保护区外被删除
        # turn 2-3 tail 内：全 Elm 保留（含 tool 行）
        assert result[2] == {"role": "user", "content": "U2"}
        assert result[3]["role"] == "assistant" and result[3]["content"] == "T2"  # thought=Elm
        assert result[4]["role"] == "tool" and result[4]["content"] == "R2"      # tool=Elm ← 尾区保护
        assert result[5] == {"role": "assistant", "content": "F2"}
        assert result[6] == {"role": "user", "content": "U3"}
        assert result[7]["role"] == "tool" and result[7]["content"] == "R3"      # tool=Elm ← 尾区保护
        assert result[8] == {"role": "assistant", "content": "F3"}


# ============================================================================
# Mixed grades
# ============================================================================


class TestMixedGrades:
    def test_mixed_act_rel_far(self, ca_engine):
        """3 轮各不同话题等级，合并验证"""
        _write_rows(ca_engine.store, "test", [
            # turn 1
            {"turn": 1, "seq": 0, "role": "user", "Elm": "U1", "Fct": "UF1", "Hdl": "UH1"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "T1", "Fct": "TF1", "Hdl": "TH1"},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "R1", "Fct": "RF1", "Hdl": "RH1",
             "tool_name": "exec", "tool_call_id": "e1"},
            {"turn": 1, "seq": 3, "role": "assistant", "finish_reason": "stop",
             "Elm": "F1", "Fct": "FF1", "Hdl": "FH1"},
            # turn 2
            {"turn": 2, "seq": 0, "role": "user", "Elm": "U2", "Fct": "UF2", "Hdl": "UH2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "F2", "Fct": "FF2", "Hdl": "FH2"},
            # turn 3
            {"turn": 3, "seq": 0, "role": "user", "Elm": "U3"},
        ])
        mgr = _make_mock_topic_mgr({
            1: TopicGrade.ACT,
            2: TopicGrade.FAR,
            3: TopicGrade.REL,
        })
        result = _build(ca_engine, mgr)
        # turn 1 (ACT, 保护区外)：3 user turn = tail 保护 turn 2-3
        assert result[0] == {"role": "user", "content": "U1"}           # user=Elm
        assert result[1]["content"] == "TF1"                              # thought=Fct
        assert result[2]["content"] == "RF1"                              # tool=Fct
        assert result[3] == {"role": "assistant", "content": "F1"}        # fin=Elm
        # turn 2 (FAR, tail 内)：全 Elm
        assert result[4] == {"role": "user", "content": "U2"}
        assert result[5] == {"role": "assistant", "content": "F2"}
        # turn 3 (REL, tail 内)：全 Elm
        assert result[6] == {"role": "user", "content": "U3"}


# ============================================================================
# System message
# ============================================================================


class TestSystemMessage:
    def test_system_message_prepended(self, ca_engine):
        """system_msg 在结果头部"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "hi"},
        ])
        result = _build(ca_engine, None, system_msg={"role": "system", "content": "助手"})
        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "助手"}
        assert result[1] == {"role": "user", "content": "hi"}


# ============================================================================
# Fallback when Fct/Hdl is empty
# ============================================================================


class TestFallback:
    def test_empty_fct_falls_to_elm(self, ca_engine):
        """FCT grade 但 Fct 列空 → 回退为 Elm"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "原始提问"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "原始回答"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 2, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "A2"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.REL, 2: TopicGrade.REL})
        result = _build(ca_engine, mgr)
        # REL: user/fin=Fct 但 Fct 列为空→fallback 到 Elm
        assert result[0] == {"role": "user", "content": "原始提问"}
        assert result[1] == {"role": "assistant", "content": "原始回答"}
        # turn 2 tail 内
        assert result[2] == {"role": "user", "content": "Q2"}

    def test_empty_hdl_returns_lue(self, ca_engine):
        """HDL grade 但 Hdl 列空 → 返回"略" """
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "U1", "Hdl": ""},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "stop",
             "Elm": "F1", "Hdl": ""},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "U2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "U3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.FAR, 2: TopicGrade.FAR, 3: TopicGrade.FAR})
        result = _build(ca_engine, mgr)
        # turn 1 保护区外（3 turn，tail 保护 2-3）：FAR user=Hdl(空→"略"), fin=Hdl(空→"略")
        assert result[0] == {"role": "user", "content": "略"}
        assert result[1] == {"role": "assistant", "content": "略"}
        # turn 2-3 tail 内
        assert result[2] == {"role": "user", "content": "U2"}


# ============================================================================
# bg_review filtering
# ============================================================================


class TestBgReviewFiltering:
    def test_bg_review_rows_skipped(self, ca_engine):
        """biz_category=bg_review 的行被过滤掉"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "正常轮次", "biz_category": None},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "后台审查", "biz_category": "bg_review"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "第三次"},
        ])
        result = _build(ca_engine, None)
        # turn 2（bg_review）被跳过
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "正常轮次"}
        assert result[1] == {"role": "user", "content": "第三次"}


# ============================================================================
# Tool calls JSON serialization
# ============================================================================


class TestToolCallsJson:
    def test_thought_row_with_tool_calls(self, ca_engine):
        """thought 行带 tool_calls_json → 反序列化且 arguments 归一化为 JSON string"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "查天气"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "", "Fct": "查询天气",
             "tool_calls_json": json.dumps([
                 {"id": "call_1", "type": "function",
                  "function": {"name": "get_weather", "arguments": {"city": "北京"}}}
             ], ensure_ascii=False)},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "再说吧"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT, 3: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        # turn 1 保护区外（3 turn）
        assert result[1]["content"] == "查询天气"
        raw_args = result[1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(raw_args, str), f"arguments 必须是 str，got {type(raw_args).__name__}"
        assert json.loads(raw_args) == {"city": "北京"}

    @pytest.mark.parametrize("bad_args", ["", "not-json"])
    def test_invalid_arguments_string_normalized_to_empty_object(self, ca_engine, bad_args):
        """历史非法/空 arguments string → 归一化为 "{}"（select_context 绕过宿主 sanitize_tool_call_arguments）"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "thinking", "Fct": "thought",
             "tool_calls_json": json.dumps([
                 {"id": "c1", "type": "function",
                  "function": {"name": "f1", "arguments": bad_args}}
             ])},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        assert result[1]["tool_calls"][0]["function"]["arguments"] == "{}"

    @pytest.mark.parametrize("bad_value, expected", [
        (None, {}),
        ({"x": 1, "y": "二"}, {"x": 1, "y": "二"}),
        (object(), {}),
        ({1, 2}, {}),
        (b"bytes", {}),
        (float("nan"), {}),
        (float("inf"), {}),
    ])
    def test_normalize_arguments_never_raises(self, bad_value, expected):
        """不可序列化 / NaN 值 → "{}"，不能让单条历史脏行把整轮 select_context 打回 fail-open"""
        out = _normalize_arguments_str(bad_value)
        assert isinstance(out, str)
        assert json.loads(out) == expected

    @pytest.mark.parametrize("bad_json", ['{"x": 1}', '"tool"'])
    def test_non_list_tool_calls_json_not_emitted(self, ca_engine, bad_json):
        """tool_calls_json 解析为非 list → 不设置 tool_calls，避免 OpenAI schema 非法消息"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "thinking", "Fct": "thought", "tool_calls_json": bad_json},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        thought = result[1]
        assert "tool_calls" not in thought

    def test_parallel_tool_calls(self, ca_engine):
        """多个并行 tool_calls 在同一 thought 行里"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "多工具"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "", "Fct": "工具摘要",
             "tool_calls_json": json.dumps([
                 {"id": "a", "function": {"name": "tool_a"}},
                 {"id": "b", "function": {"name": "tool_b"}},
             ])},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "结果a",
             "Fct": "摘要a", "tool_name": "tool_a", "tool_call_id": "a"},
            {"turn": 1, "seq": 3, "role": "tool", "Elm": "结果b",
             "Fct": "摘要b", "tool_name": "tool_b", "tool_call_id": "b"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT, 3: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        assert result[1]["content"] == "工具摘要"
        assert len(result[1]["tool_calls"]) == 2
        assert result[2]["content"] == "摘要a"
        assert result[3]["content"] == "摘要b"
        assert result[2]["tool_call_id"] == "a"
        assert result[3]["tool_call_id"] == "b"


# ============================================================================
# Hdl truncation
# ============================================================================


class TestHdlTruncation:
    def test_hdl_truncated_150(self, ca_engine):
        """Hdl 内容超过 150 字符时截断"""
        long_hdl = "a" * 300
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "U1", "Hdl": long_hdl},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "U2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "U3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.FAR, 2: TopicGrade.FAR, 3: TopicGrade.FAR})
        result = _build(ca_engine, mgr)
        # turn 1 FAR user=Hdl[:150]（保护区外）
        assert result[0]["content"] == "a" * 150
        assert len(result[0]["content"]) == 150


# ============================================================================
# Tool row fields
# ============================================================================


class TestToolRowFields:
    def test_tool_has_call_id_and_tool_name(self, ca_engine):
        """tool 行保留 tool_call_id 和 tool_name（Hermes 内部字段，transport 会在上线前 strip）"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "", "Fct": "thought",
             "tool_calls_json": '[{"id":"c1","function":{"name":"f1"}}]'},
            {"turn": 1, "seq": 2, "role": "tool", "Elm": "R",
             "Fct": "toolFct", "tool_name": "f1", "tool_call_id": "c1"},
            {"turn": 2, "seq": 0, "role": "user", "Elm": "Q2"},
            {"turn": 3, "seq": 0, "role": "user", "Elm": "Q3"},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT, 3: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        assert result[2]["tool_call_id"] == "c1"
        assert result[2]["tool_name"] == "f1"
        assert "name" not in result[2], "tool 消息不允许携带 Chat Completions schema 外的 name 字段"
        assert result[2]["content"] == "toolFct"

    def test_thought_message_omits_finish_reason(self, ca_engine):
        """thought 行重建的 assistant 消息不携带 finish_reason（Hermes 正常路径上线前会 pop，strict provider 拒绝）"""
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "thinking", "Fct": "thought",
             "tool_calls_json": '[{"id":"c1","function":{"name":"f1"}}]'},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        thought = next(m for m in result if m["role"] == "assistant")
        assert "finish_reason" not in thought, (
            "assistant 消息不允许携带 finish_reason（Chat Completions schema 外字段）"
        )
        # 缺 arguments 的历史行必须补 "{}"，否则宿主 _canonicalize_api_tool_calls 会 KeyError
        assert thought["tool_calls"] == [
            {"id": "c1", "function": {"name": "f1", "arguments": "{}"}}
        ]

    def test_tool_call_arguments_normalized_to_json_string(self, ca_engine):
        """tool_calls[].function.arguments 必须是 JSON string，兼容历史 dict 行"""
        legacy = json.dumps([
            {"id": "c1", "type": "function",
             "function": {"name": "f1", "arguments": {"x": 1, "y": "二"}}}
        ], ensure_ascii=False)
        _write_rows(ca_engine.store, "test", [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "Q"},
            {"turn": 1, "seq": 1, "role": "assistant", "finish_reason": "tool_calls",
             "Elm": "thinking", "Fct": "thought", "tool_calls_json": legacy},
        ])
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        tool_call = result[1]["tool_calls"][0]
        raw_args = tool_call["function"]["arguments"]
        assert isinstance(raw_args, str), (
            f"arguments 必须是 JSON string，got {type(raw_args).__name__}: {raw_args!r}"
        )
        assert json.loads(raw_args) == {"x": 1, "y": "二"}


# ============================================================================
# 决策44 拆块配套：同 request_id 的 assistant 行合并（BUG: 2026-08-17 工具轮 400）
# ============================================================================

class TestDecision44AssistantBlockMerge:
    """决策44 E-stage 拆块（THINKING + AGENT_REPLY 分两行）与 A-stage 重建配套。

    背景（2026-08-17 线上 400）：
      DeepSeek 返回 `An assistant message with 'tool_calls' must be followed by
      tool messages responding to each 'tool_call_id'. (insufficient tool messages
      following tool_calls message)` —— 因为 E-stage v7 把同一次 API 响应拆成
      THINKING（带 tool_calls）与 AGENT_REPLY（无 tool_calls）两行，A-stage 重建
      逐行输出，产生 `assistant(tool_calls)→assistant(无tool_calls)→tool×N`
      非法序列；Hermes 的 pre-call sanitizer 只修配对不修顺序，漏网。

    修复：重建时合并同一 request_id 的相邻 assistant 行为一条消息。
    """

    def _write_decision44_tool_turn(self, store, sid="test"):
        """模拟决策44 E-stage v7 对一次 tool_calls API 响应的落盘形态。"""
        from ca.store import write_turn_v5
        req = "sess:api:1"
        write_turn_v5(store, sid, 1, 0, role="user", elm_text="查天气")
        # THINKING 行（带 tool_calls）
        write_turn_v5(store, sid, 1, 1, role="assistant",
                      elm_text="用户要查天气，需要调用工具",
                      fct_text="查询天气", hdl_text="查天气",
                      tool_calls_json=json.dumps([
                          {"id": "call_a", "type": "function",
                           "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"}},
                          {"id": "call_b", "type": "function",
                           "function": {"name": "get_weather", "arguments": "{\"city\": \"上海\"}"}},
                      ], ensure_ascii=False),
                      finish_reason="tool_calls", block_type="thinking",
                      ooda_stage="decide", request_id=req)
        # AGENT_REPLY 行（无 tool_calls，同一 request）
        write_turn_v5(store, sid, 1, 2, role="assistant",
                      elm_text="好的，我来查询两地天气。",
                      fct_text="确认查询", hdl_text="查询两地天气",
                      finish_reason="tool_calls", block_type="agent_reply",
                      ooda_stage="decide", request_id=req)
        # tool 占位/结果行
        write_turn_v5(store, sid, 1, 3, role="tool", elm_text="北京晴",
                      fct_text="北京晴", hdl_text="北京晴",
                      tool_name="get_weather", tool_call_id="call_a",
                      status="ok", block_type="tool_call_result",
                      ooda_stage="observe", request_id=req)
        write_turn_v5(store, sid, 1, 4, role="tool", elm_text="上海雨",
                      fct_text="上海雨", hdl_text="上海雨",
                      tool_name="get_weather", tool_call_id="call_b",
                      status="ok", block_type="tool_call_result",
                      ooda_stage="observe", request_id=req)
        # 下一个 user turn
        write_turn_v5(store, sid, 2, 0, role="user", elm_text="然后呢")

    def test_decision44_split_rebuild_merges_assistant_blocks(self, ca_engine):
        """同 request_id 的 THINKING+AGENT_REPLY 两行重建后必须合成一条消息，
        tool_calls 后紧跟 tool 响应，不允许中间插入无 tool_calls 的 assistant。"""
        self._write_decision44_tool_turn(ca_engine.store)
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT})
        result = _build(ca_engine, mgr)

        # 收集 assistant(tool_calls) 消息与其后消息的配对检查
        tool_calls_seen = 0
        for i, m in enumerate(result):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                tool_calls_seen += 1
                # tool_calls 消息之后必须是 tool 响应（OpenAI 协议强制）
                nxt = result[i + 1] if i + 1 < len(result) else None
                assert nxt is not None and nxt.get("role") == "tool", (
                    f"assistant(tool_calls) 后必须是 tool 响应，got: {nxt!r}\n全序列: {result}"
                )
                # tool_calls 数量与后续 tool 响应数一致
                n_calls = len(m["tool_calls"])
                j = i + 1
                while j < len(result) and result[j].get("role") == "tool":
                    j += 1
                assert j - (i + 1) == n_calls, (
                    f"tool_calls={n_calls} 但 tool 响应={j-(i+1)}"
                )
        assert tool_calls_seen == 1, f"应恰有 1 条 assistant(tool_calls)，got {tool_calls_seen}"

        # assistant(tool_calls) 的 content 应包含 AGENT_REPLY 文本（合并而非丢弃）
        asst = next(m for m in result if m["role"] == "assistant" and m.get("tool_calls"))
        assert "好的，我来查询两地天气" in asst["content"], (
            f"AGENT_REPLY 文本应合并进 assistant content: {asst['content']!r}"
        )
        # tool 响应完整
        tools = [m for m in result if m["role"] == "tool"]
        assert [t["tool_call_id"] for t in tools] == ["call_a", "call_b"]

    def test_merge_keeps_plain_assistant_rows_untouched(self, ca_engine):
        """无 tool_calls 的普通 assistant(fin) 行（不同 request）不得被误合并。"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user", elm_text="Q")
        write_turn_v5(ca_engine.store, "test", 1, 1, role="assistant",
                      elm_text="回答A", finish_reason="stop",
                      block_type="agent_reply", ooda_stage="decide",
                      request_id="req1")
        write_turn_v5(ca_engine.store, "test", 2, 0, role="user", elm_text="Q2")
        write_turn_v5(ca_engine.store, "test", 2, 1, role="assistant",
                      elm_text="回答B", finish_reason="stop",
                      block_type="agent_reply", ooda_stage="decide",
                      request_id="req2")
        mgr = _make_mock_topic_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT})
        result = _build(ca_engine, mgr)
        assts = [m for m in result if m["role"] == "assistant"]
        assert len(assts) == 2
        assert [a["content"] for a in assts] == ["回答A", "回答B"]
