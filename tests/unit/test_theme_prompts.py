"""theme 生成 prompt 构建 — ca/theme.py（v6.5 重构版）。

设计决策对照（wiki theme 重构方案 v4/v5）:
  → M4 双 prompt：首次 create 用结构化组织（类 _format_turns_for_prompt），
    后续 merge 用融合式（类 REFINE_SUMMARY_PROMPT）
  → M6 中文输出，长度预算参考 TOPIC_SUMMARY_MAX_CHARS=4000
  → 一级信息 = 当前详细状态（title + overview + OODA 四组 + key_facts + open_items）
  → timeline_overview：每次归并追加一条该主题块时点的状态概述（仅 overview，不存快照）

覆盖:
  - create prompt: 包含 strands 输入 / 中文要求 / 禁代码符号名 / 预算
  - merge prompt: 包含已有 theme + 新 strands / merge 决策字段 / timeline_overview
  - 预算注入（max_chars）
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

# ca/ 可导入（conftest 已加 sys.path；此处独立兜底）
_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.theme as theme_mod


def _strand(**overrides) -> dict:
    strand = {
        "hdl": "连接池与超时配置优化",
        "turns": [7, 8, 9],
        "ooda": {
            "现象与问题": ["连接池耗尽导致请求超时"],
            "决策与方案": ["连接池上限调至 200"],
        },
        "changes": ["连接池上限调至 200", "增加超时监控告警"],
    }
    strand.update(overrides)
    return strand


def _theme(**overrides) -> dict:
    theme = {
        "title": "连接池与超时配置优化",
        "overview": "围绕连接池耗尽导致的超时问题，完成了配置参数优化并验证效果。",
        "ooda": {
            "现象与问题": ["连接池耗尽导致请求超时"],
            "背景与约束": ["生产环境 QPS 峰值 5k"],
            "决策与方案": ["连接池上限调至 200"],
            "后续行动": ["监控超时命中率"],
        },
        "key_facts": ["连接池耗尽直接导致超时"],
        "open_items": ["压测报告待输出"],
    }
    theme.update(overrides)
    return theme


class TestBuildCreateThemePrompt:
    """create 式 prompt：新 strand → theme 初始状态（结构化组织）。"""

    def test_contains_strand_hdl(self):
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "连接池与超时配置优化" in prompt

    def test_contains_strand_ooda_content(self):
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "连接池上限调至 200" in prompt

    def test_multiple_strands_all_present(self):
        strands = [_strand(hdl="strand甲"), _strand(hdl="strand乙")]
        prompt = theme_mod.build_create_theme_prompt(strands, max_chars=4000)
        assert "strand甲" in prompt and "strand乙" in prompt

    def test_chinese_requirement(self):
        """M6: 中文输出要求显式声明。"""
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "中文" in prompt

    def test_no_code_symbol_rule(self):
        """hdl 命名规范：禁止代码符号名/英文标识符。"""
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "禁止" in prompt
        assert "符号" in prompt or "标识符" in prompt

    def test_budget_injected(self):
        """M6: 预算注入 prompt（max_chars）。"""
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4321)
        assert "4321" in prompt

    def test_json_output_instruction(self):
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "JSON" in prompt
        assert "title" in prompt and "overview" in prompt and "ooda" in prompt

    def test_timeline_field_in_create(self):
        """create 也产生初始时间线条目（首条）。"""
        prompt = theme_mod.build_create_theme_prompt(
            [_strand()], max_chars=4000)
        assert "timeline" in prompt.lower() or "时间线" in prompt


class TestBuildMergeThemePrompt:
    """merge 式 prompt：已有 theme + 新 strand → 融合更新（REFINE 风格）。"""

    def test_contains_existing_theme(self):
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=4000)
        assert "连接池与超时配置优化" in prompt          # theme title
        assert "围绕连接池耗尽" in prompt                # theme overview
        assert "压测报告待输出" in prompt                # theme open_items

    def test_contains_new_strands(self):
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand(hdl="新增工作线")], max_chars=4000)
        assert "新增工作线" in prompt

    def test_merge_decision_field(self):
        """归并决策字段：4B 可否决（merge:false）。"""
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=4000)
        assert "merge" in prompt

    def test_timeline_overview_field(self):
        """时间线追加：输出本主题块时点的状态概述。"""
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=4000)
        assert "timeline_overview" in prompt or "时间线" in prompt

    def test_title_keep_unless_major_shift(self):
        """title 规则：保留原 title，除非重大转向。"""
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=4000)
        assert "title" in prompt
        assert "重大" in prompt or "保留" in prompt or "转向" in prompt

    def test_chinese_and_budget_in_merge(self):
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=3999)
        assert "中文" in prompt
        assert "3999" in prompt

    def test_json_output_instruction_in_merge(self):
        prompt = theme_mod.build_merge_theme_prompt(
            _theme(), [_strand()], max_chars=4000)
        assert "JSON" in prompt
        assert "overview" in prompt and "ooda" in prompt
