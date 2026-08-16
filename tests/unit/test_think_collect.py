"""决策 44 测试：ca.think_collect — 思考卡门槛与 preview（DSH K0 对齐）。"""
from ca.think_collect import (
    THINK_MIN_REASONING_CHARS,
    THINK_PREVIEW_CHARS,
    has_correction_signal,
    has_tool_error_signal,
    classify_card_kind,
    make_think_card,
)


class TestCorrectionSignal:
    def test_chinese_hit(self):
        assert has_correction_signal("之前的假设被推翻了，重新分析")

    def test_english_hit(self):
        assert has_correction_signal("root cause was wrong; fixed it")

    def test_no_hit(self):
        assert not has_correction_signal("正常分析过程")


class TestToolErrorSignal:
    def test_status_error(self):
        assert has_tool_error_signal([{"turn": 1, "status": "error"}], 1)

    def test_error_text(self):
        assert has_tool_error_signal([{"turn": 1, "status": "ok", "error_text": "boom"}], 1)

    def test_no_signal(self):
        assert not has_tool_error_signal([{"turn": 1, "status": "ok"}], 1)
        assert not has_tool_error_signal([{"turn": 2, "status": "error"}], 1)


class TestClassifyCardKind:
    def test_decision_card_requires_tool_calls(self):
        assert classify_card_kind(raw_len=10, tool_calls=[{"id": "c1"}], is_fin=False) == "decision"

    def test_conclusion_long_reasoning(self):
        assert classify_card_kind(raw_len=THINK_MIN_REASONING_CHARS, tool_calls=[],
                                  is_fin=True) == "conclusion"

    def test_conclusion_correction_short(self):
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=True,
                                  tool_error=False,
                                  reasoning_text="发现根因错误并修正") == "conclusion"

    def test_conclusion_tool_error(self):
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=True,
                                  tool_error=True) == "conclusion"

    def test_no_card(self):
        assert classify_card_kind(raw_len=0, tool_calls=[], is_fin=True) is None
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=True) is None
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=False) is None

    def test_first_think_orient_zero_threshold(self):
        """决策 44 增补：事务首 think 无工具 → orient 卡，不受 800 字门槛限制。"""
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=False,
                                  is_first_think=True) == "orient"
        assert classify_card_kind(raw_len=20, tool_calls=[], is_fin=True,
                                  is_first_think=True) == "orient"

    def test_long_first_think_fin_still_conclusion(self):
        """长首段 fin think 优先 conclusion（信息更完整）。"""
        assert classify_card_kind(raw_len=THINK_MIN_REASONING_CHARS, tool_calls=[],
                                  is_fin=True, is_first_think=True) == "conclusion"


class TestMakeThinkCard:
    def test_preview_bound_and_pointer(self):
        reasoning = "思" * 1000
        card = make_think_card(
            session_id="s", turn=1, seq=3, reasoning_text=reasoning,
            tool_calls=[], card_kind="conclusion", question_text="Q")
        assert card["session_id"] == "s"
        assert card["turn"] == 1
        assert card["seq"] == 3
        assert card["txn_id"] == 1
        assert card["source_kind"] == "cloud_think"
        assert card["raw_len"] == 1000
        assert len(card["preview"]) == THINK_PREVIEW_CHARS
        assert "reasoning" not in card, "思考卡不得复制 reasoning 全文"

    def test_tool_names_dedup_and_limit(self):
        card = make_think_card(
            session_id="s", turn=1, seq=1, reasoning_text="r",
            tool_calls=[{"id": "a", "name": "bash"}, {"id": "b", "name": "bash"},
                        {"id": "c", "name": "read"}, {"id": "d", "name": "write"},
                        {"id": "e", "name": "glob"}, {"id": "f", "name": "grep"},
                        {"id": "g", "name": "edit"}],
            card_kind="decision", question_text="")
        assert card["call_id"] == "a"
        assert card["tool_name"] == "bash,read,write,glob,grep"
