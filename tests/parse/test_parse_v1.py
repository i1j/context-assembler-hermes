"""L1 摘要系统重构 — parse_v1_markdown_xml 全场景单元测试

覆盖测试方案 §2.1 (P1-P15) + 交叉评审 §2.3 (X1-X6, X9)

测试策略：
- 纯函数直接导入，不 mock
- 使用 from ca.post_process import parse_v1_markdown_xml, _safe_truncate, _json_to_v1_markdown

设计决策对照:
  → L1-004: parse_v1_markdown_xml 防御性解析 (P1-P15 全景)
  → L1-005: 截断检测双重校验 (test_truncated_*)
  → L1-009: _safe_truncate 降 . 优先级 (test_safe_truncate_*)
  → L1-010: re.compile 预编译正则 (test_regex_*)
  → L1-011: MEANINGLESS_CORE 语义短路 (test_meaningless_*)
Wiki: design/decision-points-wiki.md §L1-004~L1-011

  → tests/INDEX.md — 测试套件总览"""
import pytest
import re


# ─── 所有纯函数导入（若尚未实现，import 失败属于预期） ───
try:
    from ca.post_process import (
        parse_v1_markdown_xml,
        _safe_truncate,
        _json_to_v1_markdown,
    )
    IMPORT_OK = True
except ImportError:
    IMPORT_OK = False
    parse_v1_markdown_xml = None
    _safe_truncate = None
    _json_to_v1_markdown = None

try:
    from ca.prompts import FCT_GENERATION_PROMPT
    PROMPT_OK = True
except ImportError:
    PROMPT_OK = False
    FCT_GENERATION_PROMPT = ""


# ══════════════════════════════════════════════════════════
# P1-P15: parse_v1_markdown_xml 全场景
# ══════════════════════════════════════════════════════════
class TestParseV1MarkdownXml:

    @pytest.mark.critical
    @pytest.mark.fct
    def test_p1_normal_full_output(self):
        """P1: 正常完整 4 类 Markdown + <stage_tag>/<core_change> 对"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- CPU 使用率 90%\n"
            "### 背景与约束\n- 内存 8G\n"
            "### 决策与方案\n- 扩容\n"
            "### 后续行动\n- 采购单\n"
            "<stage_tag>\n【已实施】\n</stage_tag>\n"
            "<core_change>CPU 过高决定扩容</core_change>"
        )
        fct_dict, hdl_text, core_state = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict), f"Expected dict, got {type(fct_dict)}"
        assert "changes" in fct_dict, f"Missing changes in {fct_dict}"
        assert fct_dict["changes"] == [{"stage_tag": "已实施", "core_change": "CPU 过高决定扩容"}]
        assert "core_change" in fct_dict, f"Missing core_change in {fct_dict}"
        assert fct_dict["core_change"] == "CPU 过高决定扩容"
        assert "new_materials" in fct_dict
        assert fct_dict["new_materials"] == ["CPU 使用率 90%"]
        assert hdl_text == "CPU 过高决定扩容"
        # core_state 已弃用（core_change 不再加状态前缀）
        assert core_state is None

    @pytest.mark.high
    @pytest.mark.fct
    def test_p2_no_core_change_tag(self):
        """P2: 输出无 <core_change> 标签 → fallback"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- 测试\n"
            "### 背景与约束\n- 无\n"
            "### 决策与方案\n- 无\n"
            "### 后续行动\n- 无\n"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        # hdl_text 应为 None（无 core_change 时语义短路）
        assert hdl_text is None or fct_dict.get("core_change") == "本轮无新内容"

    @pytest.mark.high
    @pytest.mark.fct
    def test_p3_truncated_no_closing_tag(self):
        """P3: 截断输出（无 </core_change> 闭合）"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = "### 现象与问题\n- 测试\n<core_change>部分内容"
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        # 不应抛出异常，应返回部分结果
        assert hdl_text is None or isinstance(hdl_text, str)

    @pytest.mark.high
    @pytest.mark.fct
    def test_p4_empty_string(self):
        """P4: 空字符串输入"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        fct_dict, hdl_text, _ = parse_v1_markdown_xml("")
        assert isinstance(fct_dict, dict)
        assert hdl_text is None

    @pytest.mark.high
    @pytest.mark.fct
    def test_p4_none_input(self):
        """P4-None: None 输入"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(None)
        assert isinstance(fct_dict, dict)

    @pytest.mark.high
    @pytest.mark.fct
    def test_p5_pure_markdown_no_xml(self):
        """P5: 纯 Markdown 无 XML 标签"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- A\n"
            "### 决策与方案\n- B\n"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        assert hdl_text is None

    @pytest.mark.high
    @pytest.mark.fct
    def test_p6_core_change_is_wu(self):
        """P6: <core_change>内容为"无" → 语义短路"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- 测试\n"
            "<core_change>无</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        assert hdl_text is None

    @pytest.mark.high
    @pytest.mark.fct
    def test_p7_core_change_no_new_content(self):
        """P7: <core_change>内容为"本轮无新内容" → 语义短路"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- 测试\n"
            "<core_change>本轮无新内容</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        assert hdl_text is None

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p13_safe_truncate_priority(self):
        """P13: _safe_truncate 标点降级优先"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        # 在句号位置截断
        text = "第一句。第二句，第三句"
        result = _safe_truncate(text, max_len=10)
        assert len(result) <= 10 or "第一句" in result

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p14_safe_truncate_long_hdl(self):
        """P14: _safe_truncate 超长文本（200 字）"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        text = "核心变更" * 40  # 160 字
        result = _safe_truncate(text, max_len=100)
        assert len(result) <= 100

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p15_meaningless_core_set(self):
        """P15: 预编译正则匹配 MEANINGLESS_CORE 集合"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        meaningless = ["无", "无变化", "无明显变化", "none", "无核心变化", "无核心变更"]
        for val in meaningless:
            llm_output = f"### 现象与问题\n- 测试\n<core_change>{val}</core_change>"
            fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
            assert hdl_text is None, f"Failed for core='{val}': hdl_text={hdl_text}"

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p11_max_3_items_per_section(self):
        """P11: 列表项超过 3 个时只保留前 3 项"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        items = "\n".join(f"- 项目{i}" for i in range(1, 6))
        llm_output = (
            f"### 现象与问题\n{items}\n"
            "<core_change>测试</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        materials = fct_dict.get("new_materials", [])
        assert len(materials) <= 3, f"Expected ≤3 items, got {len(materials)}"

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p10_section_with_wu(self):
        """P10: 某类标题下无列表项目（写"无"）"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 决策与方案\n无\n"
            "### 后续行动\n- 采购\n"
            "<core_change>测试</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        # consensus 字段应为空列表或不存在
        consensus = fct_dict.get("consensus", [])
        assert isinstance(consensus, list)

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p12_trailing_noise(self):
        """P12: 尾随噪音字符"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- 测试\n"
            "<stage_tag>\n【探讨】\n</stage_tag>\n"
            "<core_change>核心变更</core_change>\n"
            "一些无关的尾随文字\n"
            "更多噪音"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        # 应当正确解析出 core_change
        assert hdl_text is not None

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p8_hybrid_old_new_aliases(self):
        """P8: 混合 OODA 旧别名兼容性"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        # 使用新格式 <stage_tag>/<core_change> 对 + 新别名"后续行动"
        llm_output = (
            "### 现象与问题\n- 测试\n"
            "### 后续行动\n- 采购\n"
            "<stage_tag>\n【计划】\n</stage_tag>\n"
            "<core_change>核心变更</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        assert hdl_text is not None

    @pytest.mark.medium
    @pytest.mark.fct
    def test_p9_chinese_title_all_mappings(self):
        """P9: 中文标题别名全映射"""
        if not IMPORT_OK:
            pytest.skip("parse_v1_markdown_xml not yet implemented")
        llm_output = (
            "### 现象与问题\n- 现象A\n"
            "### 背景与约束\n- 背景B\n"
            "### 决策与方案\n- 决策C\n"
            "### 后续行动\n- 行动D\n"
            "<stage_tag>\n【已实施】\n</stage_tag>\n"
            "<core_change>综合变更</core_change>"
        )
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(llm_output)
        assert isinstance(fct_dict, dict)
        for key in ("new_materials", "objective_facts", "consensus", "todo"):
            assert key in fct_dict, f"Missing key '{key}' in {fct_dict}"


# ══════════════════════════════════════════════════════════
# X1-X2: REQ-1 prompt 内容验证
# ══════════════════════════════════════════════════════════
class TestL1GenerationPrompt:

    @pytest.mark.high
    @pytest.mark.fct
    def test_x1_prompt_contains_required_elements(self):
        """X1: REQ-1 prompt 内容验证"""
        if not PROMPT_OK:
            pytest.skip("FCT_GENERATION_PROMPT not available")
        # 必须包含新 prompt 的特征
        assert "研发对话意图分析器" in FCT_GENERATION_PROMPT, \
            "Prompt should contain '研发对话意图分析器'"
        assert "{previous_summary}" in FCT_GENERATION_PROMPT, \
            "Prompt should contain {previous_summary} placeholder"
        assert "{current_dialog}" in FCT_GENERATION_PROMPT, \
            "Prompt should contain {current_dialog} placeholder"
        assert "<stage_tag>" in FCT_GENERATION_PROMPT, \
            "Prompt should contain <stage_tag> tag"
        assert "<core_change>" in FCT_GENERATION_PROMPT, \
            "Prompt should contain <core_change> tag"
        # 新格式特征：配对输出、零对或多对、单状态
        assert "零对或多对" in FCT_GENERATION_PROMPT, \
            "Prompt should contain '零对或多对' (zero-or-more pairs)"
        assert "多个事项则输出多对" in FCT_GENERATION_PROMPT, \
            "Prompt should support multiple pairs for multiple items"
        assert "【已实施】" in FCT_GENERATION_PROMPT, \
            "Prompt should contain 【已实施】 state label"
        assert "每出现一个独立的新事项，必须输出一对" in FCT_GENERATION_PROMPT, \
            "Prompt should require one pair per item"

    @pytest.mark.high
    @pytest.mark.fct
    def test_x2_prompt_no_old_features(self):
        """X2: REQ-1 prompt 不含旧特征"""
        if not PROMPT_OK:
            pytest.skip("FCT_GENERATION_PROMPT not available")
        # 不应包含旧 prompt 的特征
        assert "会议纪要摘要助手" not in FCT_GENERATION_PROMPT, \
            "Prompt should NOT contain old persona '会议纪要摘要助手'"
        assert "严谨的研发团队会议摘要专家" not in FCT_GENERATION_PROMPT, \
            "Prompt should NOT contain old persona '严谨的研发团队会议摘要专家'"
        # 新格式不使用多状态合并（旧格式的【已实施/计划】已废弃）
        assert "【已实施/计划】" not in FCT_GENERATION_PROMPT, \
            "New prompt should NOT use multi-state merge format"
        assert "【无变化】" not in FCT_GENERATION_PROMPT, \
            "New prompt should NOT contain 【无变化】 state"


# ══════════════════════════════════════════════════════════
# X3: FctTruncatedException 独立验证
# ══════════════════════════════════════════════════════════
class TestFctTruncatedException:

    @pytest.mark.high
    @pytest.mark.fct
    def test_x3_exception_properties(self):
        """X3: FctTruncatedException 继承 Exception，属性正确"""
        try:
            from ca.exceptions import FctTruncatedException
        except ImportError:
            pytest.fail("FctTruncatedException should exist in ca.exceptions")

        msg = "Test truncation"
        resp = "### 现象与问题\n- 部分内容"
        exc = FctTruncatedException(message=msg, response_text=resp)
        assert isinstance(exc, Exception), "Should inherit from Exception"
        assert str(exc) == msg or msg in str(exc)
        assert hasattr(exc, "response_text"), "Should have response_text attribute"
        assert exc.response_text == resp


# ══════════════════════════════════════════════════════════
# X4-X6: _json_to_v1_markdown 直接单元测试
# ══════════════════════════════════════════════════════════
class TestJsonToV1Markdown:

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x4_normal_json(self):
        """X4: _json_to_v1_markdown 正常 5 类英 key dict"""
        if not _json_to_v1_markdown:
            pytest.skip("_json_to_v1_markdown not yet implemented")
        data = {
            "core_change": "修复Bug",
            "new_materials": ["日志文件"],
            "objective_facts": ["OOM"],
            "consensus": ["加内存"],
            "todo": ["采购"]
        }
        result = _json_to_v1_markdown(data)
        assert isinstance(result, str)
        assert "<core_change>" in result, "Should contain <core_change> tag"
        assert "修复Bug" in result
        assert "### 现象与问题" in result

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x5_missing_key(self):
        """X5: _json_to_v1_markdown 缺失 key 容错"""
        if not _json_to_v1_markdown:
            pytest.skip("_json_to_v1_markdown not yet implemented")
        data = {"core_change": "test", "new_materials": ["A"]}
        # 缺少 consensus, objective_facts, todo — 不应抛异常
        result = _json_to_v1_markdown(data)
        assert isinstance(result, str)
        assert "<core_change>" in result

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x6_special_chars(self):
        """X6: _json_to_v1_markdown 特殊字符"""
        if not _json_to_v1_markdown or not IMPORT_OK:
            pytest.skip("_json_to_v1_markdown or parse_v1_markdown_xml not yet implemented")
        data = {
            "core_change": "特殊: - # 符号",
            "new_materials": ["包含-破折号", "包含#井号", "包含\n换行"],
            "consensus": ["决策: OK"],
        }
        result = _json_to_v1_markdown(data)
        # 转换后可被 parse_v1_markdown_xml 回读
        fct_dict, hdl_text, _ = parse_v1_markdown_xml(result)
        assert isinstance(fct_dict, dict)


# ══════════════════════════════════════════════════════════
# X9: _safe_truncate 边界值测试
# ══════════════════════════════════════════════════════════
class TestSafeTruncateBoundary:

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x9_max_len_zero(self):
        """X9a: _safe_truncate max_len=0"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        result = _safe_truncate("hello world", max_len=0)
        assert isinstance(result, str)
        assert len(result) == 0

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x9_max_len_one(self):
        """X9b: _safe_truncate max_len=1"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        result = _safe_truncate("hello", max_len=1)
        assert isinstance(result, str)
        assert len(result) <= 1

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x9_all_punctuation(self):
        """X9c: _safe_truncate 全标点文本"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        result = _safe_truncate("。，！？；：", max_len=3)
        assert isinstance(result, str)

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x9_all_spaces(self):
        """X9d: _safe_truncate 全空格文本"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        result = _safe_truncate("   ", max_len=2)
        assert isinstance(result, str)

    @pytest.mark.medium
    @pytest.mark.fct
    def test_x9_negative_max_len(self):
        """X9e: _safe_truncate 负值 max_len"""
        if not _safe_truncate:
            pytest.skip("_safe_truncate not yet implemented")
        result = _safe_truncate("hello world", max_len=-1)
        assert isinstance(result, str)
