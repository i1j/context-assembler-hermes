"""测试 CAContextEngine CE 壳 — 重点验证缓存影响。
直接提取类定义而非加载全模块，避免相对导入问题。
"""

import importlib.util
import sys
import pytest
from pathlib import Path


def _load_ca_plugin():
    """加载 __init__.py 为独立模块（与 test_plugin.py 一致）。"""
    _root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(_root))

    # 预加载依赖模块 → 挂载为 ca_assembler_plugin 的子模块
    # 使相对导入 from .topic_manager import ... 正确解析
    import topic_manager as _tm
    sys.modules["ca_assembler_plugin.topic_manager"] = _tm

    _file = _root / "__init__.py"
    _spec = importlib.util.spec_from_file_location("ca_assembler_plugin", _file)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["ca_assembler_plugin"] = _mod
    _spec.loader.exec_module(_mod)
    return _mod


_ca_plugin = _load_ca_plugin()
CAContextEngine = _ca_plugin.CAContextEngine
_ce_engine = _ca_plugin._ce_engine
register = _ca_plugin.register


class TestCEProtocol:
    def test_required_fields_present(self):
        eng = CAContextEngine()
        fields = ["name", "last_prompt_tokens", "last_completion_tokens",
                  "last_total_tokens", "threshold_tokens", "context_length",
                  "compression_count", "protect_first_n", "protect_last_n",
                  "threshold_percent"]
        for f in fields:
            assert hasattr(eng, f), f"Missing protocol field: {f}"

    def test_required_methods_present(self):
        eng = CAContextEngine()
        methods = ["update_from_response", "should_compress",
                   "compress", "on_session_start", "on_session_end",
                   "on_session_reset", "get_status", "update_model"]
        for m in methods:
            assert hasattr(eng, m), f"Missing: {m}"
            assert callable(getattr(eng, m)), f"Not callable: {m}"

    def test_name_is_ca_assembler(self):
        eng = CAContextEngine()
        assert eng.name == "ca_assembler"


class TestCachePreservation:
    def test_should_compress_returns_false(self):
        eng = CAContextEngine()
        assert eng.should_compress() is False
        assert eng.should_compress(100_000) is False

    def test_compress_noop_when_no_plugin(self):
        eng = CAContextEngine()
        messages = [{"role": "user", "content": "hello"}]
        result = eng.compress(messages)
        assert result is messages
        assert len(result) == 1

    def test_compress_returns_list(self):
        eng = CAContextEngine()
        result = eng.compress([])
        assert isinstance(result, list)


class TestRegister:
    def test_register_context_engine_called(self):
        calls = []

        class MockCtx:
            def register_hook(self, name, fn):
                calls.append(("hook", name))

            def register_context_engine(self, name, engine):
                calls.append(("engine", name, engine))

        register(MockCtx())
        engine_regs = [c for c in calls if c[0] == "engine"]
        assert len(engine_regs) == 1
        assert engine_regs[0][1] == "ca_assembler"
        assert isinstance(engine_regs[0][2], CAContextEngine)

    def test_register_still_registers_8_hooks(self):
        calls = []

        class MockCtx:
            def register_hook(self, name, fn):
                calls.append(("hook", name))

            def register_context_engine(self, name, engine):
                calls.append(("engine", name))

        register(MockCtx())
        hook_names = [c[1] for c in calls if c[0] == "hook"]
        assert len(hook_names) == 8
        expected = ["on_session_start", "on_session_end", "on_session_reset",
                    "pre_llm_call", "post_llm_call",
                    "post_api_request", "pre_tool_call", "post_tool_call"]
        assert hook_names == expected
