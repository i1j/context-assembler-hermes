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
        assert data.get("core_change") == "本轮无新内容", \
            f"Fct core_change 应为解析 fallback, got {data.get('core_change')}"

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
        """turn 无 Elm 行 → skip，不调 LLM（验证 stats 无调用计数）"""
        ca_engine._session_id = "test"
        ca_engine._turn_counter = 99
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()
        initial_fct_count = ca_engine.stats.fct_latency_ms
        ca_engine._run_f_stage("test", 99)
        # stats.fct_latency_ms 未被增加，证明 _call_llm_for_fct 未被调用
        assert ca_engine.stats.fct_latency_ms == initial_fct_count, \
            "无 Elm 行时不应调用 LLM"

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
        assert data.get("core_change") == "截断内容", \
            f"截断回退 core_change 应保留 LLM partial 输出, got {data.get('core_change')}"

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
        assert data.get("core_change") == "正常对话", \
            f"fallback core_change 应为 user Elm, got {data.get('core_change')}"

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

    def test_truncated_empty_response(self, ca_engine):
        """finish_reason=length + 空响应 → core_change 使用'阶段摘要生成中'回退"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="test")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="response",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(
            ca_engine, "_call_llm_for_fct",
            return_value=("", "length"),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "回退 Fct 应被写入"
        data = json.loads(result)
        assert "阶段摘要生成中" in data.get("core_change", ""), \
            f"空 partial 应使用'阶段摘要生成中'回退, got {data.get('core_change')}"

    def test_llm_all_retries_fail_fallback(self, ca_engine):
        """H3: _call_llm_for_fct 返回 (\"\", \"error\") → fallback Fct 含 user Elm + _assemble_status=1"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="LLM全挂了")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(
            ca_engine, "_call_llm_for_fct",
            return_value=("", "error"),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "error 后 fallback Fct 应被写入"
        data = json.loads(result)
        assert data.get("_assemble_status") == 1, \
            f"error 回退应标记 _assemble_status=1, got {data.get('_assemble_status')}"
        assert "LLM全挂了" in data.get("core_change", ""), \
            f"error 回退 core_change 应包含 user Elm, got {data.get('core_change')}"

    def test_fct_truncated_exception_catch(self, ca_engine):
        """H3: FctTruncatedException 被 _call_llm_for_fct 抛出时，partial 文本被保留"""
        from ca.exceptions import FctTruncatedException
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="test")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="response",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        with patch.object(
            ca_engine, "_call_llm_for_fct",
            side_effect=FctTruncatedException(
                message="truncated",
                response_text="### 现象与问题\n- 部分输出",
            ),
        ):
            ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert result, "异常后 Fct 应被写入"
        data = json.loads(result)
        assert data.get("_assemble_status") == 1, \
            f"truncated 回退应标记 _assemble_status=1, got {data.get('_assemble_status')}"
        assert "部分输出" in data.get("core_change", ""), \
            f"FctTruncatedException 的 partial 应被保留, got {data.get('core_change')}"


class TestFStageInvariants:
    """F-stage 不变量：幂等性 + Fct 覆盖"""

    def test_f_stage_idempotent_same_turn(self, ca_engine):
        """I3: 同一 turn 的 _run_f_stage 两次调用，Fct 内容一致"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="幂等测试")
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="回复",
                      finish_reason="stop")

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()

        # 第一次调用
        ca_engine._run_f_stage("test", 0)
        from ca.store import read_fct_v5
        first = read_fct_v5(ca_engine.store, "test", 0, 1)

        # 第二次调用
        ca_engine._run_f_stage("test", 0)
        second = read_fct_v5(ca_engine.store, "test", 0, 1)

        # LLM mock 返回相同值 → 两次 Fct 应一致
        assert first == second, \
            "同一 turn 的两次 F-stage 调用应产生相同 Fct"

    def test_f_stage_writes_fct_for_fin_row(self, ca_engine):
        """C1: F-stage 完成后，fin 行有 Fct；其他行 Fct 由 E-stage 保证"""
        write_turn_v5(ca_engine.store, "test", 0, 0,
                      role="user", content="一轮完整对话",
                      fct_text='{"core_change":"用户询问"}')
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="",
                      tool_calls_json='[{"id":"c1"}]',
                      fct_text='{"core_change":"思考中"}')
        write_turn_v5(ca_engine.store, "test", 0, 2,
                      role="tool", content="结果", tool_call_id="c1",
                      fct_text='{"core_change":"工具执行结果"}')
        write_turn_v5(ca_engine.store, "test", 0, 3,
                      role="assistant", content="最终回复",
                      finish_reason="stop",
                      fct_text='{"core_change":"初始摘要"}')

        ca_engine._session_id = "test"
        ca_engine._turn_counter = 0
        ca_engine._pending_tasks = {}
        ca_engine._task_lock = threading.Lock()
        ca_engine._run_f_stage("test", 0)

        from ca.store import read_fct_v5
        # fin 行（seq=3）的 Fct 应被 F-stage 覆盖
        fin_fct = read_fct_v5(ca_engine.store, "test", 0, 3)
        assert fin_fct is not None, "F-stage 应写入 fin 行 Fct"
        data = json.loads(fin_fct)
        assert "core_change" in data and data["core_change"] != "初始摘要", \
            f"F-stage 应覆盖 fin 行 Fct, got {data.get('core_change')}"
        # tool 行（seq=2）的 Fct 应保留 E-stage 值
        tool_fct = read_fct_v5(ca_engine.store, "test", 0, 2)
        assert tool_fct is not None
        assert "工具执行结果" in tool_fct


# ============================================================================
# _extract_hdl / _format_fct_for_display / _is_valid_fct（从 test_fstage.py 合并）
# ============================================================================


class TestExtractHdl:
    """_extract_hdl — 从 Fct dict 提取 Hdl（一句话标题）"""

    def test_normal_core_change(self, ca_engine):
        hdl = ca_engine._extract_hdl({"core_change": "修复数据库连接池溢出"})
        assert hdl == "修复数据库连接池溢出"

    def test_truncated_over_100(self, ca_engine):
        hdl = ca_engine._extract_hdl({"core_change": "修复" * 60})
        assert len(hdl) <= 100

    def test_empty_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_hdl({"core_change": ""})
        assert hdl == "无"

    def test_wu_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_hdl({"core_change": "本轮无新内容"})
        assert hdl == "无"

    def test_missing_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_hdl({"other": "data"})
        assert hdl == "无"

    def test_strip_stage_tag_segments(self, ca_engine):
        """【计划】等后续段落被截断"""
        hdl = ca_engine._extract_hdl({
            "core_change": "已扩容连接池\n【计划】继续监控\n【探讨】是否需读写分离"
        })
        assert hdl == "已扩容连接池"
        assert "【计划】" not in hdl

    def test_only_plan_tag_preserved(self, ca_engine):
        """整段都是【计划】开头，保留不变"""
        hdl = ca_engine._extract_hdl({"core_change": "【计划】继续监控"})
        assert hdl == "【计划】继续监控"


class TestFormatFctForDisplay:
    """_format_fct_for_display — Fct JSON 格式化为可读文本"""

    def test_normal_json_format(self, ca_engine):
        fct = json.dumps({
            "core_change": "修复bug",
            "new_materials": ["日志分析"],
            "objective_facts": ["影响3个用户"],
        })
        result = ca_engine._format_fct_for_display(fct)
        assert "修复bug" in result
        assert "日志分析" in result
        assert "影响3个用户" in result

    def test_debug_pattern_returns_empty(self, ca_engine):
        """调试描述（非JSON）→ 返回空字符串"""
        result = ca_engine._format_fct_for_display("当前会话 CA 注入 ctx 中")
        assert result == ""

    def test_invalid_json_but_no_debug_pattern(self, ca_engine):
        """非JSON且非调试模式 → 原文返回"""
        result = ca_engine._format_fct_for_display("这是正常的摘要文本")
        assert result == "这是正常的摘要文本"

    def test_empty_input_returns_empty(self, ca_engine):
        result = ca_engine._format_fct_for_display("")
        assert result == ""

    def test_none_input_returns_none(self, ca_engine):
        result = ca_engine._format_fct_for_display(None)
        assert result is None

    def test_json_with_no_core_change(self, ca_engine):
        """JSON 格式但无核心内容 → 原文返回（非调试模式）"""
        fct = json.dumps({"new_materials": [], "objective_facts": []})
        result = ca_engine._format_fct_for_display(fct)
        assert "new_materials" in result  # 原文返回


class TestIsValidFct:
    """_is_valid_fct — Fct 有效性验证"""

    def test_valid_with_core_change(self, ca_engine):
        assert ca_engine._is_valid_fct(json.dumps({"core_change": "修复"}))

    def test_empty_core_change(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"core_change": ""}))

    def test_wu_core(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"core_change": "本轮无新内容"}))

    def test_missing_core_key(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"other": "data"}))

    def test_not_json(self, ca_engine):
        assert not ca_engine._is_valid_fct("不是 JSON")

    def test_empty_string(self, ca_engine):
        assert not ca_engine._is_valid_fct("")
