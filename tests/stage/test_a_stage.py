"""A-stage 注入测试 — 全量 + 增量 + 尾巴保护 + Grade 映射。

覆盖：
- _simple_mutation_mode_v5（全量路径）
- _incremental_mutation（增量路径）
- 尾巴保护边界
- _A_stable_cache 缓存一致性
- Grade 映射：Grade.from_topic_grade()

设计决策对照:
  → R-001: 三阶段架构 C/A/L (test_mutation_*, test_incremental_*)
  → C-010: A-stage 解耦 (test_mutation_modes)
  → V-001: Replace/Append/Off 注入 (test_replace_*, test_append_*)
Wiki: design/decision-points-wiki.md §R-001, §C-010, §V-001

  → tests/INDEX.md — 测试套件总览"""

import json
import pytest


# ── 插件实例化（与 root/test_astage.py 保持一致） ──


def _make_plugin(ca_engine, session_id: str = "test"):
    """加载插件模块并绑定引擎"""
    import importlib, sys
    from pathlib import Path
    _f = Path(__file__).resolve().parent.parent.parent / "__init__.py"
    _spec = importlib.util.spec_from_file_location("ca_assembler_test", _f)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["ca_assembler_test"] = _mod
    _spec.loader.exec_module(_mod)
    plugin = _mod.CAContextAssemblerPlugin()
    plugin._engine = ca_engine
    plugin._session_id = session_id
    return plugin


# ============================================================================
# 全量路径（_simple_mutation_mode_v5）
# ============================================================================


class TestSimpleMutationModeV5:
    """_simple_mutation_mode_v5 — turn_stream Fct 替换 tool 行"""

    def test_replaces_tool_lines_with_fct(self, ca_engine):
        """保护区外的 asst{tc} → content 被替换为 Fct"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 2, 1,
                      role="assistant", content="orig2",
                      fct_text='工具组：文件读取')
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", content="result2",
                      fct_text='read_file → 成功')

        plugin = _make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "orig2", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "result2"},
            {"role": "user", "content": "第三问"},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "第四问"},
            {"role": "assistant", "content": "a4"},
            {"role": "user", "content": "第五问"},
            {"role": "assistant", "content": "a5"},
        ]
        plugin._simple_mutation_mode_v5(conv)

        assert conv[3]["content"] == "工具组：文件读取"
        assert conv[4]["content"] == "read_file → 成功"
        assert conv[2]["content"] == "第二问"  # user 原文保留

    def test_tail_boundary_preserved(self, ca_engine):
        """尾部保护区（倒数第 2 个 user 之后）原文保留"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", content="old2", fct_text="新Fct2")
        write_turn_v5(ca_engine.store, "test", 4, 2,
                      role="tool", content="old4", fct_text="新Fct4")

        plugin = _make_plugin(ca_engine)
        conv = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "t1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "t2", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "old2"},
            {"role": "user", "content": "Q3"},
            {"role": "assistant", "content": "t3", "tool_calls": [{"id": "c3"}]},
            {"role": "tool", "tool_call_id": "c3", "content": "old3"},
            {"role": "user", "content": "Q4"},   # i=8, 尾区
            {"role": "assistant", "content": "t4", "tool_calls": [{"id": "c4"}]},
            {"role": "tool", "tool_call_id": "c4", "content": "old4"},
            {"role": "user", "content": "Q5"},
        ]
        plugin._simple_mutation_mode_v5(conv)

        assert conv[4]["content"] == "新Fct2"       # 保护区外 → 替换
        assert conv[10]["content"] == "old4"        # 尾区内 → 原文保留

    def test_no_fct_skips_line(self, ca_engine):
        """turn_stream 无 Fct → 跳过（原文保留）"""
        plugin = _make_plugin(ca_engine)
        conv = [{"role": "user", "content": "Q"} for _ in range(5)]
        conv += [{"role": "assistant", "content": "thought", "tool_calls": [{"id": "c1"}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "result"}]
        plugin._simple_mutation_mode_v5(conv)
        assert conv[6]["content"] == "result"

    def test_user_lines_never_replaced(self, ca_engine):
        plugin = _make_plugin(ca_engine)
        conv = [{"role": "user", "content": "帮我查数据"}]
        plugin._simple_mutation_mode_v5(conv)
        assert conv[0]["content"] == "帮我查数据"

    def test_snapshot_saved(self, ca_engine):
        plugin = _make_plugin(ca_engine)
        conv = [{"role": "user", "content": "hi"}]
        plugin._simple_mutation_mode_v5(conv)
        assert plugin._saved_history_snapshot is not None
        assert plugin._saved_history_snapshot[0]["content"] == "hi"


# ============================================================================
# 增量路径（_incremental_mutation）
# ============================================================================


class TestIncrementalMutation:
    """_incremental_mutation — 缓存稳定区 → delta 替换 → 尾部保留"""

    def test_returns_none_without_engine(self, ca_engine):
        """无引擎时返回 None"""
        plugin = _make_plugin(ca_engine)
        plugin._engine = None
        result = plugin._incremental_mutation([{"role": "user", "content": "hi"}])
        assert result is None

    def test_no_cache_falls_back_to_full(self, ca_engine):
        """无 _A_stable_cache → 回退到 _full_mutation"""
        plugin = _make_plugin(ca_engine)
        plugin._A_stable_cache = None
        result = plugin._incremental_mutation([{"role": "user", "content": "hi"}])
        assert result is None  # _full_mutation 返回 None
        assert plugin._saved_history_snapshot is not None

    def test_cache_replaces_stable_area(self, ca_engine):
        """缓存命中 → 稳定区被替换"""
        from ca.store import write_turn_v5
        # DB 写入 Fct（供 cache 读取时引用）
        write_turn_v5(ca_engine.store, "test", 1, 2,
                      role="tool", content="old1", fct_text="cached_fct")

        plugin = _make_plugin(ca_engine)
        # 手动设置缓存
        plugin._A_stable_cache = [
            {"role": "user", "content": "cached_Q"},
            {"role": "assistant", "content": "cached_A", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "cached_T"},
        ]
        plugin._A_cache_turns = 1
        conv = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "T1"},
            {"role": "user", "content": "Q2"},  # 尾区起始
            {"role": "assistant", "content": "A2"},
        ]
        result = plugin._incremental_mutation(conv)
        # 稳定区被替换
        assert conv[0]["content"] == "cached_Q"
        assert conv[1]["content"] == "cached_A"
        assert conv[2]["content"] == "cached_T"
        # 尾区保留
        assert conv[3]["content"] == "Q2"
        assert conv[4]["content"] == "A2"

    def test_cache_mismatch_falls_back(self, ca_engine):
        """缓存角色不匹配 → 回退到全量，缓存被更新"""
        plugin = _make_plugin(ca_engine)
        plugin._A_stable_cache = [
            {"role": "user", "content": "cached"},
        ]
        plugin._A_cache_turns = 1
        conv = [
            {"role": "user", "content": "Q1"},
            {"role": "tool", "tool_call_id": "c1", "content": "T"},
        ]
        result = plugin._incremental_mutation(conv)
        # 全量路径重新计算缓存：tail_boundary=0（不足2 user），缓存为空
        assert plugin._A_cache_turns == 0

    def test_snapshot_saved_incremental(self, ca_engine):
        """增量路径也保存快照"""
        plugin = _make_plugin(ca_engine)
        plugin._A_stable_cache = [
            {"role": "user", "content": "cached"},
        ]
        conv = [{"role": "user", "content": "原始"}]
        plugin._incremental_mutation(conv)
        assert plugin._saved_history_snapshot is not None
        assert plugin._saved_history_snapshot[0]["content"] == "原始"
