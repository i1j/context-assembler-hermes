"""决策 44 测试：F-stage 事务帧输入（多事务 OODA 支撑）。"""
from unittest.mock import patch

from ca.blocks import format_transaction_frames
from ca.store import write_turn_v5


class TestFStageStructuredInput:
    def test_structured_rows_render_frames(self, ca_engine):
        write_turn_v5(ca_engine.store, "test", 1, 0, role="user", elm_text="多事务任务",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(ca_engine.store, "test", 1, 1, role="assistant", elm_text="思考A",
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(ca_engine.store, "test", 1, 2, role="tool", elm_text="结果A",
                      tool_name="bash", block_type="tool_call_result", ooda_stage="observe")
        write_turn_v5(ca_engine.store, "test", 1, 3, role="assistant", elm_text="答复A",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", is_fin=1)

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 1
        ca_engine._pending_tasks = {}
        import threading
        ca_engine._task_lock = threading.Lock()

        captured = {}

        def tracking_mock(prev_fct, elm_text, turn_index):
            captured["elm_text"] = elm_text
            return ("mock", "stop")

        with patch.object(ca_engine, "_call_llm_for_fct", tracking_mock):
            ca_engine._run_f_stage("test", 1, 3)

        assert "[事务 #1]" in captured["elm_text"]
        assert "[orient|user_message]" in captured["elm_text"]
        assert "[observe|tool_call_result] bash: 结果A" in captured["elm_text"]

    def test_legacy_rows_still_feed_plain_text(self, ca_engine):
        write_turn_v5(ca_engine.store, "test", 2, 0, role="user", elm_text="旧数据")
        write_turn_v5(ca_engine.store, "test", 2, 1, role="assistant", elm_text="旧回复",
                      finish_reason="stop")
        ca_engine._session_id = "test"
        ca_engine._turn_counter = 2
        ca_engine._pending_tasks = {}
        import threading
        ca_engine._task_lock = threading.Lock()
        captured = {}

        def tracking_mock(prev_fct, elm_text, turn_index):
            captured["elm_text"] = elm_text
            return ("mock", "stop")

        with patch.object(ca_engine, "_call_llm_for_fct", tracking_mock):
            ca_engine._run_f_stage("test", 2, 1)
        assert "User: 旧数据" in captured["elm_text"]
        assert "Assistant: 旧回复" in captured["elm_text"]


def test_format_transaction_frames_multi_affair():
    rows = [
        {"turn": 1, "seq": 0, "role": "user", "Elm": "任务1+任务2",
         "block_type": "user_message", "ooda_stage": "orient"},
        {"turn": 1, "seq": 1, "role": "assistant", "Elm": "处理1",
         "block_type": "thinking", "ooda_stage": "decide"},
        {"turn": 1, "seq": 2, "role": "tool", "Elm": "结果1",
         "block_type": "tool_call_result", "ooda_stage": "observe", "tool_name": "bash"},
        {"turn": 1, "seq": 3, "role": "assistant", "Elm": "处理2",
         "block_type": "thinking", "ooda_stage": "decide"},
        {"turn": 1, "seq": 4, "role": "assistant", "Elm": "完成",
         "block_type": "agent_reply", "ooda_stage": "decide", "is_fin": 1},
    ]
    text = format_transaction_frames(rows)
    lines = text.splitlines()
    assert lines[0] == "[事务 #1]"
    assert "[orient|user_message]" in lines[1]
    assert "[decide|thinking]" in lines[2]
    assert "[observe|tool_call_result]" in lines[3]
