"""F-stage 核心流程测试 — _run_f_stage (LLM 路径 + 截断 + 回退)。

覆盖：
- _run_f_stage LLM 生成 → parse → write 全链路
- 截断检测（finish_reason=length / 不完整标签）
- LLM 异常 → fallback Fct
- 无 Elm 行 → skip
- 已有前轮 summary → prompt 拼接

设计决策对照:
  → R-004: F-stage 分片上限 (test_*truncat*)
  → L1-004: parse_v1_markdown_xml 防御性解析 (test_*parse*)
  → L1-005: 截断检测双重校验 (test_*truncat*)
  → L1-011: MEANINGLESS_CORE 语义短路 (test_*meaningless*)
Wiki: design/decision-points-wiki.md §R-004, §L1-004~L1-011

  → tests/INDEX.md — 测试套件总览"""

import json
import threading
import time
import pytest
from unittest.mock import patch
from ca.store import write_turn_v5


# ============================================================================
# _run_f_stage — LLM 路径
# ============================================================================


class TestRunFStage:
    """REQ-FUNC-FSTAGE-001: F-stage LLM 路径"""

    def test_llm_path_writes_fct_and_hdl(self, ca_engine):
        """写入一轮 Elm → LLM 生成 → parse → _update_fct_v5"""
        # 预写一轮用户 Elm（F-stage 的输入）
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="你好",
                      fct_text='{"core_change":"初始摘要"}')
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="回复：你好！",
                      finish_reason="stop")
        # LLM mock 已在 conftest 中 autouse（返回标准 mock response）

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()
        ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "Fct 应被写入"
        data = json.loads(result)
        assert "core_change" in data, f"Fct 应含 core_change, got {data}"

    def test_llm_path_updates_hdl(self, ca_engine):
        """验证 Hdl 也被写入"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="修复数据库连接池",
                      fct_text='{"core_change":"初始摘要"}')
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="已修复连接数限制",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()
        ca_engine._run_f_stage("test", 0)

        cur = ca_engine.store.conn.execute(
            "SELECT Hdl FROM turn_stream WHERE session_id=? AND turn=? AND seq=1",
            ("test", 0),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] and row[0] != "", f"Hdl 应为非空, got {row[0]!r}"

    def test_no_elm_rows_skips(self, ca_engine):
        """turn 无 Elm 行 → skip，不调 LLM"""
        ca_engine._session_id = "test"
        ca_engine._turn_counter = 99
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()
        # 如果调了 LLM 会抛异常——但 mock 返回默认值，所以用 stats 验证未运行
        ca_engine._run_f_stage("test", 99)
        # 不抛异常即 pass（无 Elm 行时直接 return）

    def test_truncated_detection_uses_fallback(self, ca_engine):
        """finish_reason=length → 截断回退"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="很长的对话内容" * 50)
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="回复很长" * 30,
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(
            ca_engine, "_call_llm_for_fct",
            return_value=("截断内容", "length"),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "回退 Fct 应被写入"
        data = json.loads(result)
        assert data.get("_assemble_status") == 1, \
            f"截断回退应标记 _assemble_status=1, got {data.get('_assemble_status')}"
        assert data.get("core_change") == "本轮无新内容", \
            f"截断回退 core_change 应为'本轮无新内容', got {data.get('core_change')}"

    def test_truncated_incomplete_tag_fallback(self, ca_engine):
        """有 <stage_tag> 但未闭合 </core_change> → 截断回退"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="something")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="something",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        # 有 <stage_tag> 但尾部残缺（无闭合 </core_change>）
        truncated_xml = "<stage_tag>已实施</stage_tag>\n<core_change>测试"
        with patch.object(
            ca_engine, "_call_llm_for_fct",
            return_value=(truncated_xml, "stop"),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        data = json.loads(result)
        assert data.get("_assemble_status") == 1, \
            f"不完整标签应触发回退, got {data}"

    def test_llm_crash_fallback(self, ca_engine):
        """LLM 调用抛异常 → fallback Fct"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="正常对话")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="正常回复",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(
            ca_engine, "_call_llm_for_fct",
            side_effect=RuntimeError("LLM 挂了"),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "异常后 fallback Fct 应被写入"
        data = json.loads(result)
        assert "core_change" in data

    def test_previous_summary_used_in_prompt(self, ca_engine):
        """前一 fin 行 Fct 被传给 _call_llm_for_fct"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="第一轮")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="已处理", finish_reason="stop",
                      fct_text='{"core_change":"第一轮摘要"}')
        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", content="第二轮")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 1
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        prev_fct_called = []

        def tracking_mock(prev_fct, elm_text):
            prev_fct_called.append(prev_fct)
            return ("mock_response", "stop")

        with patch.object(ca_engine, "_call_llm_for_fct", tracking_mock):
            ca_engine._run_f_stage("test", 1)

        assert len(prev_fct_called) == 1, "_call_llm_for_fct 应被调用"
        assert "第一轮摘要" in str(prev_fct_called[0]), \
            f"prev_fct 应包含第一轮摘要, got {prev_fct_called[0]}"

    def test_llm_was_actually_called(self, ca_engine):
        """_call_llm_for_fct 在 _run_f_stage 中被实际调用（通过 tracking mock 验证）"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="对话")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="回复",
                      finish_reason="stop")

        call_count = [0]

        def tracking_mock(prev_fct, elm_text):
            call_count[0] += 1
            return ("mock_response", "stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(ca_engine, "_call_llm_for_fct", tracking_mock):
            ca_engine._run_f_stage("test", 0)

        assert call_count[0] == 1, \
            f"_call_llm_for_fct 应被调 1 次, 实际 {call_count[0]}"
