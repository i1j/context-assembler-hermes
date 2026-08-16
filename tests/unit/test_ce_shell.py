"""测试 CAContextEngine CE 壳 — 契约族（select_context 8 步/错误语义/R10 注入/session 标识）+ 反转用例（register 1 参/should_compress 恒 False）。
直接提取类定义而非加载全模块，避免相对导入问题。
"""

import importlib.util
import sys
import copy
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


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
ContextEngine = _ca_plugin.ContextEngine
_ce_engine = _ca_plugin._ce_engine
register = _ca_plugin.register
_engines = _ca_plugin._engines
_engines_lock = _ca_plugin._engines_lock


def _make_plugin(ca_engine, session_id: str = "test"):
    """创建 CAContextAssemblerPlugin 实例并绑定引擎"""
    plugin = _ca_plugin.CAContextAssemblerPlugin()
    plugin._engine = ca_engine
    plugin._session_id = session_id
    return plugin


@pytest.fixture
def real_engines_registry(tmp_path):
    """真实 _engines 注册表 fixture；按 sid 注册真实 ContextAssembler 并清理。

    T11 使用单 sid，T12 使用 sidA/sidB 两实例；注册 sid / 写行 sid / CE._session_id
    由调用方传入同一 sid 对齐。teardown 带锁清理，避免跨用例泄漏。
    """
    from ca import ContextAssembler

    created = []

    def _register(sid: str):
        db = tmp_path / f"{sid}.db"
        ca_engine = ContextAssembler(db_path=str(db), session_id=sid)
        plugin = _make_plugin(ca_engine, sid)
        with _engines_lock:
            _engines[sid] = plugin
        created.append((sid, ca_engine))
        return ca_engine

    yield _register

    for sid, ca_engine in reversed(created):
        with _engines_lock:
            _engines.pop(sid, None)
        ca_engine.destroy()


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
                   "compress", "select_context", "on_session_start", "on_session_end",
                   "on_session_reset", "get_status", "update_model"]
        for m in methods:
            assert hasattr(eng, m), f"Missing: {m}"
            assert callable(getattr(eng, m)), f"Not callable: {m}"

    def test_name_is_ca_assembler(self):
        eng = CAContextEngine()
        assert eng.name == "ca_assembler"

    def test_deepcopy_engine_smoke(self):
        """A40：CAContextEngine 实例可深拷贝（Hermes agent_init.py:2486 deepcopy 失败仅 warning 回退内置 compressor）。

        实例字段保持纯数据可拷贝——select_context 不新增锁/连接等不可拷贝字段。
        """
        import copy
        eng = CAContextEngine()
        cloned = copy.deepcopy(eng)
        assert cloned.name == "ca_assembler"
        assert cloned is not eng

    def test_on_session_end_removes_engine_registry(self, ca_engine):
        """真实会话边界由 CE.on_session_end 清账并移除 _engines，防长跑 gateway 泄漏。"""
        plugin = _make_plugin(ca_engine, "test")
        with _engines_lock:
            _engines["test"] = plugin
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce.on_session_end("test", [])
        assert plugin._engine is None
        with _engines_lock:
            assert "test" not in _engines

    def test_on_session_reset_clears_compress_guard(self):
        """跨会话不能复用 _last_compress_msg_len，否则新会话消息更少时 compress 被误跳过。"""
        ce = CAContextEngine()
        ce._last_compress_msg_len = 99
        ce.on_session_reset()
        assert ce._last_compress_msg_len == 0


class TestCachePreservation:
    def test_should_compress_returns_true_always(self):
        """反转：should_compress 恒 False（函数名保留自基线，防止收集数漂移）。"""
        eng = CAContextEngine()
        assert eng.should_compress() is False          # 无线索时也 False
        assert eng.should_compress(0) is False         # 零 token 也 False
        assert eng.should_compress(1_000_000) is False  # 大 token 也 False

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

    def test_register_context_engine_paused(self):
        """反转：register() 恢复 1 参 CE 壳注册（函数名保留自基线）。

        断言四点：恰 1 次、engine.name == "ca_assembler"、engine is _ce_engine、
        isinstance(engine, ContextEngine) 为真。
        """
        calls = []

        class MockCtx:
            def register_hook(self, name, fn):
                calls.append(("hook", name))

            def register_context_engine(self, engine):
                calls.append(("engine", engine))

        register(MockCtx())
        engine_regs = [c for c in calls if c[0] == "engine"]
        assert len(engine_regs) == 1
        engine = engine_regs[0][1]
        assert engine.name == "ca_assembler"
        assert engine is _ce_engine
        assert isinstance(engine, ContextEngine)

    def test_register_still_registers_8_hooks(self):
        calls = []

        class MockCtx:
            def register_hook(self, name, fn):
                calls.append(("hook", name))

            def register_context_engine(self, engine):
                calls.append(("engine", engine))

        register(MockCtx())
        hook_names = [c[1] for c in calls if c[0] == "hook"]
        expected = ["on_session_start", "on_session_end", "on_session_reset",
                    "pre_llm_call", "post_llm_call",
                    "post_api_request", "pre_tool_call", "post_tool_call"]
        assert len(hook_names) == len(expected)
        assert set(hook_names) == set(expected)


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
        write_turn_v5(ca_engine.store, "test", 2, 0,
                      role="user", elm_text="第二轮用户消息",
                      fct_text="第二轮用户消息",
                      hdl_text="第二轮")
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
        assert "你好原始内容" not in first_content, "FAR turn user 不应保留 Elm 原文"

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

    @pytest.mark.parametrize("bad_conv", [
        [],
        [{"role": "system", "content": "only-system"}],
        [{"bad": 1}],
        [{"role": "assistant", "content": "no-user"}],
    ])
    def test_compress_fail_open_on_invalid_build(self, ca_engine, bad_conv):
        """手动 /compress 与 select_context 同构：空/畸形/无 user 的重建结果不得替换真实消息。"""
        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = None
        ce = CAContextEngine()
        ce._get_plugin = MagicMock(return_value=plugin)
        plugin._engine._build_conv_history_v6 = MagicMock(return_value=bad_conv)

        msg = [{"role": "user", "content": "real user"}]
        result = ce.compress(msg)
        assert result is msg

    def test_T9_compress_force_and_guard(self, ca_engine):
        """T9：force 强制重建生效；消息未增长时守卫返回原列表对象。"""
        from ca.store import write_turn_v5
        from ca.grade import TopicGrade

        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="你好原始内容",
                      fct_text="你好原始内容",
                      hdl_text="你好")
        write_turn_v5(ca_engine.store, "test", 2, 0,
                      role="user", elm_text="第二轮用户消息",
                      fct_text="第二轮用户消息",
                      hdl_text="第二轮")
        write_turn_v5(ca_engine.store, "test", 3, 0,
                      role="user", elm_text="第三轮用户消息",
                      fct_text="第三轮用户消息",
                      hdl_text="第三轮")

        mock_mgr = MagicMock()
        mock_mgr.get_turn_grade.side_effect = lambda tn: {
            1: TopicGrade.FAR
        }.get(tn, TopicGrade.ACT)

        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = mock_mgr
        ce = CAContextEngine()
        ce._get_plugin = MagicMock(return_value=plugin)

        msg = [{"role": "user", "content": "你好原始内容"}]
        force_result = ce.compress(msg, force=True)
        assert force_result is not msg
        assert len(force_result[0].get("content", "")) < 100
        assert "你好" in force_result[0].get("content", "")
        assert "你好原始内容" not in force_result[0].get("content", "")

        guard_ce = CAContextEngine()
        guard_ce._get_plugin = MagicMock(return_value=plugin)
        first_result = guard_ce.compress(msg)
        assert first_result is not msg
        second_result = guard_ce.compress(msg)
        assert second_result is msg


class TestSelectContext:
    """select_context 契约族（T2-T6/T11-T14；T1 覆写 guard 为族外 aux）。"""

    def _write_user_turns(self, ca_engine, sid, contents):
        from ca.store import write_turn_v5

        for turn, content in enumerate(contents, 1):
            write_turn_v5(ca_engine.store, sid, turn, 0,
                          role="user", elm_text=content,
                          fct_text=f"FCT{turn}", hdl_text=f"HDL{turn}")

    def _write_three_user_turns(self, ca_engine, sid="test"):
        self._write_user_turns(
            ca_engine, sid,
            ["第一轮原始内容", "第二轮原始内容", "第三轮原始内容"],
        )

    def _make_mock_mgr(self, grades=None):
        from ca.grade import TopicGrade
        mgr = MagicMock()
        mgr.get_turn_grade.side_effect = (
            (lambda tn: grades.get(tn, TopicGrade.ACT)) if grades
            else (lambda tn: TopicGrade.ACT)
        )
        return mgr

    def test_aux_select_context_overridden_guard(self):
        """族外 guard：CAContextEngine.select_context 必须覆写基类方法。"""
        base_select = getattr(ContextEngine, "select_context", None)
        if base_select is not None:
            assert CAContextEngine().select_context.__func__ is not base_select
        eng = CAContextEngine()
        assert callable(eng.select_context)

    def test_T2_select_context_matches_build_and_system_first(self, ca_engine):
        """T1+T2：非空 DB 重建契约、system 保留、turn1 Hdl 与参考参数不可变。

        expected 归一化镜像实现 ⑦ R10 替换语义（reversed 末 user 替换），实现变更须同步。
        """
        self._write_three_user_turns(ca_engine)
        from ca.grade import TopicGrade
        mgr = self._make_mock_mgr(
            {1: TopicGrade.FAR, 2: TopicGrade.ACT, 3: TopicGrade.ACT},
        )
        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = mgr
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)

        request_messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "current injected [CA_WIKI]"},
        ]
        conversation_messages = [{"role": "user", "content": "hist"}]
        conversation_before = copy.deepcopy(conversation_messages)
        incoming_message = {"role": "assistant", "content": "assistant-in"}
        budget_tokens = 123

        expected = plugin._engine._build_conv_history_v6(
            plugin._topic_mgr,
            system_message=request_messages[0],
        )
        result = ce.select_context(
            request_messages,
            conversation_messages=conversation_messages,
            incoming_message=incoming_message,
            budget_tokens=budget_tokens,
        )

        assert result is not request_messages
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "SYS"
        expected_normalized = [dict(m) for m in expected]
        for m in reversed(expected_normalized):
            if m.get("role") == "user":
                m["content"] = request_messages[-1]["content"]
                break
        assert result == expected_normalized
        assert result[-1]["content"] == "current injected [CA_WIKI]"

        user_messages = [m for m in result if m.get("role") == "user"]
        assert user_messages
        assert "第一轮原始内容" not in user_messages[0].get("content", "")
        assert "HDL1" in user_messages[0].get("content", "")

        assert conversation_messages == conversation_before
        assert incoming_message == {"role": "assistant", "content": "assistant-in"}
        assert budget_tokens == 123

    def test_T13_bg_review_returns_none(self):
        ce = CAContextEngine()
        ce.on_session_start("test")
        with patch("tools.skill_provenance.get_current_write_origin",
                   return_value="background_review"):
            result = ce.select_context([{"role": "user", "content": "bg"}])
        assert result is None

    @pytest.mark.parametrize("request_messages", [
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
        [{"role": "user", "content": "hi"}],
    ])
    def test_T3_empty_store_returns_none(self, ca_engine, request_messages):
        plugin = _make_plugin(ca_engine, "test")
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)
        with patch("tools.skill_provenance.get_current_write_origin",
                   return_value="foreground"):
            assert ce.select_context(request_messages) is None

    @pytest.mark.parametrize("failure_mode", [
        "plugin_missing",
        "engine_errored",
        "engine_none",
        "build_raises",
    ])
    def test_T4_fail_open_returns_request_messages(self, ca_engine, failure_mode):
        request_messages = [{"role": "user", "content": "hello"}]
        ce = CAContextEngine()
        ce.on_session_start("test")

        if failure_mode == "plugin_missing":
            with _engines_lock:
                _engines.pop("test", None)
            assert ce._get_plugin() is None
            # 不注册 _engines：真实 _get_plugin 找不到 plugin，短路 fail-open
            result = ce.select_context(request_messages)
        else:
            plugin = _make_plugin(ca_engine, "test")
            if failure_mode == "engine_errored":
                plugin._engine_errored = True
            elif failure_mode == "engine_none":
                plugin._engine = None
            else:
                from ca.store import write_turn_v5
                write_turn_v5(ca_engine.store, "test", 1, 0,
                              role="user", elm_text="hello")
                ce._get_plugin = MagicMock(return_value=plugin)
                with patch.object(plugin._engine, "_build_conv_history_v6",
                                  side_effect=RuntimeError("boom")):
                    result = ce.select_context(request_messages)
                assert result is request_messages
                return

            ce._get_plugin = MagicMock(return_value=plugin)
            result = ce.select_context(request_messages)

        assert result is request_messages

    def test_T5_empty_list_returns_request_messages(self, ca_engine):
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="hello")
        ca_engine._build_conv_history_v6 = MagicMock(return_value=[])
        plugin = _make_plugin(ca_engine, "test")
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)
        request_messages = [{"role": "user", "content": "hello"}]
        assert ce.select_context(request_messages) is request_messages

    @pytest.mark.parametrize("invalid", [
        [{"bad": 1}],
        [{"role": "user"}, 123],
    ])
    def test_T6_invalid_dict_returns_request_messages(self, ca_engine, invalid):
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="hello")
        ca_engine._build_conv_history_v6 = MagicMock(return_value=invalid)
        plugin = _make_plugin(ca_engine, "test")
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)
        request_messages = [{"role": "user", "content": "hello"}]
        assert ce.select_context(request_messages) is request_messages

    def test_T15_tool_round_wire_shape_and_current_user(self, ca_engine):
        """工具轮：当前 user 不在 request 尾部仍被 R10 替换；重建消息符合 OpenAI 线上 schema。"""
        import json
        from ca.store import write_turn_v5
        from ca.grade import TopicGrade

        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="第一轮")
        write_turn_v5(ca_engine.store, "test", 1, 1,
                      role="assistant", elm_text="第一轮回复",
                      finish_reason="stop")
        write_turn_v5(ca_engine.store, "test", 2, 0,
                      role="user", elm_text="当前轮原始")
        write_turn_v5(
            ca_engine.store, "test", 2, 1,
            role="assistant", elm_text="thinking", fct_text="thought",
            finish_reason="tool_calls",
            tool_calls_json=json.dumps([
                {"id": "call_1", "type": "function",
                 "function": {"name": "bash", "arguments": {"cmd": "ls"}}}
            ], ensure_ascii=False),
        )
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", elm_text="a.txt", tool_name="bash",
                      tool_call_id="call_1")

        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = self._make_mock_mgr({1: TopicGrade.ACT, 2: TopicGrade.ACT})
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)

        request_messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "第一轮回复"},
            {"role": "user", "content": "当前轮 [CA_WIKI] 注入"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}]},
            {"role": "tool", "content": "a.txt", "tool_call_id": "call_1"},
        ]
        result = ce.select_context(request_messages)

        users = [m for m in result if m.get("role") == "user"]
        assert users[-1]["content"] == "当前轮 [CA_WIKI] 注入"

        thought = result[-2]
        assert thought["role"] == "assistant"
        assert "finish_reason" not in thought, "select_context 返回值绕过 Hermes 正常路径的 finish_reason pop，不能携带该字段"
        raw_args = thought["tool_calls"][0]["function"]["arguments"]
        assert isinstance(raw_args, str), f"arguments 必须是 str，got {type(raw_args).__name__}"
        assert json.loads(raw_args) == {"cmd": "ls"}

        tool_msg = result[-1]
        assert tool_msg["tool_name"] == "bash"
        assert "name" not in tool_msg, "tool 消息不允许携带 name 字段"

    def test_T16_bg_only_rows_fallback_to_request_messages(self, ca_engine):
        """DB 仅有 bg_review 行 → 重建结果无 user，必须 fail-open 保留 request_messages。"""
        from ca.store import write_turn_v5

        write_turn_v5(ca_engine.store, "test", 1, 0,
                      role="user", elm_text="bg row", biz_category="bg_review")
        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = self._make_mock_mgr()
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)

        request_messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "real current user"},
        ]
        result = ce.select_context(request_messages)
        assert result is request_messages, (
            f"bg-only DB 下重建结果无 user 行，不得替换 request_messages，got {result!r}"
        )

    def test_T11_real_engine_registry_hit(self, real_engines_registry):
        sid = "sid"
        sid2 = "sid2"
        ca_engine = real_engines_registry(sid)
        ca_engine2 = real_engines_registry(sid2)

        self._write_user_turns(ca_engine, sid, ["sid-A-turn1", "sid-A-turn2"])
        self._write_user_turns(ca_engine2, sid2, ["sid2-B-turn1", "sid2-B-turn2"])
        with _engines_lock:
            _engines[sid]._topic_mgr = self._make_mock_mgr()
            _engines[sid2]._topic_mgr = self._make_mock_mgr()

        ce = CAContextEngine()
        ce.on_session_start(sid)
        request_messages = [{"role": "user", "content": "current user A"}]
        result = ce.select_context(request_messages)
        assert result is not request_messages
        assert result[-1]["content"] == "current user A"
        assert any(
            "sid-A-turn1" in m.get("content", "")
            for m in result if m.get("role") == "user"
        )
        assert not any(
            "sid2-B-turn1" in m.get("content", "")
            for m in result if m.get("role") == "user"
        )

        ce.on_session_start(sid2)
        assert ce._session_id == sid2
        request_messages2 = [{"role": "user", "content": "current user B"}]
        result2 = ce.select_context(request_messages2)
        assert result2 is not request_messages2
        assert result2[-1]["content"] == "current user B"
        assert any(
            "sid2-B-turn1" in m.get("content", "")
            for m in result2 if m.get("role") == "user"
        )

    def test_T12_dual_session_isolation(self, real_engines_registry):
        sid_a = "sidA"
        sid_b = "sidB"
        ca_engine_a = real_engines_registry(sid_a)
        ca_engine_b = real_engines_registry(sid_b)

        self._write_user_turns(ca_engine_a, sid_a, ["A_DATA_ONE", "A_DATA_TWO"])
        self._write_user_turns(ca_engine_b, sid_b, ["B_DATA_ONE", "B_DATA_TWO"])
        with _engines_lock:
            _engines[sid_a]._topic_mgr = self._make_mock_mgr()
            _engines[sid_b]._topic_mgr = self._make_mock_mgr()

        ce = CAContextEngine()

        ce.on_session_start(sid_a)
        request_a = [{"role": "user", "content": "current-A"}]
        result_a = ce.select_context(request_a)

        ce.on_session_start(sid_b)
        request_b = [{"role": "user", "content": "current-B"}]
        result_b = ce.select_context(request_b)

        def user_contents(result):
            return [
                m.get("content", "")
                for m in result if m.get("role") == "user"
            ]

        assert result_a is not request_a
        assert result_b is not request_b
        assert any("A_DATA_ONE" in c for c in user_contents(result_a))
        assert not any("B_DATA_" in c for c in user_contents(result_a))
        assert any("B_DATA_ONE" in c for c in user_contents(result_b))
        assert not any("A_DATA_" in c for c in user_contents(result_b))
        assert result_a[-1]["content"] == "current-A"
        assert result_b[-1]["content"] == "current-B"

    def test_aux_conftest_import_path(self):
        """族外辅助用例：验证 tests/unit/conftest.py 注入的 Hermes 路径可导入。"""
        from tools.skill_provenance import get_current_write_origin
        assert callable(get_current_write_origin)

    def test_T14_injected_current_user_content_preserved(self, ca_engine):
        self._write_three_user_turns(ca_engine)
        from ca.grade import TopicGrade
        plugin = _make_plugin(ca_engine, "test")
        plugin._topic_mgr = self._make_mock_mgr(
            {1: TopicGrade.FAR, 2: TopicGrade.ACT, 3: TopicGrade.ACT},
        )
        ce = CAContextEngine()
        ce.on_session_start("test")
        ce._get_plugin = MagicMock(return_value=plugin)

        request_messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "[CA_WIKI] injected current turn"},
        ]
        result = ce.select_context(request_messages)
        users = [m for m in result if m.get("role") == "user"]
        assert users
        assert users[-1]["content"] == "[CA_WIKI] injected current turn"

        # 防御边界：尾部无 user 消息时不执行替换
        system_only = [{"role": "system", "content": "SYS2"}]
        expected_system_only = plugin._engine._build_conv_history_v6(
            plugin._topic_mgr,
            system_message=system_only[0],
        )
        result_system_only = ce.select_context(system_only)
        assert result_system_only == expected_system_only
        assert result_system_only is not system_only
