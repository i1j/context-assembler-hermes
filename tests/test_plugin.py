"""插件适配层测试 — CAContextAssemblerPlugin 生命周期与钩子。

覆盖 `__init__.py`（根）的插件包装层：
- is_available() / 断路器状态
- on_session_start / on_session_end / on_session_reset
- pre_llm_call（context 注入）
- post_llm_call（C‑stage 触发）
- register() 钩子注册
"""

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 加载根 __init__.py（插件适配层）作为独立模块
# conftest 已把 ca_assembler/ 添加到 sys.path，但其父目录才是 ca_assembler 包的所在
_plugin_file = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("ca_assembler_plugin", _plugin_file)
_ca_plugin = importlib.util.module_from_spec(_spec)
sys.modules["ca_assembler_plugin"] = _ca_plugin
_spec.loader.exec_module(_ca_plugin)

CAContextAssemblerPlugin = _ca_plugin.CAContextAssemblerPlugin
is_available = _ca_plugin.is_available
_record_failure = _ca_plugin._record_failure
_record_success = _ca_plugin._record_success
_read_state = _ca_plugin._read_state
_state_file_path = _ca_plugin._state_file_path
register = _ca_plugin.register

# ============================================================================
# REQ-FUNC-CIRCUIT: 断路器 / is_available
# ============================================================================


class TestIsAvailable:
    """REQ-FUNC-CIRCUIT-002/003: 断路器状态查询。"""

    def test_initial_state(self):
        """初始状态为可用。"""
        _record_success()
        assert is_available() is True

    def test_after_3_failures(self):
        """连续 3 次失败后不可用。"""
        _record_success()
        _record_failure()
        _record_failure()
        _record_failure()
        assert is_available() is False
        _record_success()  # cleanup

    def test_after_2_failures(self):
        """2 次失败仍可用（阈值=3）。"""
        _record_success()
        _record_failure()
        _record_failure()
        assert is_available() is True
        _record_success()

    def test_after_success_recovers(self):
        """成功后重置，可用。"""
        _record_success()
        _record_failure()
        _record_failure()
        _record_failure()
        assert is_available() is False
        _record_success()
        assert is_available() is True


# ============================================================================
# 断路器状态文件清理：_pid_exists / _cleanup_stale_state_files / _write_state
# ============================================================================


class TestBreakerStateCleanup:
    """_cleanup_stale_state_files() 与 _pid_exists() 的单元测试。"""

    # ── _pid_exists ──

    def test_pid_self_exists(self):
        """当前进程 PID 应存在。"""
        assert _ca_plugin._pid_exists(os.getpid()) is True

    def test_pid_zero_does_not_exist(self):
        """PID 0 不应存在。"""
        assert _ca_plugin._pid_exists(0) is False

    @pytest.mark.skipif(os.name != 'posix', reason='requires /proc')
    def test_pid_init_exists(self):
        """PID 1 (init/systemd) 应存在。"""
        assert _ca_plugin._pid_exists(1) is True

    @pytest.mark.skipif(os.name != 'posix', reason='requires /proc')
    def test_pid_huge_does_not_exist(self):
        """超大 PID 应返回 False。"""
        assert _ca_plugin._pid_exists(999999999) is False

    # ── _cleanup_stale_state_files ──

    def test_cleanup_removes_dead_pids_leaves_current(self, tmp_path):
        """清理死 PID 文件，保留当前 PID 文件。"""
        current_pid = os.getpid()
        (tmp_path / f".ca_assembler_state_{current_pid}.json").write_text("{}")
        (tmp_path / ".ca_assembler_state_9999999.json").write_text("{}")
        (tmp_path / ".ca_assembler_state_9999998.json").write_text("{}")

        _ca_plugin._cleanup_stale_state_files()

        remaining = [p.name for p in tmp_path.iterdir()]
        assert f".ca_assembler_state_{current_pid}.json" in remaining
        assert ".ca_assembler_state_9999999.json" not in remaining
        assert ".ca_assembler_state_9999998.json" not in remaining

    def test_cleanup_ignores_non_state_files(self, tmp_path):
        """不匹配状态文件模式的文件不受影响。"""
        pid = os.getpid()
        (tmp_path / f".ca_assembler_state_{pid}.json").write_text("{}")
        (tmp_path / "random.txt").write_text("hello")
        (tmp_path / ".other_prefix_state_123.json").write_text("{}")

        _ca_plugin._cleanup_stale_state_files()

        remaining = [p.name for p in tmp_path.iterdir()]
        assert "random.txt" in remaining
        assert ".other_prefix_state_123.json" in remaining
        assert f".ca_assembler_state_{pid}.json" in remaining
        # 过滤 ca_engine fixture 可能产生的 .db/.db-wal/.db-shm 文件
        relevant = [n for n in remaining if not n.endswith(('.db', '.db-wal', '.db-shm'))]
        assert len(relevant) == 3, f"Expected 3 relevant files, got {len(relevant)}: {relevant}"

    def test_cleanup_empty_dir_no_error(self, tmp_path):
        """空目录不报错。"""
        _ca_plugin._cleanup_stale_state_files()  # 无异常即通过

    def test_cleanup_nonexistent_dir_no_error(self, tmp_path, monkeypatch):
        """状态目录不存在时不报错。"""
        nonexistent = tmp_path / "nope"
        orig = _ca_plugin._state_file_path
        _ca_plugin._state_file_path = lambda: nonexistent / "state.json"
        _ca_plugin._cleanup_stale_state_files()  # 无异常即通过
        _ca_plugin._state_file_path = orig

    # ── _write_state 集成 ──

    def test_write_state_also_cleans_stale_files(self, tmp_path):
        """_write_state 在写入前清理 stale 文件。"""
        (tmp_path / ".ca_assembler_state_9999999.json").write_text(
            json.dumps({"failures": 3, "retry_after": None})
        )

        _ca_plugin._write_state({"failures": 0, "retry_after": None})

        remaining = [p.name for p in tmp_path.iterdir()]
        assert not any("9999999" in n for n in remaining), "死 PID 文件应被清理"

        # 当前状态文件应存在（尽管 fixture 改名为 .test_breaker.json）
        state_file_found = any(".json" in n for n in remaining)
        assert state_file_found, "状态文件应被写入"

    def test_write_state_survives_cleanup_error(self, tmp_path, monkeypatch):
        """清理抛异常时不影响写入（try/except OSError 保护）。"""
        # 让 _cleanup_stale_state_files 抛异常
        orig = _ca_plugin._state_file_path
        _ca_plugin._state_file_path = lambda: tmp_path / "state.json"
        
        orig_cleanup = _ca_plugin._cleanup_stale_state_files
        def _broken_cleanup():
            raise OSError(13, "Permission denied")
        _ca_plugin._cleanup_stale_state_files = _broken_cleanup

        # 不应抛异常
        _ca_plugin._write_state({"failures": 0, "retry_after": None})

        # 状态文件写成了
        state_file = tmp_path / "state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["failures"] == 0

        _ca_plugin._cleanup_stale_state_files = orig_cleanup
        _ca_plugin._state_file_path = orig


@pytest.fixture(autouse=True)
def _plugin_state_dir(tmp_path):
    """所有插件测试默认用临时状态文件（~/.hermes 只读）。"""
    state_file = tmp_path / ".test_breaker.json"
    orig = _ca_plugin._state_file_path
    _ca_plugin._state_file_path = lambda: Path(str(state_file))
    yield
    _ca_plugin._state_file_path = orig


# ============================================================================
# REQ-FUNC-INTF: 插件生命周期
# ============================================================================


class TestLifecycle:
    """CAContextAssemblerPlugin 生命周期管理。"""

    def test_init_state(self):
        """初始时无引擎，无错误。"""
        plugin = CAContextAssemblerPlugin()
        assert plugin._engine is None
        assert plugin._engine_errored is False

    def test_on_session_start_failure_handling(self):
        """on_session_start 失败时设置错误标志或引擎降级。"""
        plugin = CAContextAssemblerPlugin()
        plugin.on_session_start(session_id="nonexistent_session_xyz")
        # 引擎可能被创建（缓存降级），也可能为 None；错误标志应被处理
        assert plugin._engine_errored or plugin._engine is not None
    
    def test_pre_llm_call_without_engine(self):
        """引擎不可用时 pre_llm_call 返回 None。"""
        plugin = CAContextAssemblerPlugin()
        result = plugin.pre_llm_call(user_message="hello")
        assert result is None

    def test_post_llm_call_without_engine(self):
        """引擎不可用时 post_llm_call 不报错。"""
        plugin = CAContextAssemblerPlugin()
        # 不应抛异常
        plugin.post_llm_call(user_message="hello", assistant_response="world", conversation_history=[])
        assert True

    def test_on_session_end_cleans_up(self):
        """on_session_end 清理引擎。"""
        plugin = CAContextAssemblerPlugin()
        plugin._session_id = "test_sid"

        with patch.object(_ca_plugin.session_manager, 'remove') as mock_remove:
            with patch.object(type(plugin), '_engine', create=True) as mock_engine:
                mock_engine.wait_for_pending = MagicMock()
                plugin._engine = mock_engine
                plugin.on_session_end()
                mock_remove.assert_called_once_with("test_sid")

    def test_on_session_reset_clears_error(self):
        """on_session_reset 清除错误状态。"""
        plugin = CAContextAssemblerPlugin()
        plugin._engine_errored = True
        plugin.on_session_reset()
        assert plugin._engine_errored is False


# ============================================================================
# REQ-FUNC-HOOKS: 上下文注入
# ============================================================================


class TestPreLlmCall:
    """pre_llm_call 钩子。"""

    def test_returns_none_when_errored(self):
        """引擎错误时返回 None。"""
        plugin = CAContextAssemblerPlugin()
        plugin._engine_errored = True
        result = plugin.pre_llm_call(user_message="hello", context_length=32000)
        assert result is None

    def test_returns_none_without_user_message(self):
        """无 user_message 时返回 None。"""
        plugin = CAContextAssemblerPlugin()
        result = plugin.pre_llm_call(user_message="", context_length=32000)
        assert result is None

    def test_extracts_ca_markers_from_assemble(self):
        """从 _compute_assemble_plan → _simple_mutation_mode 正常返回。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        # 模拟 _compute_assemble_plan 返回正常结果
        from ca import TurnPlanEntry, _AssemblePlanResult
        plan = [
            TurnPlanEntry(turn_index=1, turn_type="dialogue",
                          target_level="L1", decision_reason="middle",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
        ]
        result = _AssemblePlanResult(plan=plan, messages=[], stats=MagicMock(), tokens_before=10)
        mock_engine._compute_assemble_plan.return_value = result
        plugin._engine = mock_engine
        plugin._engine_errored = False

        # _simple_mutation_mode 需要 store 和 session_id
        from unittest.mock import MagicMock as MM
        mock_engine.store = MM()
        mock_engine.store.read_turn_texts.return_value = (None, "", "")
        mock_engine.store.read_tool_rows_for_group.return_value = []
        mock_engine.store.read_turn_biz_categories.return_value = {}
        mock_engine.store.read_session.return_value = []
        plugin._session_id = "test_sid"
        plugin._engine._format_tool_group_assembly.return_value = ""

        result = plugin.pre_llm_call(user_message="查询", context_length=32000,
                                     conversation_history=[
                                         {"role": "user", "content": "查询"},
                                         {"role": "assistant", "content": "结果"},
                                     ])
        # mutation mode 返回 None（history 已原地替换）
        assert result is None

    def test_assemble_failure_returns_none(self):
        """assemble 失败时返回 None。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        mock_engine.assemble.side_effect = RuntimeError("模拟失败")
        plugin._engine = mock_engine
        plugin._engine_errored = False

        result = plugin.pre_llm_call(user_message="查询", context_length=32000)
        assert result is None


class TestPostLlmCall:
    """post_llm_call 钩子 — PR2 新增 flush_tool_buffer 前置。"""

    def test_calls_flush_then_process(self):
        """先 flush_tool_buffer，再 process_turn_async，不再传 messages。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        mock_engine.flush_tool_buffer.return_value = 3
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        plugin.post_llm_call(
            user_message="查文件",
            assistant_response="查完了",
            conversation_history=[{"role": "user", "content": "查文件"}],
        )

        # 先 flush
        mock_engine.flush_tool_buffer.assert_called_once()
        # 再 process_turn_async（不传 messages）
        mock_engine.process_turn_async.assert_called_once()
        args, kwargs = mock_engine.process_turn_async.call_args
        assert "查文件" in args
        assert "查完了" in args
        # PR2: 不再传 messages 关键字参数
        assert "messages" not in kwargs

    def test_skipped_when_errored(self):
        """引擎错误时跳过。"""
        plugin = CAContextAssemblerPlugin()
        plugin._engine_errored = True
        plugin.post_llm_call(user_message="hi", assistant_response="yo", conversation_history=[])
        assert True  # 不抛异常

    def test_skipped_when_empty(self):
        """user_message 和 assistant_response 都空时跳过。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        plugin.post_llm_call(user_message="", assistant_response="", conversation_history=[])
        mock_engine.process_turn_async.assert_not_called()


class TestSnapshotRestore:
    """post_llm_call 快照恢复测试。"""

    def test_restores_content_after_replace(self):
        """替换内容后 snapshot 能恢复原始内容。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "content": "好的", "finish_reason": "stop"},
        ]
        # 模拟 _mutation_mode 设的快照
        plugin._saved_history_snapshot = [{**m} for m in history]
        # mutation_mode 已经修改了 content
        history[0]["content"] = "替换摘要"
        history[1]["content"] = "替换回复"

        # post_llm_call 应恢复
        plugin.post_llm_call(
            user_message="查文件",
            assistant_response="好的",
            conversation_history=history,
        )
        assert history[0]["content"] == "查文件", f"Expected original, got {history[0]['content']}"
        assert history[1]["content"] == "好的", f"Expected original, got {history[1]['content']}"
        assert plugin._saved_history_snapshot is None, "Snapshot should be cleared"

    def test_restores_skipped_rows(self):
        """被移除的行在 snapshot 恢复后应返回。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "tool_calls": [{}], "content": "thought"},
        ]
        plugin._saved_history_snapshot = [{**m} for m in history]
        # mutation_mode 移除了 assistant{tc} 行
        del history[1]

        plugin.post_llm_call(
            user_message="查文件",
            assistant_response="好的",
            conversation_history=history,
        )
        assert len(history) == 2, f"Expected 2 messages restored, got {len(history)}"
        assert history[1]["role"] == "assistant", "tool_calls row should be restored"
        assert "tool_calls" in history[1], "tool_calls should be restored"

    def test_legacy_saved_history_fallback(self):
        """无 snapshot 时使用旧 _saved_history fallback。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        msg = {"role": "user", "content": "被替换的内容"}
        history = [msg]
        plugin._saved_history = {id(msg): "查文件原文"}

        plugin.post_llm_call(
            user_message="查文件",
            assistant_response="好的",
            conversation_history=history,
        )
        assert msg["content"] == "查文件原文", f"Expected restored, got {msg['content']}"
        assert plugin._saved_history is None


# ============================================================================
# REQ-FUNC-INTF: register 钩子
# ============================================================================


class TestRegister:
    """register() 注册 8 个钩子。"""

    def test_registers_eight_hooks(self):
        """register 注册 5 个生命周期 + 3 个工具轮数据采集钩子。"""
        ctx = MagicMock()
        register(ctx)

        assert ctx.register_hook.call_count == 8
        hook_names = [call[0][0] for call in ctx.register_hook.call_args_list]
        assert "on_session_start" in hook_names
        assert "on_session_end" in hook_names
        assert "on_session_reset" in hook_names
        assert "pre_llm_call" in hook_names
        assert "post_llm_call" in hook_names
        assert "post_api_request" in hook_names
        assert "pre_tool_call" in hook_names
        assert "post_tool_call" in hook_names
