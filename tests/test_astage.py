
"""A-stage 替换测试 — _simple_mutation_mode_v5 / pre_llm_call_v5

A-stage tail 边界：倒数第 3 个 user 之后为保护区。
要产生保护区外的轮次，至少需要 5 个 user。
（4 个 user 时 3rd-from-last=index2，一切自此为尾区。）
"""

import json
import pytest


class TestSimpleMutationModeV5:
    """_simple_mutation_mode_v5 — turn_stream Fct 替换 tool 行"""

    def _make_plugin(self, ca_engine):
        import importlib, sys
        from pathlib import Path
        _f = Path(__file__).resolve().parent.parent / "__init__.py"
        _spec = importlib.util.spec_from_file_location("ca_assembler_plug", _f)
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["ca_assembler_plug"] = _mod
        _spec.loader.exec_module(_mod)
        plugin = _mod.CAContextAssemblerPlugin()
        plugin._engine = ca_engine
        plugin._session_id = "test"
        return plugin

    def test_replaces_tool_lines_with_fct(self, ca_engine):
        """保护区外的 asst{tc} → content 被替换为 Fct。"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 2, 1,
                      role="assistant", content="orig2",
                      l1_text='工具组：文件读取')
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", content="result2",
                      l1_text='read_file → 成功')

        plugin = self._make_plugin(ca_engine)
        # 5 users → 3rd-from-last user = index 6 (user #3)
        # 尾区: i >= 6 → user#3-5 plus their tools
        # 保护区外: i < 6 → user#1-2 plus their tools
        conv = [
            {"role": "user", "content": "第一问"},        # i=0 turn1
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "第二问"},        # i=2 turn2
            {"role": "assistant", "content": "orig2", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "result2"},
            {"role": "user", "content": "第三问"},        # i=5 turn3
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "第四问"},        # i=7 turn4
            {"role": "assistant", "content": "a4"},
            {"role": "user", "content": "第五问"},        # i=9 turn5
            {"role": "assistant", "content": "a5"},
        ]
        plugin._simple_mutation_mode_v5(conv)

        # turn 2 asst{tc} (i=3) 在保护区外 → 被替换
        assert conv[3]["content"] == "工具组：文件读取"
        # turn 2 tool (i=4) 在保护区外 → 被替换
        assert conv[4]["content"] == "read_file → 成功"
        # user 行原文保留
        assert conv[2]["content"] == "第二问"

    def test_tail_boundary_preserved(self, ca_engine):
        """尾部保护区（倒数第 3 个 user 之后）原文保留。"""
        from ca.store import write_turn_v5
        # 保护区外 (turn 2): 有 Fct
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", content="old2", l1_text="新Fct2")
        # 尾区内 (turn 4): 有 Fct 但不该被替换
        write_turn_v5(ca_engine.store, "test", 4, 2,
                      role="tool", content="old4", l1_text="新Fct4")

        plugin = self._make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "第一问"},        # i=0 turn1
            {"role": "assistant", "content": "t1"},
            {"role": "user", "content": "第二问"},        # i=2 turn2
            {"role": "assistant", "content": "t2", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "old2"},
            {"role": "user", "content": "第三问"},        # i=5 turn3
            {"role": "assistant", "content": "t3", "tool_calls": [{"id": "c3"}]},
            {"role": "tool", "tool_call_id": "c3", "content": "old3"},
            {"role": "user", "content": "第四问"},        # i=8 turn4 (尾区)
            {"role": "assistant", "content": "t4", "tool_calls": [{"id": "c4"}]},
            {"role": "tool", "tool_call_id": "c4", "content": "old4"},
            {"role": "user", "content": "第五问"},        # i=11 turn5 (尾区)
        ]
        plugin._simple_mutation_mode_v5(conv)

        # 保护区外 (turn 2, i=4): 被替换
        assert conv[4]["content"] == "新Fct2"
        # 尾区内 (turn 4, i=10): 原文保留
        assert conv[10]["content"] == "old4"

    def test_no_fct_skips_line(self, ca_engine):
        """turn_stream 无 Fct → 跳过（原文保留）。"""
        plugin = self._make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "第一问"},
            {"role": "user", "content": "第二问"},
            {"role": "user", "content": "第三问"},
            {"role": "user", "content": "第四问"},
            {"role": "user", "content": "第五问"},
            {"role": "assistant", "content": "thought", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        plugin._simple_mutation_mode_v5(conv)
        assert conv[6]["content"] == "result"  # 原文保留

    def test_user_lines_never_replaced(self, ca_engine):
        """user 行始终原文保留。"""
        plugin = self._make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "帮我查数据"},
        ]
        plugin._simple_mutation_mode_v5(conv)
        assert conv[0]["content"] == "帮我查数据"

    def test_snapshot_saved(self, ca_engine):
        """保存替换前快照。"""
        plugin = self._make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "hi"},
        ]
        plugin._simple_mutation_mode_v5(conv)
        assert plugin._saved_history_snapshot is not None
        assert len(plugin._saved_history_snapshot) == 1
        assert plugin._saved_history_snapshot[0]["content"] == "hi"
