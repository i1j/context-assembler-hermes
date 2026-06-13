"""test_aligned_outcomes.py — PR3 1:1 对齐注入新函数覆盖

覆盖：
- _format_single_tool() 降级三路
- _merge_consecutive_tool_outcomes() ×N 合并
- _build_aligned_outcomes() 三种输出类型
- read_tool_rows_for_group() DB 查询
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
from typing import Any, Dict, List, Optional

import pytest

from ca import ContextAssembler, TurnPlanEntry, _AssemblePlanResult

TEST_SESSION = "test"

# ──────────────────────────────────────────────
# _format_single_tool 降级链测试
# ──────────────────────────────────────────────

class TestFormatSingleTool:

    def test_fst_l1_json_with_status(self, ca_engine):
        """l1_text 含 tool_name, result_summary, status=error → 显示错误"""
        l1 = json.dumps({"tool_name": "read_file", "result_summary": "file not found", "status": "error"})
        text = ca_engine._format_single_tool(l1)
        assert "read_file" in text
        assert "file not found" in text
        assert "error" in text

    def test_fst_l1_json_ok_no_status(self, ca_engine):
        """status=ok 不显示 (ok)"""
        l1 = json.dumps({"tool_name": "search_files", "result_summary": "找到3个", "status": "ok"})
        text = ca_engine._format_single_tool(l1)
        assert "search_files" in text
        assert "找到3个" in text
        assert "(ok)" not in text

    def test_fst_l0_fallback(self, ca_engine):
        """无 l1_text，用 l0_text"""
        l0 = "单行简略"
        text = ca_engine._format_single_tool(l1_text="", l0_text=l0, tool_name="read_file")
        assert "read_file" in text
        assert "单行简略" in text

    def test_fst_content_fallback(self, ca_engine):
        """无 l1/l0，用 content 前 60 字"""
        content = "这是一个很长的文件内容，超过了六十个字符的上限会被截断..." * 3
        text = ca_engine._format_single_tool(l1_text="", l0_text="",
                                              content=content, tool_name="t")
        assert "t" in text
        assert "这是一个很长的文件内容" in text

    def test_fst_all_empty(self, ca_engine):
        """全空返回空字符串"""
        text = ca_engine._format_single_tool("", "", "", "")
        assert text == ""

    def test_fst_raw_string_l1(self, ca_engine):
        """l1_text 是不可解析字符串 → 降级"""
        text = ca_engine._format_single_tool("不可解析的纯文本",
                                              tool_name="t", content="raw")
        assert "t" in text
        assert "raw" in text


# ──────────────────────────────────────────────
# _merge_consecutive_tool_outcomes 合并测试
# ──────────────────────────────────────────────

class TestMergeConsecutiveToolOutcomes:

    def test_mct_merge_three(self, ca_engine):
        """连续 3 条相同 → 合并为 ×3"""
        outcomes = ["read_file: aaa", "read_file: aaa", "read_file: aaa"]
        history = [
            {"role": "tool", "_api_call_count": 1},
            {"role": "tool", "_api_call_count": 1},
            {"role": "tool", "_api_call_count": 1},
        ]
        merged = ca_engine._merge_consecutive_tool_outcomes(outcomes, history)
        assert merged[0] == "read_file: aaa ×3"
        assert merged[1] == ""
        assert merged[2] == ""

    def test_mct_no_merge_different(self, ca_engine):
        """内容不同不合并"""
        outcomes = ["read_file: aaa", "search_files: bbb"]
        history = [
            {"role": "tool", "_api_call_count": 1},
            {"role": "tool", "_api_call_count": 1},
        ]
        merged = ca_engine._merge_consecutive_tool_outcomes(outcomes, history)
        assert merged[0] == "read_file: aaa"
        assert merged[1] == "search_files: bbb"

    def test_mct_cross_group_no_merge(self, ca_engine):
        """不同 api_call_count 不跨组合并"""
        outcomes = ["read_file: aaa", "read_file: aaa"]
        history = [
            {"role": "tool", "_api_call_count": 1},  # 组1
            {"role": "tool", "_api_call_count": 2},  # 组2
        ]
        merged = ca_engine._merge_consecutive_tool_outcomes(outcomes, history)
        # 内容相同但跨组，应各自独立
        assert merged[0] == "read_file: aaa"
        assert merged[1] == "read_file: aaa"

    def test_mct_old_format_no_api_count(self, ca_engine):
        """旧格式 _api_call_count=None 跳过合并"""
        outcomes = ["tool: x", "tool: x", "tool: x"]
        history = [
            {"role": "tool"},          # 无 _api_call_count
            {"role": "tool"},          # 无
            {"role": "assistant"},     # 非 tool
        ]
        merged = ca_engine._merge_consecutive_tool_outcomes(outcomes, history)
        # 不修改，保持原样
        assert merged == outcomes

    def test_mct_single_noop(self, ca_engine):
        """只有一条不合并"""
        outcomes = ["read_file: aaa"]
        history = [{"role": "tool", "_api_call_count": 1}]
        merged = ca_engine._merge_consecutive_tool_outcomes(outcomes, history)
        assert merged == outcomes


# ──────────────────────────────────────────────
# _format_tool_group_assembly 边缘场景
# ──────────────────────────────────────────────

class TestFormatToolGroupAssembly:

    def test_fta_old_json_format(self, ca_engine):
        """旧 JSON 格式（有 thought 字段）→ 提取 thought"""
        l1 = json.dumps({
            "group_intent": "查文件", "group_result": "aaa",
            "tool_count": 2, "state": "ok",
            "thought": "我想查一下文件系统",
        }, ensure_ascii=False)
        text = ca_engine._format_tool_group_assembly(l1)
        assert "我想查一下文件系统" in text

    def test_fta_old_json_no_thought(self, ca_engine):
        """旧 JSON 格式（无 thought）→ 降级到 group_intent"""
        l1 = json.dumps({
            "group_intent": "查文件", "group_result": "aaa",
            "tool_count": 2, "state": "ok",
        }, ensure_ascii=False)
        text = ca_engine._format_tool_group_assembly(l1)
        assert "查文件" in text

    def test_fta_new_plain_text(self, ca_engine):
        """新格式纯文本 → 直接返回"""
        text = ca_engine._format_tool_group_assembly("我想查一下文件系统")
        assert "我想查一下文件系统" in text

    def test_fta_empty(self, ca_engine):
        """空输入 → 空字符串"""
        assert ca_engine._format_tool_group_assembly("") == ""
        assert ca_engine._format_tool_group_assembly(None) == ""

    def test_fta_truncation(self, ca_engine):
        """超长文本截断"""
        long_text = "a" * 200
        text = ca_engine._format_tool_group_assembly(long_text)
        assert len(text) <= 105  # _safe_truncate 100 + 允许少量余量


# ──────────────────────────────────────────────
# generate_group_summary 空 thought 场景
# ──────────────────────────────────────────────

class TestGenerateGroupSummary:

    def test_ggs_thought_truncation(self):
        """有 thought 时返回截断文本"""
        from ca.tool_summarizer import ToolSummarizer
        result = ToolSummarizer.generate_group_summary("我想查一下文件系统")
        assert isinstance(result, str)
        assert "文件系统" in result

    def test_ggs_empty_thought_fallback(self):
        """thought 空时返回空字符串"""
        from ca.tool_summarizer import ToolSummarizer
        result = ToolSummarizer.generate_group_summary("")
        assert result == ""

    def test_ggs_none_thought_fallback(self):
        """None thought 时返回空字符串"""
        from ca.tool_summarizer import ToolSummarizer
        result = ToolSummarizer.generate_group_summary(None)
        assert result == ""

    def test_ggs_sentence_truncation(self):
        """句尾截断：超过 100 字时在 。处断开"""
        from ca.tool_summarizer import ToolSummarizer
        long_thought = "先查询用户信息表了解用户的基本情况。" + "再处理".join(["的" * 20] * 10)
        long_thought += "。最后输出结果"
        result = ToolSummarizer.generate_group_summary(long_thought)
        assert len(result) <= 105  # 允许少量超额
        assert result.endswith("。") or result.endswith("。")  # 句号结尾

    def test_ggs_no_llm_call(self):
        """generate_group_summary 不调 LLM"""
        from ca.tool_summarizer import ToolSummarizer
        result = ToolSummarizer.generate_group_summary("thought test")
        assert isinstance(result, str)

    def test_ggs_transition_phrase_excluded(self):
        """过渡词被剥离，后面的真正 thought 被引用"""
        from ca.tool_summarizer import ToolSummarizer
        # 仅为过渡词 → 返回空
        assert ToolSummarizer.generate_group_summary("开始。") == ""
        assert ToolSummarizer.generate_group_summary("查代码。") == ""
        assert ToolSummarizer.generate_group_summary("明白了。") == ""
        # 过渡词开头 + 真正 thought → 剥离后引用后者
        assert ToolSummarizer.generate_group_summary("开始。先定位数据源。") == "先定位数据源。"
        assert ToolSummarizer.generate_group_summary("开始审视。看看情况。") == "看看情况。"


# ──────────────────────────────────────────────
# _build_aligned_outcomes 对齐测试
# ──────────────────────────────────────────────

class TestBuildAlignedOutcomes:

    DIALOGUE_L1 = json.dumps({"core_change": "用户想查文件"}, ensure_ascii=False)
    GROUP_L1 = json.dumps({"group_intent": "查文件", "group_result": "找到aaa",
                           "tool_count": 1, "state": "ok"}, ensure_ascii=False)
    TOOL_L1 = json.dumps({"tool_name": "read_file", "result_summary": "找到aaa",
                          "status": "ok"}, ensure_ascii=False)

    def _setup_turn1(self, engine, dialogue_l1=None, dialogue_l0=None,
                     group_l1=None, tool_l1=None):
        """写入第1轮：user + assistant{tc} + tool"""
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=0, seq_index=0, role='user',
            content="查文件", l1_text=dialogue_l1 or "",
            l0_text=dialogue_l0 or "", _assemble_status=0)
        tc_json = json.dumps([
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"/a"}'}},
        ])
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=1, seq_index=0, role='assistant',
            content="thought", tool_calls_json=tc_json,
            finish_reason="tool_calls",
            l1_text=group_l1 or "", _assemble_status=0)
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=1, seq_index=1, role='tool',
            content="aaa", tool_call_id="c1",
            tool_name="read_file", status="ok",
            l1_text=tool_l1 or "", _assemble_status=0)

    def _setup_turn2_dialogue_only(self, engine):
        """写入第2轮：user + final assistant（纯对话）"""
        engine.store.write_turn(TEST_SESSION, 2,
            api_call_count=0, seq_index=0, role='user',
            content="继续", _assemble_status=0)
        engine.store.write_turn(TEST_SESSION, 2,
            api_call_count=999999, seq_index=0, role='assistant',
            content="好的", finish_reason="stop", _assemble_status=0)

    def _rebuild_cache(self, engine):
        """重建 cache 含全部数据"""
        from ca.cache import CacheBuilder
        builder = CacheBuilder(engine.store)
        engine.cache = builder.build(TEST_SESSION)

    def test_bao_dialogue_l1_returns_text(self, ca_engine):
        """dialogue L1 → outcomes[str]"""
        engine = ca_engine
        self._setup_turn1(engine, dialogue_l1=self.DIALOGUE_L1,
                          group_l1=self.GROUP_L1, tool_l1=self.TOOL_L1)
        self._rebuild_cache(engine)

        plan = [
            TurnPlanEntry(turn_index=1, turn_type="dialogue",
                          target_level="L1", decision_reason="middle",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
            TurnPlanEntry(turn_index=1, turn_type="tool_group",
                          tool_sub_index=1, api_call_count=1, seq_index=0,
                          target_level="L1", decision_reason="dialogue_downgrade",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
        ]
        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "tool_calls": [{}], "content": "thought"},
            {"role": "tool", "content": "aaa", "tool_call_id": "c1",
             "name": "read_file", "_api_call_count": 1, "_seq_index": 1},
            {"role": "assistant", "content": "好的", "finish_reason": "stop"},
        ]
        outcomes = engine._build_aligned_outcomes(plan, history)
        assert len(outcomes) == 4
        # dialogue L1 → None（保留用户原文）
        assert outcomes[0] is None, "user row should preserve original input"
        # tool_group L1 → group summary
        assert outcomes[1] is not None, "tool_group L1 should produce text"
        # tool L1 → tool summary
        assert outcomes[2] is not None, "tool L1 should produce text"
        # final assistant → None
        assert outcomes[3] is None, "final assistant should be None"

    def test_bao_dialogue_l2_returns_none(self, ca_engine):
        """dialogue L2 → outcomes[None]"""
        engine = ca_engine
        self._setup_turn1(engine, dialogue_l1=self.DIALOGUE_L1,
                          group_l1=self.GROUP_L1, tool_l1=self.TOOL_L1)
        self._rebuild_cache(engine)

        plan = [
            TurnPlanEntry(turn_index=1, turn_type="dialogue",
                          target_level="L2", decision_reason="tail",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
            TurnPlanEntry(turn_index=1, turn_type="tool_group",
                          tool_sub_index=1, api_call_count=1, seq_index=0,
                          target_level="L2", decision_reason="tail",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
        ]
        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "tool_calls": [{}], "content": "thought"},
            {"role": "tool", "content": "aaa", "tool_call_id": "c1",
             "name": "read_file", "_api_call_count": 1, "_seq_index": 1},
            {"role": "assistant", "content": "好的", "finish_reason": "stop"},
        ]
        outcomes = engine._build_aligned_outcomes(plan, history)
        assert outcomes[0] is None, "dialogue L2 should return None"
        assert outcomes[1] is None, "tool_group L2 should return None"
        assert outcomes[2] == "", "tool L2 should be removed (empty string)"

    def test_bao_tool_l0_skips(self, ca_engine):
        """dialogue L0 → l0 摘要, tool 行无 tool_group entry → None"""
        engine = ca_engine
        self._setup_turn1(engine, dialogue_l0="简略摘要")
        self._rebuild_cache(engine)

        plan = [
            TurnPlanEntry(turn_index=1, turn_type="dialogue",
                          target_level="L0", decision_reason="topic_degraded",
                          l2_tokens=10, summary_tokens=5, tokens_saved=5),
            # tool_group 完全不存在于 plan 中（L0 不生成 tool_group entry）
        ]
        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "tool_calls": [{}], "content": "thought"},
            {"role": "tool", "content": "aaa", "tool_call_id": "c1",
             "name": "read_file", "_api_call_count": 1, "_seq_index": 1},
            {"role": "assistant", "content": "好的", "finish_reason": "stop"},
        ]
        outcomes = engine._build_aligned_outcomes(plan, history)
        # dialogue L0 → None（保留用户原文）
        assert outcomes[0] is None, "user row should preserve original input"
        # assistant{tc} 和 tool 在 plan 无 tool_group entry → None / ""
        assert outcomes[1] is None
        assert outcomes[2] == "", "tool without plan entry should be removed"
        # assistant_fin → None
        assert outcomes[3] is None

    def test_bao_system_returns_none(self, ca_engine):
        """system 行始终 None"""
        engine = ca_engine
        self._rebuild_cache(engine)
        plan: list = []
        history = [{"role": "system", "content": "你是一个助手"}]
        outcomes = engine._build_aligned_outcomes(plan, history)
        assert outcomes[0] is None

    def test_bao_empty_plan_all_none(self, ca_engine):
        """空 plan → 全部 None"""
        engine = ca_engine
        self._setup_turn1(engine)
        self._rebuild_cache(engine)
        plan: list = []
        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "tool_calls": [{}], "content": "thought"},
            {"role": "tool", "content": "aaa", "tool_call_id": "c1",
             "name": "read_file", "_api_call_count": 1, "_seq_index": 1},
        ]
        outcomes = engine._build_aligned_outcomes(plan, history)
        # user → None, assistant{tc} → None, tool → ""（v5.1 全部删除）
        assert len(outcomes) == 3
        assert outcomes[0] is None
        assert outcomes[1] is None
        assert outcomes[2] == ""


# ──────────────────────────────────────────────
# read_tool_rows_for_group 查询测试
# ──────────────────────────────────────────────

class TestReadToolRowsForGroup:

    def test_rtg_normal(self, ca_engine):
        """读取工具组内所有工具行"""
        engine = ca_engine
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=1, seq_index=0, role='assistant',
            content="thought", tool_calls_json="[]",
            finish_reason="tool_calls", _assemble_status=0)
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=1, seq_index=1, role='tool',
            content="aaa", tool_call_id="c1",
            tool_name="read_file", status="ok",
            l1_text='{"tool_name":"read_file","result_summary":"aaa","status":"ok"}',
            _assemble_status=0)
        engine.store.write_turn(TEST_SESSION, 1,
            api_call_count=1, seq_index=2, role='tool',
            content="bbb", tool_call_id="c2",
            tool_name="search_files", status="ok",
            l1_text='{"tool_name":"search_files","result_summary":"bbb","status":"ok"}',
            _assemble_status=0)

        rows = engine.store.read_tool_rows_for_group(TEST_SESSION, 1, 1)
        assert len(rows) == 2
        assert rows[0]["tool_name"] == "read_file"
        assert rows[0]["seq_index"] == 1
        assert rows[1]["tool_name"] == "search_files"
        assert rows[1]["seq_index"] == 2

    def test_rtg_empty(self, ca_engine):
        """无工具行时返回空列表"""
        engine = ca_engine
        rows = engine.store.read_tool_rows_for_group(TEST_SESSION, 99, 1)
        assert rows == []


# ──────────────────────────────────────────────
# _annotation_mode 拼接测试
# ──────────────────────────────────────────────

class TestAnnotationMode:
    """annotation_mode 拼接测试（direct via engine outcome extraction）"""

    def test_annotation_extracts_non_none(self, ca_engine):
        """annotation_mode 拼接非 None outcomes"""
        plan: list = []
        history = [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "content": "结果", "finish_reason": "stop"},
        ]
        outcomes = ca_engine._build_aligned_outcomes(plan, history)
        # 空 plan → 全部 None
        parts = [o for o in outcomes if o]
        assert parts == []

    def test_annotation_degraded_returns_none(self):
        """annotation_mode degraded path 返回 None"""
        import pathlib as _pl, sys as _sys, importlib as _il
        _f = _pl.Path(__file__).resolve().parent.parent / "__init__.py"
        _s = _il.util.spec_from_file_location("_ca_at", _f)
        _m = _il.util.module_from_spec(_s)
        _sys.modules["_ca_at"] = _m
        _s.loader.exec_module(_m)
        p = _m.CAContextAssemblerPlugin()
        text = p._annotation_mode([{"role": "user", "content": "xxx"}], [])
        assert text is None
