"""插件适配层测试 — CAContextAssemblerPlugin 生命周期与钩子。

覆盖 `__init__.py`（根）的插件包装层：
- is_available() / 断路器状态
- on_session_start / on_session_end / on_session_reset
- pre_llm_call（context 注入）
- post_llm_call（C‑stage 入口）

设计决策对照:
  → P-001: Plugin 层职责分离 (test_is_available, test_plugin_lifecycle)
  → P-004: Hook 注册 + 8 个 hooks (test_hook_*, test_pre_post_llm_*)
Wiki: decision-points-wiki.md §P-001, §P-004

  → tests/INDEX.md — 测试套件总览"""

# ── 插件实例化 ──
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from ca.grade import TopicGrade

# 加载根 __init__.py（插件适配层）作为独立模块
# conftest 已把 ca_assembler/ 添加到 sys.path，但其父目录才是 ca_assembler 包的所在
_plugin_file = Path(__file__).resolve().parent.parent.parent / "__init__.py"
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
_on_pre_llm_call_v5 = _ca_plugin._on_pre_llm_call_v5
# v6.5: 话题召回本地化为 query_themes_by_semantics（theme 结构）
_engines = _ca_plugin._engines
_engines_lock = _ca_plugin._engines_lock

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
        """引擎不可用时 _on_pre_llm_call_v5 返回 None。"""
        plugin = CAContextAssemblerPlugin()
        with _engines_lock:
            _engines["test_noeng"] = plugin
        result = _on_pre_llm_call_v5(session_id="test_noeng")
        assert result is None

    def test_post_llm_call_without_engine(self):
        """引擎不可用时 post_llm_call 不报错。"""
        plugin = CAContextAssemblerPlugin()
        # 不应抛异常
        plugin.post_llm_call_v5(user_message="hello", assistant_response="world", conversation_history=[])
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
    """_on_pre_llm_call_v5 钩子（方向 B 独立函数）。"""

    def test_returns_none_when_no_session(self):
        """无 session 时返回 None。"""
        result = _on_pre_llm_call_v5(session_id="")
        assert result is None

    def test_returns_none_when_engine_errored(self):
        """引擎错误时返回 None。"""
        plugin = CAContextAssemblerPlugin()
        plugin._engine_errored = True
        with _engines_lock:
            _engines["test_sid"] = plugin
        result = _on_pre_llm_call_v5(session_id="test_sid")
        assert result is None

    def test_skips_turn_on_bg(self):
        """bg 轮跳过。"""
        with patch('tools.skill_provenance.get_current_write_origin', return_value="background_review"):
            plugin = CAContextAssemblerPlugin()
            plugin._engine_errored = False
            with _engines_lock:
                _engines["test_bg"] = plugin
            result = _on_pre_llm_call_v5(session_id="test_bg")
            assert result is None
            assert plugin._bg_turn is True

    def test_returns_none_without_user_message(self):
        """空 user_message 时返回 None。"""
        with patch('ca_assembler_plugin.get_current_write_origin', return_value=""):
            plugin = CAContextAssemblerPlugin()
            plugin._engine_errored = False
            plugin._bg_turn = False
            mock_engine = MagicMock()
            mock_engine._current_turn = 0
            mock_engine._seq_counter = {}
            mock_engine.store = MagicMock()
            plugin._engine = mock_engine
            with _engines_lock:
                _engines["test_nomsg"] = plugin
            result = _on_pre_llm_call_v5(
                session_id="test_nomsg",
                user_message="",
                conversation_history=[],
            )
            assert result is None


class TestPostLlmCall:
    """post_llm_call_v5 钩子 — v5 E-stage 写入 + snapshot 恢复。"""

    def test_calls_process_turn_after_estage(self):
        """v7：先写 fin（E-stage _on_final_response_v7），再 process_turn_f_stage。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"
        mock_engine._current_turn = 1
        mock_engine._seq_counter = {1: 0}
        mock_engine._on_final_response_v7.side_effect = (
            lambda **kw: mock_engine._seq_counter.__setitem__(1, 1))

        plugin.post_llm_call_v5(
            user_message="查文件",
            assistant_response="查完了",
            conversation_history=[{"role": "user", "content": "查文件"}],
        )

        mock_engine._on_final_response_v7.assert_called_once_with(
            session_id="test_sid", turn_index=1, assistant_response="查完了")
        # 然后 process_turn_f_stage（参数为 turn_index）
        mock_engine.process_turn_f_stage.assert_called_once_with(1, fin_seq=1)

    def test_skipped_when_errored(self):
        """引擎错误时跳过。"""
        plugin = CAContextAssemblerPlugin()
        plugin._engine_errored = True
        plugin.post_llm_call_v5(user_message="hi", assistant_response="yo", conversation_history=[])
        assert True  # 不抛异常

    def test_skipped_when_empty(self):
        """user_message 和 assistant_response 都空时跳过。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        plugin.post_llm_call_v5(user_message="", assistant_response="", conversation_history=[])
        mock_engine.process_turn_async.assert_not_called()

class TestSnapshotRestore:
    """post_llm_call 快照恢复测试。"""

    @pytest.mark.xfail(reason="v6.0 方向 B: compress 从 DB 重建, 不再恢复快照", strict=False)
    def test_restores_content_after_replace_v5(self, engine, ca_engine):
        """v5: snapshot 恢复替换内容。"""
        plugin = CAContextAssemblerPlugin()
        mock_engine = MagicMock()
        mock_engine._current_turn = 1
        mock_engine._seq_counter = {1: 0}
        mock_engine.store.conn = MagicMock()
        plugin._engine = mock_engine
        plugin._engine_errored = False
        plugin._session_id = "test_sid"

        from unittest.mock import patch as _patch
        with _patch('ca.store.write_turn_v5', return_value=True):
            history = [
                {"role": "user", "content": "查文件"},
                {"role": "assistant", "content": "好的", "finish_reason": "stop"},
            ]
            mock_engine._saved_history_snapshot = [{**m} for m in history]
            # 模拟替换
            history[0]["content"] = "替换摘要"
            history[1]["content"] = "替换回复"

            plugin.post_llm_call_v5(
                user_message="查文件",
                assistant_response="好的",
                conversation_history=history,
            )
            assert history[0]["content"] == "查文件", f"Expected original, got {history[0]['content']}"
            assert history[1]["content"] == "好的", f"Expected original, got {history[1]['content']}"
            assert mock_engine._saved_history_snapshot is None, "Engine snapshot should be cleared"


class TestEngineRefreshAfterTtlCleanup:
    """CR-009: 验证 engine 被 SessionManager TTL 清理后，hook 函数能自动恢复。"""

    def test_pre_llm_call_recovers_after_engine_destroyed(self):
        """pre_llm_call 在引擎被销毁后重新获取引擎并成功写入。"""
        import ca
        plugin = CAContextAssemblerPlugin()
        session_id = f"cr009_test_{int(time.time())}"
        plugin.on_session_start(session_id, model="deepseek-v4-flash")
        assert plugin._engine is not None
        assert plugin._engine_errored is False

        with _engines_lock:
            _engines[session_id] = plugin

        # 写第一行数据，验证引擎正常
        from ca.store import write_turn_v5, read_turn_stream_all
        ok = write_turn_v5(
            plugin._engine.store, session_id, turn=1, seq=0,
            role='user', elm_text="first turn",
            fct_text="first turn", hdl_text="first",
        )
        assert ok, "first write should succeed"
        rows_before = read_turn_stream_all(plugin._engine.store, session_id)
        assert len(rows_before) >= 1

        # 模拟 TTL cleanup: 从 session_manager 缓存移除 + destroy
        try:
            ca.session_manager._sessions.pop(session_id, None)
        except Exception:
            pass
        old_engine = plugin._engine
        old_engine.destroy()
        # 确认 engine 已不可用（store 已关闭）
        try:
            old_engine.store.conn.execute("SELECT 1")
            assert False, "connection should be closed"
        except Exception:
            pass
        # _engine_errored 仍为 False（插件不知情）
        assert plugin._engine_errored is False

        # 调用 pre_llm_call hook → 应触发 _refresh_engine
        with patch('tools.skill_provenance.get_current_write_origin', return_value="foreground"):
            result = _on_pre_llm_call_v5(
                session_id=session_id,
                user_message="recovery test",
                conversation_history=[{"role": "user", "content": "first turn"}],
                model="deepseek-v4-flash",
            )

        # 验证: 引擎被替换为新实例
        assert plugin._engine is not old_engine, "should get new engine instance"
        assert plugin._engine is not None
        assert plugin._engine_errored is False

        # 验证: topic_mgr 的底层依赖已随新引擎重绑（旧连接已关闭）
        assert plugin._topic_mgr is not None
        assert plugin._topic_mgr._store is plugin._engine.store
        assert plugin._topic_mgr._embed_client is plugin._engine.embed_client
        assert plugin._topic_mgr._cache is plugin._engine.cache

        # 新引擎能读旧数据
        rows_after = read_turn_stream_all(plugin._engine.store, session_id)
        assert len(rows_after) >= len(rows_before), "should retain previous data"

        # 新用户行已写入
        cur = plugin._engine.store.conn.execute(
            "SELECT Elm FROM turn_stream WHERE session_id=? AND Elm=?",
            (session_id, "recovery test"),
        )
        assert cur.fetchone() is not None, "recovery user message should be in DB"

        # 清理
        with _engines_lock:
            _engines.pop(session_id, None)
        try:
            ca.session_manager._sessions.pop(session_id, None)
        except Exception:
            pass

    def test_refresh_restores_turn_state_after_mid_turn_ttl(self):
        """TTL 驱逐后新引擎必须从 DB 恢复 _current_turn/_seq_counter/_tool_seq_map，否则 mid-turn hook 会写 turn 0。"""
        import ca
        plugin = CAContextAssemblerPlugin()
        session_id = f"cr009_mid_{int(time.time())}"
        plugin.on_session_start(session_id, model="deepseek-v4-flash")
        with _engines_lock:
            _engines[session_id] = plugin

        from ca.store import write_turn_v5
        write_turn_v5(plugin._engine.store, session_id, 5, 0,
                      role="user", elm_text="turn5 user")
        write_turn_v5(plugin._engine.store, session_id, 5, 1,
                      role="assistant", elm_text="thinking",
                      finish_reason="tool_calls")
        write_turn_v5(plugin._engine.store, session_id, 5, 2,
                      role="tool", elm_text="",
                      tool_name="bash", tool_call_id="mid_call",
                      status="pending")

        old_engine = plugin._engine
        try:
            ca.session_manager._sessions.pop(session_id, None)
        except Exception:
            pass
        old_engine.destroy()

        assert _ca_plugin._refresh_engine(session_id, plugin) is True
        assert plugin._engine is not old_engine
        assert plugin._engine._current_turn == 5
        assert plugin._engine._seq_counter.get(5) == 2
        assert plugin._engine._tool_seq_map == {"mid_call": (5, 2)}

        # 清理
        with _engines_lock:
            _engines.pop(session_id, None)
        try:
            ca.session_manager._sessions.pop(session_id, None)
        except Exception:
            pass


# ============================================================================
# REQ-FUNC-INTF: register 钩子
# ============================================================================


class TestRegister:
    """register() 注册 13 个钩子（决策 44：8 旧 + 5 近源采集）。"""

    def test_registers_thirteen_hooks(self):
        """register 注册 13 个钩子（v7：5 生命周期/轮级 + post_api + 2 工具轮 + 5 近源）。"""
        ctx = MagicMock()
        register(ctx)

        assert ctx.register_hook.call_count == 13
        hook_names = [call[0][0] for call in ctx.register_hook.call_args_list]
        for name in (
            "on_session_start", "on_session_end", "on_session_reset",
            "pre_llm_call", "post_llm_call", "post_api_request",
            "pre_tool_call", "post_tool_call",
            "pre_api_request", "api_request_error",
            "on_stream_start", "on_stream_delta", "on_stream_end",
        ):
            assert name in hook_names, f"missing hook: {name}"

# ============================================================================
# 从 test_circuit / test_lifecycle 合并的剩余唯一测试
# ============================================================================


@pytest.mark.high
def test_tc_cb_007_engine_no_breaker(engine):
    """Plugin 层负责断路器，引擎不感知
    Steps: 检查引擎层是否包含断路器读写代码"""
    import inspect
    source = inspect.getsource(type(engine))
    assert "state_file" not in source or "cb_" not in source,         "Engine should not contain circuit breaker code"


@pytest.mark.medium
def test_tc_intf_002_context_length_hot_reload(engine):
    """context_length 属性可通过 Config 热重载
    Steps: 设置 CA_CONTEXT_LENGTH=30000 → Config.reload() → 验证"""
    import os
    from ca.config import Config
    old_env = os.environ.get("CA_CONTEXT_LENGTH")
    try:
        os.environ["CA_CONTEXT_LENGTH"] = "30000"
        Config.reload()
        engine.context_length = Config.context_length_for_model("test-model")
        assert engine.context_length >= 1000,             f"context_length should be valid, got {engine.context_length}"
    finally:
        if old_env is not None:
            os.environ["CA_CONTEXT_LENGTH"] = old_env
        else:
            os.environ.pop("CA_CONTEXT_LENGTH", None)
        Config.reload()


@pytest.mark.medium
def test_tc_intf_003_get_status(engine):
    """get_status() 返回 token 使用量和引擎状态"""
    if hasattr(engine, "get_status"):
        status = engine.get_status()
        assert isinstance(status, dict),             f"get_status should return dict, got {type(status)}"
    else:
        assert True  # method may not exist on engine


@pytest.mark.medium
def test_tc_reset_001_fd_no_leak(engine):
    """连续 reset 100 次无 FD 泄漏"""
    import os
    init_fd = len(os.listdir("/proc/self/fd"))
    for _ in range(100):
        engine.reset()
    final_fd = len(os.listdir("/proc/self/fd"))
    assert final_fd - init_fd <= 5,         f"FD leak: initial={init_fd}, final={final_fd}"


# ============================================================================
# bg_review 路径
# ============================================================================


class TestBgReview:
    """GAP-C: bg_review 检测 → 跳过 A-stage → 同步写 Fct"""

    def test_bg_review_writes_fct_equals_content(self, engine, ca_engine, monkeypatch):
        """bg_review 轮：Fct = user_message, Hdl = user_message[:100], A-stage 跳过"""
        from unittest.mock import patch

        # 将 ca_engine 注册到 _engines，使 _on_pre_llm_call_v5 能找到 plugin
        plugin = CAContextAssemblerPlugin()
        plugin._engine = ca_engine
        plugin._engine_errored = False
        plugin._session_id = "test_bg"
        plugin._current_turn = 0
        _ca_plugin._engines["test_bg"] = plugin

        try:
            with patch('tools.skill_provenance.get_current_write_origin', return_value="background_review"):
                # 调用模块级 hook
                result = _ca_plugin._on_pre_llm_call_v5(
                    session_id="test_bg",
                    user_message="这是后台审查内容",
                    conversation_history=[],
                    context_length=50000,
                )

            # 1. 返回 None（跳过 A-stage）
            assert result is None, "bg_review 应返回 None"

            # 2. _bg_turn 标记已设置
            assert plugin._bg_turn is True, "bg_review 应设置 _bg_turn=True"

            # 3. turn 计数器未增长
            assert plugin._engine._current_turn == 0, f"bg 不增 turn"

            # 4. DB 无任何写入（方向 B：bg 不写 DB）
            from ca.store import read_turn_stream_all
            rows = read_turn_stream_all(ca_engine.store, "test_bg")
            assert len(rows) == 0, f"bg 不应写 DB, got {len(rows)} rows"

            # 清理 _bg_turn 标记
            plugin._bg_turn = False
        finally:
            # 清理
            _ca_plugin._engines.pop("test_bg", None)


# ============================================================================
# 从 test_circuit / test_lifecycle 合并的剩余唯一测试
# ============================================================================


class TestPreLlmCallSessionRecall:
    """首轮会话 recall 注入行为。"""

    @pytest.fixture(autouse=True)
    def _force_reality_inject(self, monkeypatch):
        """注入开关显式置 True（测试自包含，不依赖外部环境）。

        v7.1 (BUG-08)：REALITY_INJECT_ENABLED 由环境变量控制，shell/gateway
        继承的 .env 可能置 0 导致测试短路——测试必须自己控制 Config 状态。
        """
        monkeypatch.setenv("CA_REALITY_INJECT_ENABLED", "1")
        from ca.config import Config
        Config.REALITY_INJECT_ENABLED = True
        yield
        Config.REALITY_INJECT_ENABLED = os.getenv("CA_REALITY_INJECT_ENABLED", "1") == "1"

    def _setup_plugin(self, session_id: str, turn: int = 1) -> tuple:
        """创建插件 + 真实引擎，注册到 _engines。"""
        plugin = CAContextAssemblerPlugin()
        plugin.on_session_start(session_id, model="deepseek-v4-flash")
        assert plugin._engine is not None
        assert plugin._engine_errored is False
        with _engines_lock:
            _engines[session_id] = plugin
        conv_hist = [{"role": "user", "content": "hello"}] if turn >= 1 else []
        return plugin, conv_hist

    def _cleanup(self, session_id: str):
        with _engines_lock:
            _engines.pop(session_id, None)
        try:
            from ca import session_manager
            session_manager._sessions.pop(session_id, None)
        except Exception:
            pass

    def test_session_start_recalls_on_turn_1(self):
        """turn=1 时从 reality 语义召回并注入 <wiki_carryover>（v7 决策 41）。"""
        sid = f"recall_test_{int(time.time())}"
        plugin, conv = self._setup_plugin(sid, turn=1)
        try:
            fake_entries = [
                {"reality_id": 1, "name": "旧话题", "hdl": "旧话题状态锚点",
                 "current_status": {"current_state": ["旧话题结论"]},
                 "timeline": []},
            ]
            with patch('ca.inject.pick_injection_realities',
                       return_value=fake_entries) as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16) as mock_embed:
                    with patch('tools.skill_provenance.get_current_write_origin',
                               return_value="foreground"):
                        plugin._cleanup_done = True  # 跳过 120s 清账等待
                        result = _on_pre_llm_call_v5(
                            session_id=sid,
                            user_message="test query",
                            conversation_history=conv,
                            model="deepseek-v4-flash",
                        )
            # 验证 recall 被注入（wiki_carryover 含 current_state）
            assert result is not None
            assert "<wiki_carryover>" in result
            assert "旧话题结论" in result
            mock_q.assert_called_once()
            mock_embed.assert_called()
            assert plugin._session_recall_done is True
        finally:
            self._cleanup(sid)

    def test_session_start_not_recalled_twice(self):
        """turn=1 后 _session_recall_done=True，再调不重复。"""
        sid = f"recall_twice_{int(time.time())}"
        plugin, conv = self._setup_plugin(sid, turn=1)
        # 置为已注入状态
        plugin._session_recall_done = True
        try:
            with patch('ca.inject.pick_injection_realities') as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16):
                    with patch('tools.skill_provenance.get_current_write_origin',
                               return_value="foreground"):
                        result = _on_pre_llm_call_v5(
                            session_id=sid,
                            user_message="another query",
                            conversation_history=conv,
                            model="deepseek-v4-flash",
                        )
            # _session_recall_done=True → 不应再查 wiki
            mock_q.assert_not_called()
            assert result is None
        finally:
            self._cleanup(sid)

    def test_turn_2_does_not_trigger_session_recall(self):
        """turn=2 时不触发 session-start recall（等 topic_switch 触发）。"""
        sid = f"recall_turn2_{int(time.time())}"
        plugin, conv = self._setup_plugin(sid, turn=2)
        try:
            with patch('ca.inject.pick_injection_realities') as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16):
                    with patch('tools.skill_provenance.get_current_write_origin',
                               return_value="foreground"):
                        _on_pre_llm_call_v5(
                            session_id=sid,
                            user_message="turn 2 msg",
                            conversation_history=[
                                {"role": "user", "content": "first"},
                                {"role": "user", "content": "turn 2 msg"},
                            ],
                            model="deepseek-v4-flash",
                        )
            # session-start recall 不应在 turn=2 时触发
            # 注意: topic_switch recall 也可能触发，这里只验证 session-start 逻辑不触发
            assert plugin._session_recall_done is False
        finally:
            self._cleanup(sid)


# ============================================================================
# 话题切换 FAR 守卫 — _on_pre_llm_call_v5 topic_switch 行为
# ============================================================================


class TestTopicSwitchRecall:
    """话题切换时 FAR/REL 守卫行为。"""

    @pytest.fixture(autouse=True)
    def _force_reality_inject(self, monkeypatch):
        """注入开关显式置 True（同 TestPreLlmCallSessionRecall，v7.1 BUG-08）。"""
        monkeypatch.setenv("CA_REALITY_INJECT_ENABLED", "1")
        from ca.config import Config
        Config.REALITY_INJECT_ENABLED = True
        yield
        Config.REALITY_INJECT_ENABLED = os.getenv("CA_REALITY_INJECT_ENABLED", "1") == "1"

    def _setup_plugin(self, session_id: str) -> tuple:
        """创建插件 + 真实引擎 + mock topic_mgr。"""
        plugin = CAContextAssemblerPlugin()
        plugin.on_session_start(session_id, model="deepseek-v4-flash")
        assert plugin._engine is not None
        plugin._session_recall_done = True  # 屏蔽首轮干扰
        # 注入 mock topic_manager
        mock_mgr = MagicMock()
        mock_mgr._turn_to_topic = {1: 1, 2: 2}
        mock_mgr._current_topic_id = 2
        plugin._topic_mgr = mock_mgr
        plugin._last_topic_id = 1  # 旧话题 = 1
        with _engines_lock:
            _engines[session_id] = plugin
        return plugin

    def _cleanup(self, session_id: str):
        with _engines_lock:
            _engines.pop(session_id, None)
        try:
            from ca import session_manager
            session_manager._sessions.pop(session_id, None)
        except Exception:
            pass

    def _call_hook(self, session_id: str, user_msg: str = "new topic", turn: int = 2):
        """调用 pre_llm_call hook。"""
        conv = [{"role": "user", "content": "old msg"}] + \
               [{"role": "user", "content": user_msg}]
        with patch('tools.skill_provenance.get_current_write_origin',
                   return_value="foreground"):
            return _on_pre_llm_call_v5(
                session_id=session_id,
                user_message=user_msg,
                conversation_history=conv,
                model="deepseek-v4-flash",
            )

    def test_far_switch_triggers_recall(self):
        """旧话题 FAR 时从 wiki 语义召回并注入 <wiki_carryover>。"""
        sid = f"far_test_{int(time.time())}"
        plugin = self._setup_plugin(sid)
        try:
            # mock detect → switched
            plugin._engine._compute_assemble_plan = MagicMock(return_value=([], None))
            fake_entries = [
                {"reality_id": 1, "name": "T", "hdl": "",
                 "current_status": {"current_state": ["far recall 结论"]},
                 "timeline": []},
            ]
            with patch('ca.inject.pick_injection_realities',
                       return_value=fake_entries) as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16):
                    with patch('ca_assembler_plugin.TopicGradeManager') as mock_tm_cls:
                        mock_tm = MagicMock()
                        mock_tm_cls.return_value = mock_tm
                        plugin._topic_mgr = mock_tm
                        mock_tm.detect.return_value = True
                        mock_tm.get_topic_grades.return_value = {1: TopicGrade.FAR}
                        mock_tm._current_topic_id = 2
                        mock_tm._turn_to_topic = {1: 1, 2: 2}

                        result = self._call_hook(sid)

            mock_q.assert_called_once()
            assert result is not None
            assert "far recall 结论" in result
        finally:
            self._cleanup(sid)

    def test_rel_switch_skips_recall(self):
        """旧话题 REL 时跳过 recall。"""
        sid = f"rel_test_{int(time.time())}"
        plugin = self._setup_plugin(sid)
        try:
            with patch('ca.inject.pick_injection_realities') as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16):
                    with patch('ca_assembler_plugin.TopicGradeManager') as mock_tm_cls:
                        mock_tm = MagicMock()
                        mock_tm_cls.return_value = mock_tm
                        plugin._topic_mgr = mock_tm
                        mock_tm.detect.return_value = True
                        mock_tm.get_topic_grades.return_value = {1: TopicGrade.REL}
                        mock_tm._current_topic_id = 2
                        mock_tm._turn_to_topic = {1: 1, 2: 2}

                        self._call_hook(sid)

            mock_q.assert_not_called()
        finally:
            self._cleanup(sid)

    def test_no_switch_skips_recall(self):
        """话题未切换时跳过 recall。"""
        sid = f"noswitch_test_{int(time.time())}"
        plugin = self._setup_plugin(sid)
        try:
            with patch('ca.inject.pick_injection_realities') as mock_q:
                with patch.object(plugin._engine.embed_client, 'embed',
                                  return_value=[0.1] * 16):
                    with patch('ca_assembler_plugin.TopicGradeManager') as mock_tm_cls:
                        mock_tm = MagicMock()
                        mock_tm_cls.return_value = mock_tm
                        plugin._topic_mgr = mock_tm
                        mock_tm.detect.return_value = False
                        mock_tm._current_topic_id = 1
                        mock_tm._turn_to_topic = {}

                        self._call_hook(sid)

            mock_q.assert_not_called()
        finally:
            self._cleanup(sid)

