"""theme carryover 注入格式化 — _format_wiki_carryover（v6.5 重构版）。

设计决策对照（wiki theme 重构方案 v4/v5）:
  → 注入 = 当前详细状态（title + overview + OODA 四组 + key_facts + open_items）
  → 互信息量优先级裁剪（读者 = 云端大模型）:
      P0 title（永不裁剪）
      P1 overview（连贯叙述 = 最快建立主题认知的载体）
      P2 OODA 决策与方案
      P3 OODA 现象与问题
      P4 OODA 背景与约束
      P5 OODA 后续行动
      P6 key_facts（与决策/现象重叠，信息增量低）
      P7 open_items（与后续行动重叠，信息增量最低）
  → 超预算从 P7 起逐级整段移除；同级内先缩条数（全部→5→3→1）再缩长度（200→120→80）
  → 单 theme 预算 = per_theme_max_chars（默认 2000，一次性注入不进对话记录表）

覆盖:
  - 空列表 / 无内容 theme → 空字符串
  - 完整 theme 格式化（方括号分段 + 中文标签排版）
  - P0 title 永不裁剪
  - 优先级裁剪顺序: open_items → key_facts → 后续行动 → 背景 → 现象 → 决策 → overview
  - 同级条数裁剪（5→3→1，保留前缀）
  - 同级长度裁剪（200→120→80），条数优先于长度
  - 多 theme 各自独立预算
"""

import importlib
import sys
from pathlib import Path

import pytest

# 加载 ca_assembler/__init__.py 作为独立模块（与 test_plugin.py 相同模式）
_plugin_file = Path(__file__).resolve().parent.parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("ca_assembler_plugin_theme_carryover", _plugin_file)
_ca = importlib.util.module_from_spec(_spec)
sys.modules["ca_assembler_plugin_theme_carryover"] = _ca
_spec.loader.exec_module(_ca)

_format_wiki_carryover = _ca._format_wiki_carryover
_format_single_theme = _ca._format_single_theme


def _theme(**overrides) -> dict:
    """构造一个完整 theme dict（所有字段齐备）。"""
    theme = {
        "title": "连接池与超时配置优化",
        "overview": "围绕连接池耗尽导致的超时问题，完成了配置参数优化并验证效果。",
        "ooda": {
            "现象与问题": ["连接池耗尽导致请求超时", "超时命中率波动"],
            "背景与约束": ["生产环境 QPS 峰值 5k", "数据库连接数上限 100"],
            "决策与方案": ["连接池上限调至 200", "增加超时监控告警"],
            "后续行动": ["监控超时命中率", "压测验证新配置"],
        },
        "key_facts": ["连接池耗尽直接导致超时", "调参后超时率下降 40%"],
        "open_items": ["压测报告待输出"],
    }
    theme.update(overrides)
    return theme


class TestThemeCarryoverBase:
    """基础行为：空输入 / 标记 / 排版结构。"""

    def test_empty_list_returns_empty(self):
        assert _format_wiki_carryover([]) == ""

    def test_theme_with_no_content_skipped(self):
        """无 title 且无任何内容的 theme 不产生块。"""
        themes = [{"title": "", "ooda": {}, "key_facts": [], "open_items": [], "overview": ""}]
        assert _format_wiki_carryover(themes) == ""

    def test_markers_present(self):
        result = _format_wiki_carryover([_theme()])
        assert result.startswith("<wiki_carryover>\n")
        assert result.endswith("\n</wiki_carryover>")

    def test_title_heading_present(self):
        result = _format_wiki_carryover([_theme()])
        assert "## 连接池与超时配置优化" in result

    def test_full_theme_all_sections_present(self):
        """完整 theme → 所有分段标题与内容都出现（预算充足）。"""
        result = _format_wiki_carryover([_theme()], per_theme_max_chars=5000)
        assert "[当前状态]" in result and "围绕连接池耗尽" in result
        for group in ["现象与问题", "背景与约束", "决策与方案", "后续行动"]:
            assert f"[{group}]" in result
        assert "[结论]" in result and "- 连接池耗尽直接导致超时" in result
        assert "[待办]" in result and "- 压测报告待输出" in result

    def test_ooda_items_listed_as_bullets(self):
        result = _format_wiki_carryover([_theme()], per_theme_max_chars=5000)
        assert "- 连接池上限调至 200" in result
        assert "- 连接池耗尽导致请求超时" in result


class TestThemeCarryoverPriority:
    """互信息量优先级裁剪（P7→P0 逆序移除，直接测 _format_single_theme）。"""

    @staticmethod
    def _budget_cut(full: str, section: str) -> int:
        """预算 = 全量渲染长度 - 该段（含 join 分隔 \n\n）→ 刚好触发该段移除。"""
        return len(full) - len(section) - 2

    def test_title_never_cut(self):
        """P0: 预算极小 → 仍保留 title 标题行。"""
        result = _format_single_theme(_theme(), max_chars=30)
        assert "## 连接池与超时配置优化" in result

    def test_open_items_cut_first(self):
        """P7: 预算刚够去掉 open_items → [待办] 消失，其余保留。"""
        theme = _theme()
        full = _format_single_theme(theme, max_chars=5000)
        budget = self._budget_cut(full, "[待办]\n- 压测报告待输出")
        result = _format_single_theme(theme, max_chars=budget)
        assert "[待办]" not in result
        assert "[结论]" in result          # P6 仍在
        assert "[决策与方案]" in result    # P2 仍在

    def test_key_facts_cut_before_ooda(self):
        """P6: 预算再紧 → [结论] 消失，但 OODA 组仍在。"""
        theme = _theme()
        full = _format_single_theme(theme, max_chars=5000)
        kf_section = "[结论]\n- 连接池耗尽直接导致超时\n- 调参后超时率下降 40%"
        budget = self._budget_cut(full, kf_section)
        result = _format_single_theme(theme, max_chars=budget)
        assert "[结论]" not in result
        assert "[待办]" not in result      # P7 已先被砍
        assert "[后续行动]" in result      # P5 仍在

    def test_followup_cut_before_background(self):
        """P5: 后续行动比背景与约束先砍（仅含相邻两段，无低优先级段干扰）。"""
        theme = _theme(
            overview="",
            ooda={
                "背景与约束": ["生产环境 QPS 峰值 5k"],
                "后续行动": ["监控超时命中率", "压测验证新配置"],
            },
            key_facts=[],
            open_items=[],
        )
        full = _format_single_theme(theme, max_chars=5000)
        sec = "[后续行动]\n- 监控超时命中率\n- 压测验证新配置"
        budget = len(full) - len(sec) - 2
        result = _format_single_theme(theme, max_chars=budget)
        assert "[后续行动]" not in result
        assert "[背景与约束]" in result    # P4 仍在

    def test_background_cut_before_problem(self):
        """P4: 背景与约束比现象与问题先砍（仅含相邻两段）。"""
        theme = _theme(
            overview="",
            ooda={
                "现象与问题": ["连接池耗尽导致请求超时"],
                "背景与约束": ["生产环境 QPS 峰值 5k", "数据库连接数上限 100"],
            },
            key_facts=[],
            open_items=[],
        )
        full = _format_single_theme(theme, max_chars=5000)
        sec = "[背景与约束]\n- 生产环境 QPS 峰值 5k\n- 数据库连接数上限 100"
        budget = len(full) - len(sec) - 2
        result = _format_single_theme(theme, max_chars=budget)
        assert "[背景与约束]" not in result
        assert "[现象与问题]" in result    # P3 仍在

    def test_problem_cut_before_decision(self):
        """P3: 现象与问题比决策与方案先砍（仅含相邻两段）。"""
        theme = _theme(
            overview="",
            ooda={
                "决策与方案": ["连接池上限调至 200"],
                "现象与问题": ["连接池耗尽导致请求超时", "超时命中率波动"],
            },
            key_facts=[],
            open_items=[],
        )
        full = _format_single_theme(theme, max_chars=5000)
        sec = "[现象与问题]\n- 连接池耗尽导致请求超时\n- 超时命中率波动"
        budget = len(full) - len(sec) - 2
        result = _format_single_theme(theme, max_chars=budget)
        assert "[现象与问题]" not in result
        assert "[决策与方案]" in result    # P2 仍在

    def test_decision_cut_before_overview(self):
        """P2: 决策与方案比 overview 先砍（仅含相邻两段）。"""
        theme = _theme(
            overview="围绕连接池耗尽的超时问题完成了参数优化",
            ooda={
                "决策与方案": ["连接池上限调至 200", "增加超时监控告警"],
            },
            key_facts=[],
            open_items=[],
        )
        full = _format_single_theme(theme, max_chars=5000)
        sec = "[决策与方案]\n- 连接池上限调至 200\n- 增加超时监控告警"
        budget = len(full) - len(sec) - 2
        result = _format_single_theme(theme, max_chars=budget)
        assert "[决策与方案]" not in result
        assert "[当前状态]" in result      # P1 overview 仍在

    def test_overview_cut_last_before_title(self):
        """P1: overview 最后砍（只剩 title）。"""
        result = _format_single_theme(_theme(), max_chars=30)
        assert "## 连接池与超时配置优化" in result
        assert "[当前状态]" not in result  # P1 已被砍
        assert "[决策与方案]" not in result


class TestThemeCarryoverShrink:
    """同级裁剪：先缩条数（全部→5→3→1，保留前缀），再缩长度（200→120→80）。"""

    def _minimal_theme(self, decision_items: list[str]) -> dict:
        """只含 title + 决策与方案（排除其它段干扰裁剪路径）。"""
        return _theme(
            overview="",
            ooda={"决策与方案": decision_items},
            key_facts=[],
            open_items=[],
        )

    def test_item_count_shrinks_to_3_keeping_prefix(self):
        """P2 段 5 条 → 预算紧 → 缩到 3 条（保留前 3 条）。"""
        theme = self._minimal_theme([f"决策项{i}" for i in range(5)])
        full = _format_single_theme(theme, max_chars=5000)
        assert full.count("- 决策项") == 5
        # 预算 = 去掉后 2 条的长度 → 保留 3 条
        budget = len(full) - len("- 决策项3\n- 决策项4")
        result = _format_single_theme(theme, max_chars=budget)
        assert result.count("- 决策项") == 3
        assert "- 决策项0" in result and "- 决策项1" in result and "- 决策项2" in result
        assert "- 决策项3" not in result

    def test_item_count_shrinks_to_1(self):
        """预算更紧 → 缩到 1 条。"""
        theme = self._minimal_theme([f"决策项{i}" for i in range(5)])
        full = _format_single_theme(theme, max_chars=5000)
        budget = len(full) - len("- 决策项1\n- 决策项2\n- 决策项3\n- 决策项4")
        result = _format_single_theme(theme, max_chars=budget)
        assert result.count("- 决策项") == 1
        assert "- 决策项0" in result

    def test_item_length_shrinks_200_to_120(self):
        """单条超长 → 条数无可缩（1 条）→ 长度 200→120。"""
        theme = self._minimal_theme(["长" * 200])  # 400 字单条
        full = _format_single_theme(theme, max_chars=5000)
        assert len(full) > 200  # 初始渲染 = title + 200 字截断条
        result = _format_single_theme(theme, max_chars=180)
        assert "[决策与方案]" in result
        assert len(result) < 180  # 已缩到 120 字版本

    def test_shrink_order_count_before_length(self):
        """同级内先缩条数后缩长度：预算下 3 条（不截断）优于 1 条（截断）。"""
        items = [f"项{i}:" + "x" * 58 for i in range(5)]  # 每条 62 字
        theme = self._minimal_theme(items)
        full = _format_single_theme(theme, max_chars=5000)
        assert full.count("- 项") == 5
        # 预算: 3 条 × 62 ≈ 200 放得下；验证缩到 3 条而非截断长度
        budget = len(full) - len("- 项3:" + "x" * 58 + "\n- 项4:" + "x" * 58)
        result = _format_single_theme(theme, max_chars=budget)
        assert result.count("- 项") == 3
        assert "x" * 58 in result  # 长度未被截断（条数优先）


class TestThemeCarryoverMulti:
    """多 theme：各自独立预算裁剪。"""

    def test_multi_themes_concatenated(self):
        themes = [_theme(title="主题A"), _theme(title="主题B")]
        result = _format_wiki_carryover(themes, per_theme_max_chars=5000)
        assert "## 主题A" in result
        assert "## 主题B" in result
        assert result.startswith("<wiki_carryover>\n")
        assert result.endswith("\n</wiki_carryover>")

    def test_multi_themes_each_budgeted(self):
        """预算 300 → 两个 theme 各自裁剪，不是共享 300。"""
        themes = [_theme(title="主题A"), _theme(title="主题B")]
        result = _format_wiki_carryover(themes, per_theme_max_chars=300)
        assert "## 主题A" in result
        assert "## 主题B" in result
        # 各自裁剪到 ≤300+余量（去除包装与另一 theme）
        inner = result[len("<wiki_carryover>\n"):-len("\n</wiki_carryover>")]
        blocks = inner.split("\n\n")
        assert all(len(b) <= 350 for b in blocks)
