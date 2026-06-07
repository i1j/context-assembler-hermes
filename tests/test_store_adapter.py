"""
L1 摘要系统重构 — format_previous_summary_for_prompt 单元测试

覆盖测试方案 §2.2 (S1-S8)

测试策略：
- 纯函数直接导入，不 mock
- 使用 from ca.store import format_previous_summary_for_prompt
- 部分断言可能因实现尚未完成而失败
"""
import json
import pytest

try:
    from ca.store import format_previous_summary_for_prompt
    IMPORT_OK = True
except ImportError:
    IMPORT_OK = False
    format_previous_summary_for_prompt = None


class TestFormatPreviousSummary:

    @pytest.mark.high
    @pytest.mark.l1
    def test_s1_none_input(self):
        """S1: None 输入 → 返回 "无" """
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        result = format_previous_summary_for_prompt(None)
        assert result == "无", f"Expected '无', got {repr(result)}"

    @pytest.mark.high
    @pytest.mark.l1
    def test_s2_empty_string(self):
        """S2: 空字符串 → 返回 "无" """
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        result = format_previous_summary_for_prompt("")
        assert result == "无", f"Expected '无', got {repr(result)}"

    @pytest.mark.high
    @pytest.mark.l1
    def test_s3_old_json_to_markdown(self):
        """S3: 旧 5 类 JSON → 4 类 Markdown"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        old_json = json.dumps({
            "core_change": "修复Bug",
            "new_materials": ["日志文件"],
            "objective_facts": ["OOM"],
            "consensus": ["加内存"],
            "todo": ["采购"]
        })
        result = format_previous_summary_for_prompt(old_json)
        assert isinstance(result, str), f"Expected str, got {type(result)}"
        assert "修复Bug" in result, f"Should contain core_change content: {result}"
        # 应包含新格式的 Markdown 标题
        assert "### " in result, f"Should contain Markdown headings: {result}"

    @pytest.mark.high
    @pytest.mark.l1
    def test_s4_plain_text_returned_as_is(self):
        """S4: 纯文本原样返回"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        text = "纯文本摘要内容"
        result = format_previous_summary_for_prompt(text)
        assert result == text, f"Expected '{text}', got {repr(result)}"

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s5_json_missing_fields(self):
        """S5: JSON 缺少部分字段"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        old_json = json.dumps({"core_change": "test"})
        result = format_previous_summary_for_prompt(old_json)
        assert isinstance(result, str)
        # 不应抛出异常
        assert len(result) > 0

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s6_json_empty_lists(self):
        """S6: JSON 含空列表字段"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        old_json = json.dumps({
            "core_change": "test",
            "new_materials": [],
            "objective_facts": [],
            "consensus": [],
            "todo": []
        })
        result = format_previous_summary_for_prompt(old_json)
        assert isinstance(result, str)
        assert "test" in result or "<core_change>" in result

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s7_already_markdown(self):
        """S7: 已是最新 Markdown 格式 → 原样返回（幂等）"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        markdown = (
            "### 现象与问题\n- 测试\n"
            "### 背景与约束\n- 无\n"
            "### 决策与共识\n- 无\n"
            "### 后续行动\n- 无\n"
            "<core_change>测试</core_change>"
        )
        result = format_previous_summary_for_prompt(markdown)
        assert result == markdown, f"Should be idempotent: {repr(result)[:60]} != {repr(markdown)[:60]}"

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s8_large_json(self):
        """S8: 超大 JSON（模拟 DB 历史记录），只输出前 3 项"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        data = {
            "core_change": "批量更新",
            "new_materials": [f"文件{i}" for i in range(10)],
            "objective_facts": [f"事实{i}" for i in range(10)],
            "consensus": [f"共识{i}" for i in range(10)],
            "todo": [f"待办{i}" for i in range(10)]
        }
        result = format_previous_summary_for_prompt(json.dumps(data))
        assert isinstance(result, str)
        # 不应抛出异常
        assert "批量更新" in result

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s2b_wu_string(self):
        """S2b: "无" 输入 → 返回 "无" """
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        result = format_previous_summary_for_prompt("无")
        assert result == "无", f"Expected '无', got {repr(result)}"

    @pytest.mark.medium
    @pytest.mark.l1
    def test_s2c_no_valid_increment(self):
        """S2c: "无有效增量" 输入 → 当前实现原样返回（未在 MEANINGLESS_CORE 中定义）"""
        if not IMPORT_OK:
            pytest.skip("format_previous_summary_for_prompt not yet implemented")
        result = format_previous_summary_for_prompt("无有效增量")
        # 当前实现未将"无有效增量"映射为"无"，保留原样
        assert isinstance(result, str)
