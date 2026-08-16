"""决策 44 测试：ca.blocks — 块类型检测 / OODA 打标 / 事务帧格式化。"""
import pytest

from ca.blocks import (
    AGENT_REPLY,
    THINKING,
    TOOL_CALL_REQUEST,
    TOOL_CALL_RESULT,
    USER_MESSAGE,
    UNKNOWN,
    detect_block_type,
    map_ooda_stage,
    format_transaction_frames,
)


class TestDetectBlockType:
    def test_user_role(self):
        assert detect_block_type(role="user") == USER_MESSAGE

    def test_tool_role(self):
        assert detect_block_type(role="tool") == TOOL_CALL_RESULT

    def test_assistant_tool_calls_wins_over_reasoning(self):
        assert detect_block_type(role="assistant", has_tool_calls=True,
                                 has_reasoning=True) == TOOL_CALL_REQUEST

    def test_assistant_reasoning(self):
        assert detect_block_type(role="assistant", has_reasoning=True) == THINKING

    def test_assistant_plain(self):
        assert detect_block_type(role="assistant") == AGENT_REPLY

    def test_unknown_role(self):
        assert detect_block_type(role="api_metadata") == "api_metadata"
        assert detect_block_type(role="") == UNKNOWN


class TestMapOodaStage:
    @pytest.mark.parametrize("block_type, expected", [
        (USER_MESSAGE, "orient"),
        (THINKING, "decide"),
        (AGENT_REPLY, "decide"),
        (TOOL_CALL_REQUEST, "act"),
        (TOOL_CALL_RESULT, "observe"),
        ("api_metadata", None),
        (UNKNOWN, "observe"),
    ])
    def test_mapping(self, block_type, expected):
        assert map_ooda_stage(block_type) == expected


class TestFormatTransactionFrames:
    def test_grouped_structured_rows(self):
        rows = [
            {"turn": 3, "seq": 0, "role": "user", "Elm": "修两个bug",
             "block_type": "user_message", "ooda_stage": "orient"},
            {"turn": 3, "seq": 1, "role": "assistant", "Elm": "先查A",
             "block_type": "thinking", "ooda_stage": "decide"},
            {"turn": 3, "seq": 2, "role": "tool", "Elm": "结果A",
             "block_type": "tool_call_result", "ooda_stage": "observe",
             "tool_name": "bash"},
            {"turn": 3, "seq": 3, "role": "assistant", "Elm": "A已修",
             "block_type": "agent_reply", "ooda_stage": "decide",
             "is_fin": 1},
        ]
        text = format_transaction_frames(rows)
        assert "[事务 #3]" in text
        assert "[orient|user_message] 修两个bug" in text
        assert "[decide|thinking] 先查A" in text
        assert "[observe|tool_call_result] bash: 结果A" in text
        assert "[decide|agent_reply] A已修 (fin)" in text

    def test_legacy_fallback(self):
        rows = [
            {"turn": 1, "seq": 0, "role": "user", "Elm": "你好"},
            {"turn": 1, "seq": 1, "role": "assistant", "Elm": "你好呀"},
        ]
        text = format_transaction_frames(rows)
        assert "User: 你好" in text
        assert "Assistant: 你好呀" in text

    def test_tool_call_request_renders_name(self):
        rows = [{"turn": 2, "seq": 1, "role": "tool", "Elm": "",
                 "tool_name": "read", "block_type": "tool_call_request",
                 "ooda_stage": "act"}]
        text = format_transaction_frames(rows)
        assert "[act|tool_call_request] read" in text
