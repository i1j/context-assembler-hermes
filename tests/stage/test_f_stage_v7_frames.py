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


MULTI_AFFAIR_JSON = """{"affairs":[
{"hdl":"事务A","turns":[1],
 "ooda":{"现象与问题":["问题A"],"背景与约束":[],"决策与方案":["方案A"],"后续行动":[]},
 "changes":[{"stage_tag":"已实施","core_change":"方案A落地"}]},
{"hdl":"事务B","turns":[1],
 "ooda":{"现象与问题":["问题B"],"背景与约束":[],"决策与方案":["方案B"],"后续行动":["跟进B"]},
 "changes":[{"stage_tag":"计划","core_change":"方案B将实施"}]}
]}"""


class TestFStageMultiAffairOutput:
    def test_multi_affair_response_stored_with_legacy_flatten(self, ca_engine):
        import json as _json
        from ca.store import read_fct_v5
        write_turn_v5(ca_engine.store, "test", 3, 0, role="user", elm_text="两件事",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(ca_engine.store, "test", 3, 1, role="assistant", elm_text="答复",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", is_fin=1)
        ca_engine._session_id = "test"
        ca_engine._turn_counter = 3
        ca_engine._pending_tasks = {}
        import threading
        ca_engine._task_lock = threading.Lock()

        with patch.object(ca_engine, "_call_llm_for_fct",
                          return_value=(MULTI_AFFAIR_JSON, "stop")):
            ca_engine._run_f_stage("test", 3, 1)

        data = _json.loads(read_fct_v5(ca_engine.store, "test", 3, 1))
        assert data["_fct_format"] == "v2-multi-affair"
        assert [a["hdl"] for a in data["affairs"]] == ["事务A", "事务B"]
        assert [c["core_change"] for c in data["changes"]] == ["方案A落地", "方案B将实施"]
        assert "跟进B" in data["todo"]

    def test_orient_card_injected_into_fct_input(self, ca_engine):
        from ca.store import write_think_card_v1
        write_turn_v5(ca_engine.store, "test", 4, 0, role="user", elm_text="修两个问题",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(ca_engine.store, "test", 4, 1, role="assistant", elm_text="首段分析" * 100,
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(ca_engine.store, "test", 4, 2, role="assistant", elm_text="答复",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", is_fin=1)
        write_think_card_v1(ca_engine.store, {
            "session_id": "test", "turn": 4, "seq": 1, "txn_id": 4,
            "source_kind": "cloud_think", "card_kind": "orient",
            "raw_len": 400, "preview": "首段", "status": "raw"})

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 4
        ca_engine._pending_tasks = {}
        import threading
        ca_engine._task_lock = threading.Lock()
        captured = {}

        def tracking_mock(prev_fct, elm_text, turn_index):
            captured["elm_text"] = elm_text
            return (MULTI_AFFAIR_JSON, "stop")

        with patch.object(ca_engine, "_call_llm_for_fct", tracking_mock):
            ca_engine._run_f_stage("test", 4, 2)

        assert "【首轮思考线索（代码筛选）】" in captured["elm_text"]
        assert "[思考卡 orient" in captured["elm_text"]
        assert "修两个问题" in captured["elm_text"]
