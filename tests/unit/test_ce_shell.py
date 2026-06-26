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


class TestCEMutationRestore:
    """CE compress + post_llm_call 往返：FAR 行删除后 content 恢复不索引错位"""

    def _make_topic_mgr(self, grade_map):
        from unittest.mock import MagicMock
        mock = MagicMock()
        mock.get_turn_grade.side_effect = lambda tn: grade_map.get(tn, MagicMock())
        return mock

    def test_post_llm_call_uses_snapshot_not_misaligned_backup(self, ca_engine):
        """CE compress 删除 FAR 行后，_full_backup 是完整原始快照；
        post_llm_call_v5 用 _full_backup 整表替换 conv_hist，
        恢复原始 Elm 内容及 FAR 行。

        场景：4 个 user turn（16 行），turn 2 的 thought/tool 为 FAR 等级。
        compress 删除 FAR thought+tool（2 行）→ conv 为 14 行 →
        post_llm_call 用 _full_backup 整表替换 → conv 恢复为 16 行原创内容。
        """
        from ca.store import write_turn_v5
        from ca.grade import TopicGrade
        from unittest.mock import MagicMock

        plugin = _make_plugin(ca_engine)
        plugin._engine = ca_engine
        ca_engine._current_turn = 4
        ca_engine._session_id = "test"
        ca_engine._seq_counter.clear()
        ca_engine._seq_counter[4] = 12

        # 写入 4 个 turn 的 DB 数据（1-indexed）
        for tn, turns_data in {
            1: [("user","Q1","U1_Fct"), ("assistant","thought1","T1_Fct"),
                ("tool","R1","To1_Fct"), ("assistant","fin1","F1_Fct")],
            2: [("user","Q2","U2_Fct"), ("assistant","thought2_FAR","T2_Fct"),
                ("tool","R2","To2_Fct"), ("assistant","fin2","F2_Fct")],
            3: [("user","Q3","U3_Fct"), ("assistant","thought3","T3_Fct"),
                ("tool","R3","To3_Fct"), ("assistant","fin3","F3_Fct")],
            4: [("user","Q4","U4_Fct"), ("assistant","thought4","T4_Fct"),
                ("tool","R4","To4_Fct"), ("assistant","fin4","F4_Fct")],
        }.items():
            for seq, (role, content, fct) in enumerate(turns_data):
                kw = dict(role=role, elm_text=content, fct_text=fct)
                if role == "assistant" and seq == 1:
                    kw["tool_calls_json"] = f'[{{"id":"tc{tn}"}}]'
                if role == "assistant" and seq == 3:
                    kw["finish_reason"] = "stop"
                if role == "tool":
                    kw["tool_call_id"] = f"tc{tn}"
                write_turn_v5(ca_engine.store, "test", tn, seq, **kw)

        # topic_grades: turn 2 = FAR, 其余 ACT
        mock_tm = MagicMock()
        mock_tm.get_turn_grade.side_effect = lambda tn: TopicGrade.FAR if tn == 2 else TopicGrade.ACT
        plugin._topic_mgr = mock_tm

        # 构建 conversation_history（16 行）
        orig = [
            # turn 1 (ACT) — 非 tail
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "thought1", "reasoning_content": "r1", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "R1"},
            {"role": "assistant", "content": "fin1", "finish_reason": "stop"},
            # turn 2 (FAR) — 非 tail
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "thought2_FAR", "reasoning_content": "r2", "tool_calls": [{"id": "tc2"}]},
            {"role": "tool", "content": "R2"},
            {"role": "assistant", "content": "fin2", "finish_reason": "stop"},
            # turn 3 (ACT) — tail
            {"role": "user", "content": "Q3"},
            {"role": "assistant", "content": "thought3", "reasoning_content": "r3", "tool_calls": [{"id": "tc3"}]},
            {"role": "tool", "content": "R3"},
            {"role": "assistant", "content": "fin3", "finish_reason": "stop"},
            # turn 4 (ACT) — tail
            {"role": "user", "content": "Q4"},
            {"role": "assistant", "content": "thought4", "reasoning_content": "r4", "tool_calls": [{"id": "tc4"}]},
            {"role": "tool", "content": "R4"},
            {"role": "assistant", "content": "fin4", "finish_reason": "stop"},
        ]

        # ── 模拟 CE compress 路径 ──
        plugin._full_backup = [{**m} for m in orig]
        conv = [{**m} for m in orig]

        plugin.pre_llm_call_v5(conversation_history=conv)

        # FAR thought+tool 被删（turn 2: conv[5]=thought2_FAR, conv[6]=R2）
        assert len(conv) == 14, f"Expected 14 rows after FAR deletion, got {len(conv)}"

        # ── 模拟 post_llm_call（_full_backup 不清除） ──
        plugin.post_llm_call_v5(
            session_id="test", user_message="Q4",
            assistant_response="final_response",
            conversation_history=conv,
        )

        # ── 验证恢复结果 ──
        # _full_backup 整表替换 → conv 恢复为完整的 16 行原创内容
        assert len(conv) == 16, f"Expected 16 rows after restoration, got {len(conv)}"
        assert conv[0]["content"] == "Q1", f"conv[0]={conv[0]['content']!r}"
        assert conv[1]["content"] == "thought1", f"conv[1]={conv[1]['content']!r}"
        # FAR 行也恢复了
        assert conv[5]["content"] == "thought2_FAR", f"conv[5]={conv[5]['content']!r}"
        assert conv[6]["content"] == "R2", f"conv[6]={conv[6]['content']!r}"
        assert conv[5].get("reasoning_content") == "r2", "FAR reasoning_content restored"
        # 尾巴保护区行正确
        assert conv[8]["content"] == "Q3", f"conv[8]={conv[8]['content']!r}"
        assert conv[12]["content"] == "Q4", f"conv[12]={conv[12]['content']!r}"
        assert conv[1].get("reasoning_content") == "r1", "reasoning_content restored"


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
