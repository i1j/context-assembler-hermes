"""决策 44 测试：E 阶段 v7 — 拆块、llm_calls、think_trace、fin 元数据。"""
import json
from types import SimpleNamespace

from ca.store import (
    read_turn_stream_all,
    read_llm_call_v1,
    read_think_cards_v1,
)


def _msg(content="", tool_calls=None, reasoning=None, reasoning_content=None):
    msg = SimpleNamespace()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.reasoning = reasoning
    msg.provider_data = {}
    if reasoning_content:
        msg.provider_data["reasoning_content"] = reasoning_content
    return msg


def _tc(cid, name, args=None):
    tc = SimpleNamespace()
    tc.id = cid
    tc.name = name
    tc.type = "function"
    tc.arguments = json.dumps(args or {})
    return tc


class TestOnApiResponseV7:
    def test_reasoning_and_content_split_into_blocks(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r1",
            assistant_message=_msg(content="surface", tool_calls=[_tc("c1", "bash")],
                                   reasoning_content="深层思考"),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4",
            base_url="https://api.deepseek.com", api_mode="chat_completions",
            api_duration=0.5,
            usage={"prompt_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        rows = read_turn_stream_all(ca_engine.store, "test")
        assert [r["seq"] for r in rows] == [1, 2, 3]
        assert rows[0]["block_type"] == "thinking"
        assert rows[0]["ooda_stage"] == "decide"
        assert rows[0]["Elm"] == "深层思考"
        assert rows[0]["reasoning_chars"] == 4
        assert rows[1]["block_type"] == "agent_reply"
        assert rows[1]["Elm"] == "surface"
        assert rows[2]["block_type"] == "tool_call_request"
        assert rows[2]["ooda_stage"] == "act"
        assert rows[2]["tool_name"] == "bash"

    def test_usage_summary_output_tokens_mapped(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r2",
            assistant_message=_msg(content="查", tool_calls=[_tc("c2", "read")]),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="openai", model="gpt-4o",
            usage={"input_tokens": 10, "output_tokens": 20,
                   "cache_read_tokens": 5, "prompt_tokens": 15, "total_tokens": 35},
        )
        rows = read_turn_stream_all(ca_engine.store, "test")
        assert rows[0]["usage_prompt_tokens"] == 15
        assert rows[0]["usage_completion_tokens"] == 20

    def test_llm_calls_written(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r3",
            assistant_message=_msg(content="查", tool_calls=[_tc("c3", "read")]),
            api_call_count=2, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4", base_url="https://api.deepseek.com",
            api_mode="chat_completions", api_duration=1.25, message_count=6,
            usage={"prompt_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        )
        row = read_llm_call_v1(ca_engine.store, "test", "r3")
        assert row is not None
        assert row["provider"] == "deepseek"
        assert row["model"] == "deepseek-v4"
        assert row["base_url"] == "https://api.deepseek.com"
        assert row["finish_kind"] == "tool_calls"
        assert row["duration_ms"] == 1250
        assert row["messages_count"] == 6
        assert json.loads(row["usage_json"])["total_tokens"] == 30

    def test_think_decision_card_written(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r4",
            assistant_message=_msg(content="", tool_calls=[_tc("c4", "bash"),
                                                            _tc("c5", "bash")],
                                   reasoning_content="决策思考"),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4",
        )
        cards = read_think_cards_v1(ca_engine.store, "test")
        assert len(cards) == 1
        assert cards[0]["card_kind"] == "decision"
        assert cards[0]["call_id"] == "c4"
        assert cards[0]["tool_name"] == "bash"
        assert cards[0]["raw_len"] == 4

    def test_pure_text_still_early_returns(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r5",
            assistant_message=_msg(content="纯文本回复"),
            api_call_count=1, turn_index=1, finish_reason="stop",
            provider="deepseek", model="deepseek-v4",
        )
        assert read_turn_stream_all(ca_engine.store, "test") == []


class TestOnPostToolCallV7:
    def test_result_block_metadata(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r1",
            assistant_message=_msg(content="跑", tool_calls=[_tc("c1", "bash")]),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4",
        )
        ca_engine._on_post_tool_call_v5(
            tool_call_id="c1", tool_name="bash", args={"command": "ls"},
            result="line1\nline2", status="error",
            duration_ms=33, error_type="ToolError", error_message="boom",
        )
        rows = read_turn_stream_all(ca_engine.store, "test")
        tool_row = [r for r in rows if r["tool_call_id"] == "c1"][0]
        assert tool_row["block_type"] == "tool_call_result"
        assert tool_row["ooda_stage"] == "observe"
        assert tool_row["result_chars"] == 11
        assert tool_row["error_text"] == "boom"


class TestOnFinalResponseV7:
    def test_fin_metadata_from_last_api(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r1",
            assistant_message=_msg(content="思考", tool_calls=[_tc("c1", "bash")],
                                   reasoning_content="deep reasoning"),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4",
            usage={"prompt_tokens": 7, "output_tokens": 9},
        )
        ca_engine._on_final_response_v7(session_id="test", turn_index=1,
                                        assistant_response="最终答复")
        rows = read_turn_stream_all(ca_engine.store, "test")
        fin = [r for r in rows if r.get("is_fin") == 1][0]
        assert fin["Elm"] == "最终答复"
        assert fin["block_type"] == "agent_reply"
        assert fin["ooda_stage"] == "decide"
        assert fin["request_id"] == "r1"
        assert fin["provider"] == "deepseek"
        assert fin["model"] == "deepseek-v4"
        assert fin["finish_reason"] == "stop"

    def test_conclusion_think_card_with_tool_error(self, ca_engine):
        ca_engine._seq_counter = {1: 0}
        ca_engine._on_api_response_v7(
            api_request_id="r1",
            assistant_message=_msg(content="", tool_calls=[_tc("c1", "bash")],
                                   reasoning_content="短但纠错：根因定位失败原因"),
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
            provider="deepseek", model="deepseek-v4",
        )
        ca_engine._on_post_tool_call_v5(
            tool_call_id="c1", tool_name="bash", args={}, result="",
            status="error", error_message="exit 1",
        )
        ca_engine._on_final_response_v7(session_id="test", turn_index=1,
                                        assistant_response="已完成")
        cards = read_think_cards_v1(ca_engine.store, "test")
        kinds = {c["card_kind"] for c in cards}
        assert "decision" in kinds
        assert "conclusion" in kinds
