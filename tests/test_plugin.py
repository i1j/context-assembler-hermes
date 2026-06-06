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
        """从 assemble 结果中提取 [~/N/0] 标记。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        mock_engine.assemble.return_value = [
            {"role": "assistant", "content": "[~/1/0] 摘要A"},
            {"role": "assistant", "content": "[~/2/1] search: ok"},
            {"role": "user", "content": "普通消息（不应提取）"},
        ]
        plugin._engine = mock_engine
        plugin._engine_errored = False

        result = plugin.pre_llm_call(user_message="查询", context_length=32000)
        assert result is not None
        assert "[~/1/0] 摘要A" in result
        assert "[~/2/1] search: ok" in result
        assert "普通消息" not in result

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
    """post_llm_call 钩子。"""

    def test_calls_process_turn_async(self):
        """调用 engine.process_turn_async 传递正确的参数。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        plugin.post_llm_call(
            user_message="你好",
            assistant_response="好的",
            conversation_history=[{"role": "user", "content": "你好"}],
        )

        mock_engine.process_turn_async.assert_called_once()
        args, kwargs = mock_engine.process_turn_async.call_args
        assert "你好" in args
        assert "好的" in args

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


# ============================================================================
# REQ-FUNC-INTF: register 钩子
# ============================================================================


class TestRegister:
    """register() 注册 5 个钩子。"""

    def test_registers_five_hooks(self):
        """register 注册 5 个生命周期钩子。"""
        ctx = MagicMock()
        register(ctx)

        assert ctx.register_hook.call_count == 5
        hook_names = [call[0][0] for call in ctx.register_hook.call_args_list]
        assert "on_session_start" in hook_names
        assert "on_session_end" in hook_names
        assert "on_session_reset" in hook_names
        assert "pre_llm_call" in hook_names
        assert "post_llm_call" in hook_names
