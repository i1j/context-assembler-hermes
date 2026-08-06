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


def _make_plugin(ca_engine, session_id: str = "test"):
    """创建 CAContextAssemblerPlugin 实例并绑定引擎"""
    plugin = _ca_plugin.CAContextAssemblerPlugin()
    plugin._engine = ca_engine
    plugin._session_id = session_id
    return plugin


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
    def test_should_compress_returns_true_always(self):
        eng = CAContextEngine()
        assert eng.should_compress() is True       # 无线索时也 True
        assert eng.should_compress(0) is True       # 零 token 也 True
        assert eng.should_compress(1_000_000) is True  # 大 token 也 True

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


class TestCompressGuard:
    """E2E guard：验证 CE 管线实际调用 _build_conv_history_v6 压缩消息。

    如果这个测试失败，说明 compress() 断线了（should_compress→False 或 no-op）。
    """

    def test_compress_with_plugin_returns_compressed(self, ca_engine):
        """compress() 通过 plugin 调 _build_conv_history_v6 → 返回压缩后的消息。"""
        from ca.store import write_turn_v5
        from ca.grade import TopicGrade
        from unittest.mock import MagicMock

        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="你好原始内容",
                      fct_text="你好原始内容",
                      hdl_text="你好")
        write_turn_v5(ca_engine.store, "test", 3, 0,
                      role="user", elm_text="第三轮用户消息",
                      fct_text="第三轮用户消息",
                      hdl_text="第三轮")

        mock_mgr = MagicMock()
        mock_mgr.get_turn_grade.side_effect = lambda tn: {
            1: TopicGrade.FAR, 3: TopicGrade.ACT
        }.get(tn, TopicGrade.ACT)

        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = mock_mgr
        ce = CAContextEngine()
        ce._get_plugin = MagicMock(return_value=plugin)

        msg = [{"role": "user", "content": "第三轮用户消息"}]
        result = ce.compress(msg)
        assert result is not msg, "compress() 不应返回同一个列表对象"
        first_content = result[0].get("content", "")
        assert len(first_content) < 100, f"FAR turn user 应压缩为 Hdl，但长达 {len(first_content)}"
        assert "你好" in first_content, f"Hdl 应保留原文摘要"

    def test_compress_with_plugin_tail_protected(self, ca_engine):
        """尾部保护区最后 2 user turn 保留 Elm 原文。"""
        from ca.store import write_turn_v5
        from ca.grade import TopicGrade
        from unittest.mock import MagicMock

        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="第一轮长文本原始内容",
                      fct_text="FCT1", hdl_text="HDL1")
        write_turn_v5(ca_engine.store, "test", 2, 0,
                      role="user", elm_text="第二轮",
                      fct_text="FCT2", hdl_text="HDL2")
        write_turn_v5(ca_engine.store, "test", 3, 0,
                      role="user", elm_text="第三轮原始内容",
                      fct_text="FCT3", hdl_text="HDL3")

        mock_mgr = MagicMock()
        mock_mgr.get_turn_grade.side_effect = lambda tn: TopicGrade.FAR

        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = mock_mgr
        ce = CAContextEngine()
        ce._get_plugin = MagicMock(return_value=plugin)

        msg = [{"role": "user", "content": "第三轮原始内容"}]
        result = ce.compress(msg)

        tail_msgs = [m for m in result if m.get("role") == "user"]
        for m in tail_msgs[-2:]:
            c = m.get("content", "")
            assert "FCT" not in c, f"tail 保护区不应被 Fct 替换: {c}"
            assert "HDL" not in c, f"tail 保护区不应被 Hdl 替换: {c}"
