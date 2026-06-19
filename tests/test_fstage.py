
"""F-stage 核心函数测试 — Fct 处理、Hdl 提取、格式化、验证。

覆盖 ca/__init__.py 的 ContextAssembler 实例方法：
- _extract_l0 (Hdl)
- _format_fct_for_display
- _is_valid_fct

设计决策对照:
  → R-004: F-stage 装配管线 (test_extract_*, test_format_*)
  → L1-001: PDD — 放弃 JSON 强迫 (test_is_valid_fct)
Wiki: design/decision-points-wiki.md §R-004, §L1-001

  → tests/INDEX.md — 测试套件总览"""

import json
import pytest


class TestExtractHdl:
    """_extract_l0 — 从 Fct dict 提取 Hdl（一句话标题）"""

    def test_normal_core_change(self, ca_engine):
        hdl = ca_engine._extract_l0({"core_change": "修复数据库连接池溢出"})
        assert hdl == "修复数据库连接池溢出"

    def test_truncated_over_100(self, ca_engine):
        hdl = ca_engine._extract_l0({"core_change": "修复" * 60})
        assert len(hdl) <= 100

    def test_empty_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_l0({"core_change": ""})
        assert hdl == "无"

    def test_wu_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_l0({"core_change": "本轮无新内容"})
        assert hdl == "无"

    def test_missing_core_returns_wu(self, ca_engine):
        hdl = ca_engine._extract_l0({"other": "data"})
        assert hdl == "无"

    def test_strip_stage_tag_segments(self, ca_engine):
        """【计划】等后续段落被截断"""
        hdl = ca_engine._extract_l0({
            "core_change": "已扩容连接池\n【计划】继续监控\n【探讨】是否需读写分离"
        })
        assert hdl == "已扩容连接池"
        assert "【计划】" not in hdl

    def test_only_plan_tag_preserved(self, ca_engine):
        """整段都是【计划】开头，保留不变"""
        hdl = ca_engine._extract_l0({"core_change": "【计划】继续监控"})
        assert hdl == "【计划】继续监控"


class TestFormatFctForDisplay:
    """_format_fct_for_display — Fct JSON 格式化为可读文本"""

    def test_normal_json_format(self, ca_engine):
        fct = json.dumps({
            "core_change": "修复bug",
            "new_materials": ["日志分析"],
            "objective_facts": ["影响3个用户"],
        })
        result = ca_engine._format_fct_for_display(fct)
        assert "修复bug" in result
        assert "日志分析" in result
        assert "影响3个用户" in result

    def test_debug_pattern_returns_empty(self, ca_engine):
        """调试描述（非JSON）→ 返回空字符串"""
        result = ca_engine._format_fct_for_display("当前会话 CA 注入 ctx 中")
        assert result == ""

    def test_invalid_json_but_no_debug_pattern(self, ca_engine):
        """非JSON且非调试模式 → 原文返回"""
        result = ca_engine._format_fct_for_display("这是正常的摘要文本")
        assert result == "这是正常的摘要文本"

    def test_empty_input_returns_empty(self, ca_engine):
        result = ca_engine._format_fct_for_display("")
        assert result == ""

    def test_none_input_returns_none(self, ca_engine):
        result = ca_engine._format_fct_for_display(None)
        assert result is None

    def test_json_with_no_core_change(self, ca_engine):
        """JSON 格式但无核心内容 → 原文返回（非调试模式）"""
        fct = json.dumps({"new_materials": [], "objective_facts": []})
        result = ca_engine._format_fct_for_display(fct)
        assert "new_materials" in result  # 原文返回


class TestIsValidFct:
    """_is_valid_fct — Fct 有效性验证"""

    def test_valid_with_core_change(self, ca_engine):
        assert ca_engine._is_valid_fct(json.dumps({"core_change": "修复"}))

    def test_empty_core_change(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"core_change": ""}))

    def test_wu_core(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"core_change": "本轮无新内容"}))

    def test_missing_core_key(self, ca_engine):
        assert not ca_engine._is_valid_fct(json.dumps({"other": "data"}))

    def test_not_json(self, ca_engine):
        assert not ca_engine._is_valid_fct("不是 JSON")

    def test_empty_string(self, ca_engine):
        assert not ca_engine._is_valid_fct("")
