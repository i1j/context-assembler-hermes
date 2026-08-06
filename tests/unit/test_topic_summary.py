"""话题摘要 wiki merge — merge_wiki_summaries / _wiki_merge_fallback。

设计决策对照:
  → TP-006: wiki entry 归并（4B 合并 + 规则降级）

覆盖:
  - merge_wiki_summaries: 4B 成功合并 / 4B 失败走 fallback / title 保留 / 字段补全
  - _wiki_merge_fallback: 纯规则去重合并 / 空字段 / 重复值去重
"""

from unittest.mock import patch

import json

import pytest


# ── merge_wiki_summaries ──


class TestMergeWikiSummaries:
    """merge_wiki_summaries — 4B 合并 + fallback 降级。"""

    def _existing(self, **overrides):
        d = {
            "title": "API 改造",
            "changes": ["重构认证", "新增限流"],
            "key_facts": ["改用 JWT", "上线 QPS 500"],
            "open_items": ["补充日志"],
        }
        d.update(overrides)
        return d

    def _new(self, **overrides):
        d = {
            "title": "用户认证优化",
            "changes": ["重构认证", "优化 Redis 缓存"],
            "key_facts": ["改用 JWT", "缓存命中率 90%"],
            "open_items": ["压力测试"],
        }
        d.update(overrides)
        return d

    def test_llm_success_returns_merged(self):
        """4B 成功返回 → 保留原 title + 使用 LLM 输出。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing()
        new_item = self._new()

        fake_llm = {
            "title": "API 改造·新标题",
            "overview": "认证与限流全面升级",
            "changes": ["重构认证", "新增限流", "优化 Redis"],
            "key_facts": ["改用 JWT", "QPS 500", "缓存 90%"],
        }

        with patch("ca.topic_summary.call_llm_for_summary", return_value=fake_llm):
            result = merge_wiki_summaries(existing, new_item)

        # title 必须保留原 identity
        assert result["title"] == "API 改造"
        # overview 从 LLM 返回继承
        assert result["overview"] == "认证与限流全面升级"
        # changes 和 key_facts 来自 LLM
        assert "新增限流" in result["changes"]
        assert "缓存 90%" in result["key_facts"]

    def test_llm_failure_falls_back(self):
        """4B 返回 None → 规则降级合并。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing()
        new_item = self._new()

        with patch("ca.topic_summary.call_llm_for_summary", return_value=None):
            result = merge_wiki_summaries(existing, new_item)

        # title 保留原 identity
        assert result["title"] == "API 改造"
        # fallback: 简单去重合并
        assert sorted(result["changes"]) == sorted([
            "重构认证", "新增限流", "优化 Redis 缓存",
        ])
        assert sorted(result["key_facts"]) == sorted([
            "改用 JWT", "上线 QPS 500", "缓存命中率 90%",
        ])
        assert sorted(result["open_items"]) == sorted([
            "补充日志", "压力测试",
        ])

    def test_title_preserved_ignoring_llm_title(self):
        """LLM 返回的 title 被覆盖为 existing title。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing(title="永久标题")
        new_item = self._new()

        fake_llm = {"title": "LLM 想改标题", "changes": [], "key_facts": []}
        with patch("ca.topic_summary.call_llm_for_summary", return_value=fake_llm):
            result = merge_wiki_summaries(existing, new_item)

        assert result["title"] == "永久标题"

    def test_missing_fields_filled(self):
        """LLM 返回缺失 overview/open_items → setdefault 补全。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing()
        new_item = self._new()

        # LLM 只返回 changes + key_facts（不返回 overview/open_items）
        fake_llm = {"changes": ["A"], "key_facts": ["B"]}
        with patch("ca.topic_summary.call_llm_for_summary", return_value=fake_llm):
            result = merge_wiki_summaries(existing, new_item)

        assert result["title"] == "API 改造"
        # overview 兜底 = title
        assert result["overview"] == "API 改造"
        # open_items 兜底为空列表
        assert result["open_items"] == []

    def test_llm_returns_none_falls_back_to_rule(self):
        """call_llm_for_summary 返回 None → 规则降级。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing()
        new_item = self._new()

        with patch("ca.topic_summary.call_llm_for_summary", return_value=None):
            result = merge_wiki_summaries(existing, new_item)

        assert result["title"] == "API 改造"
        assert "重构认证" in result["changes"]

    def test_empty_open_items_handled(self):
        """new_item 没有 open_items → fallback 正常处理。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing(open_items=[])
        new_item = self._new(open_items=[])

        with patch("ca.topic_summary.call_llm_for_summary", return_value=None):
            result = merge_wiki_summaries(existing, new_item)

        assert result["open_items"] == []

    def test_empty_existing_title_falls_back_to_new(self):
        """existing title 为空 → fallback 到 new title（4B 成功路径）。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing(title="")
        new_item = self._new()

        fake_llm = {"changes": ["新增限流"], "key_facts": ["缓存命中 90%"]}
        with patch("ca.topic_summary.call_llm_for_summary", return_value=fake_llm):
            result = merge_wiki_summaries(existing, new_item)

        # existing.title="" → fallback 到 new_item.title
        assert result["title"] == "用户认证优化"

    def test_empty_existing_and_new_title_fallback(self):
        """existing 和 new 都无 title → 空字符串。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing(title="")
        new_item = self._new(title="")

        fake_llm = {"changes": ["A"], "key_facts": ["B"]}
        with patch("ca.topic_summary.call_llm_for_summary", return_value=fake_llm):
            result = merge_wiki_summaries(existing, new_item)

        assert result["title"] == ""

    def test_llm_failure_empty_existing_title(self):
        """4B 失败 + existing title 为空 → fallback 到 new title。"""
        from ca.topic_summary import merge_wiki_summaries

        existing = self._existing(title="")
        new_item = self._new()

        with patch("ca.topic_summary.call_llm_for_summary", return_value=None):
            result = merge_wiki_summaries(existing, new_item)

        # _wiki_merge_fallback: existing.title or new_item.title
        assert result["title"] == "用户认证优化"


# ── _wiki_merge_fallback ──


class TestWikiMergeFallback:
    """_wiki_merge_fallback — 规则降级去重合并。"""

    def test_dedup_merge_basic(self):
        from ca.topic_summary import _wiki_merge_fallback

        result = _wiki_merge_fallback(
            {"title": "A", "changes": ["x", "y"], "key_facts": ["f1"],
             "open_items": ["todo1"]},
            {"title": "B", "changes": ["y", "z"], "key_facts": ["f2"],
             "open_items": ["todo2"]},
        )
        assert result["changes"] == ["x", "y", "z"]
        assert result["key_facts"] == ["f1", "f2"]
        assert result["open_items"] == ["todo1", "todo2"]

    def test_title_fallback_to_new(self):
        from ca.topic_summary import _wiki_merge_fallback

        result = _wiki_merge_fallback(
            {"title": "", "changes": [], "key_facts": [], "open_items": []},
            {"title": "新标题", "changes": [], "key_facts": [], "open_items": []},
        )
        # existing title="" → falsy，fallback 到 new 的 title
        assert result["title"] == "新标题"

    def test_overview_fallback_to_new_title(self):
        """overview="" 且 existing.overview 为空 → fallback 到 new_item.title。"""
        from ca.topic_summary import _wiki_merge_fallback

        result = _wiki_merge_fallback(
            {"title": "T1", "overview": "", "changes": [], "key_facts": [],
             "open_items": []},
            {"title": "T2", "changes": [], "key_facts": [], "open_items": []},
        )
        # overview=existing.overview or new_item.title → "T2"
        assert result["overview"] == "T2"


# ── v8: summarize_topic_chunk 注入预算 + 迭代提炼 ──


def _turns(n: int, per_turn: int = 1, hdl_prefix: str = "本轮完成连接池扩容") -> list:
    """构造 n 轮 Fct 数据（每轮 per_turn 条 change）。"""
    td = []
    for t in range(1, n + 1):
        changes = [f"轮次{t}变更{c}" for c in range(per_turn)]
        td.append({
            "turn": t,
            "hdl": f"{hdl_prefix}轮次{t}",
            "changes": changes,
            "tags": {c: "已实施" for c in changes},
            "ooda_tags": {c: "决策与方案" for c in changes},
            "todos": [], "user_requests": [],
            "consensus": [], "key_facts_supp": [], "new_materials": [],
        })
    return td


def _llm_summary(n_changes: int = 3, n_facts: int = 2, title: str = "话题标题",
                 long_item: bool = False) -> dict:
    """构造 4B 摘要响应。long_item=True → 单条超长（触发超预算）。"""
    item = ("这是一条非常长的变更记录，包含大量细节信息" * 50) if long_item else "变更项"
    return {
        "title": title,
        "changes": [f"{item}{i}" for i in range(n_changes)],
        "key_facts": [f"事实{i}" for i in range(n_facts)],
        "ooda_groups": {"决策与方案": [f"变更项{i}" for i in range(n_changes)]},
        "consumable": True,
    }


class TestSummarizeTopicChunkBudget:
    """v8 注入预算 + 迭代提炼：summarize_topic_chunk(max_chars=...)。"""

    def test_budget_met_single_call(self):
        """预算内一轮完成：只调 1 次 4B，返回完整结构。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        llm = Mock(return_value=_llm_summary())
        with patch("ca.topic_summary.call_llm_for_summary", llm):
            result = summarize_topic_chunk(_turns(3), max_chars=2000)
        assert llm.call_count == 1
        assert result is not None
        assert result["title"] == "话题标题"
        assert len(result["changes"]) == 3

    def test_no_budget_legacy_single_call(self):
        """max_chars=None（旧行为）：不检查预算，只调 1 次 4B。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        llm = Mock(return_value=_llm_summary())
        with patch("ca.topic_summary.call_llm_for_summary", llm):
            result = summarize_topic_chunk(_turns(3))
        assert llm.call_count == 1
        assert result is not None

    def test_over_budget_all_covered_hdl_fallback(self):
        """全轮次已覆盖（remaining 空）但超预算 → title 兜底（质量过滤后）。

        v6.4 (Bug 2 修复)：内容保留——旧断言 `changes == []` 是误 skip 的根因。
        """
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        llm = Mock(return_value=_llm_summary(long_item=True))
        with patch("ca.topic_summary.call_llm_for_summary", llm):
            result = summarize_topic_chunk(_turns(2), max_chars=2000)
        # 超预算 + 无剩余轮次 → title 兜底为 hdl（首末轮拼接），内容保留
        assert result["title"].startswith("本轮完成连接池扩容轮次1")
        assert len(result["changes"]) == 3  # 4B 摘要内容保留，不清空
        assert len(result["key_facts"]) == 2
        assert result["ooda_groups"]  # OODA 分组保留

    def test_hdl_fallback_quality_filter(self):
        """hdl 质量不过关（过短）→ 不兜底，保留 4B 摘要。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        llm = Mock(return_value=_llm_summary(long_item=True))
        # 单轮话题 → hdl 不拼接 = "效轮次1"（4 字符 <8）→ 质量过滤弃用
        turns = _turns(1, hdl_prefix="效")
        with patch("ca.topic_summary.call_llm_for_summary", llm):
            result = summarize_topic_chunk(turns, max_chars=2000)
        assert result["title"] == "话题标题"  # 保留 4B title
        assert result["changes"]  # 内容不被清空

    def test_split_batches_refines_remaining(self):
        """输入超预算分批 → 第二轮融合剩余轮次，共 2 次 4B。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        # 强制分批：输入预算极小 → 2 轮 = 2 批
        with patch("ca.config.Config.TOPIC_SUMMARY_INPUT_BUDGET", 50):
            llm = Mock(side_effect=[
                _llm_summary(title="第一版"),
                _llm_summary(title="融合版", n_changes=4),
            ])
            with patch("ca.topic_summary.call_llm_for_summary", llm):
                result = summarize_topic_chunk(_turns(2), max_chars=2000)
        assert llm.call_count == 2
        assert result["title"] == "融合版"

    def test_max_rounds_cap(self):
        """多批轮次 + 持续超预算 → 3 轮上限保护，之后 hdl 兜底。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        # 每轮一批 → 3 轮 topics → 3 次调用即到上限
        with patch("ca.config.Config.TOPIC_SUMMARY_INPUT_BUDGET", 50):
            llm = Mock(return_value=_llm_summary(long_item=True))
            with patch("ca.topic_summary.call_llm_for_summary", llm):
                result = summarize_topic_chunk(_turns(5), max_chars=2000)
        assert llm.call_count == 3
        assert result["title"].startswith("本轮完成连接池扩容轮次1")  # hdl 兜底

    def test_llm_failure_falls_back(self):
        """4B 失败（None）→ 代码 fallback（现行为不变）。"""
        from ca.topic_summary import summarize_topic_chunk
        from unittest.mock import Mock

        llm = Mock(return_value=None)
        with patch("ca.topic_summary.call_llm_for_summary", llm):
            result = summarize_topic_chunk(_turns(2), max_chars=2000)
        assert result is not None
        assert result["changes"] == ["轮次1变更0", "轮次2变更0"]  # fallback 精确去重

    def test_build_prompt_includes_budget(self):
        """prompt 携带预算指令（≤ max_chars 字符）。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert "2000" in prompt

    def test_build_prompt_with_candidates_includes_refs(self):
        """v6.5.3: 有候选 theme 时 prompt 含候选参考段 + theme_ref 指令。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(
            _turns(2), max_chars=2000,
            candidate_themes=[{"theme_id": 30, "title": "连接池与超时配置优化",
                               "overview": "当前聚焦连接池参数调优"}],
        )
        assert "候选主题参考" in prompt
        assert "连接池与超时配置优化" in prompt
        assert "theme_ref" in prompt
        # 参考段在 Input 之前
        assert prompt.index("候选主题参考") < prompt.index("Input:")

    def test_build_prompt_without_candidates_no_refs(self):
        """v6.5.3: 无候选 theme → prompt 不含参考段（零额外输入）。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert "## 候选主题参考" not in prompt  # 段标题仅 _format_candidate_refs 生成
        assert "theme_id=" not in prompt

    def test_summarize_chunk_passes_candidates_to_prompt(self):
        """v6.5.3: summarize_topic_chunk(candidate_themes=...) 透传到 prompt。"""
        from unittest.mock import patch, Mock
        from ca.topic_summary import summarize_topic_chunk

        captured = {}

        def fake_llm(prompt):
            captured["prompt"] = prompt
            return {"title": "t", "strands": [], "changes": [], "key_facts": [],
                    "consumable": False}

        with patch("ca.topic_summary.call_llm_for_summary", side_effect=fake_llm):
            summarize_topic_chunk(
                _turns(2), max_chars=2000,
                candidate_themes=[{"theme_id": 7, "title": "会话上下文",
                                   "overview": "ov"}],
            )
        assert "候选主题参考" in captured["prompt"]
        assert "7" in captured["prompt"]

    def test_build_prompt_requires_strands(self):
        """v6.4: prompt 要求多 strand 输出（宁多勿少 + turns 元数据）。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert '"strands"' in prompt, "prompt 必须要求 strands 输出"
        assert '"turns"' in prompt, "prompt 必须要求 turns 元数据"
        assert "split into separate strands" in prompt or "split" in prompt, \
            "prompt 必须包含宁多勿少指令"
        # 顶层 ooda_groups 输出被 strands 替代（ooda 作为 strand 内部字段保留）
        assert '"ooda_groups"' not in prompt

    def test_build_refine_prompt_keeps_strands(self):
        """v6.4: 融合轮 prompt 的已有摘要包含 strands，保持 JSON 结构。"""
        from ca.topic_summary import _build_refine_prompt

        summary = {
            "title": "t", "changes": ["c1"], "key_facts": ["k1"],
            "strands": [{"hdl": "s1", "turns": [1], "ooda": {"决策与方案": ["c1"]}}],
        }
        new_turns = _turns(1)
        prompt = _build_refine_prompt(summary, new_turns, max_chars=2000)
        assert "s1" in prompt, "融合轮已有摘要必须携带 strands"


# ── v6.4.3: hdl 命名规范（Fct 风格）──


class TestPromptHdlNamingRule:
    """hdl 必须在 4B 组织 ooda 后基于 ooda 总结；禁止代码符号名（Fct 风格提示词）。"""

    def test_build_prompt_requires_hdl_summary_of_ooda(self):
        """主 prompt 必须要求：hdl 基于该 strand 的 ooda 内容总结，而非抄输入符号。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert "ooda" in prompt, "prompt 必须引用 ooda 作为 hdl 总结来源"
        assert "总结" in prompt or "summar" in prompt.lower(), \
            "prompt 必须要求 hdl 是对 ooda 的总结"
        assert "hdl" in prompt, "prompt 必须提及 hdl 字段"

    def test_build_prompt_forbids_code_symbol_names(self):
        """主 prompt 必须明确禁止：代码符号名 / 英文标识符 / 文件名 / 函数名。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert "禁止" in prompt, "prompt 必须含'禁止'规则（Fct 风格）"
        assert ("代码符号" in prompt or "符号名" in prompt or "标识符" in prompt), \
            "prompt 必须明确禁止代码符号名"
        assert ("embedding_service" in prompt or "topic_find_ca" in prompt
                or "consensus" in prompt), "反例必须点名称常见的坏 hdl 样本"

    def test_build_prompt_hdl_has_good_and_bad_examples(self):
        """主 prompt 必须含正反例（Fct 风格：✅ 正确示例 + ❌ 错误示例）。"""
        from ca.topic_summary import build_summarize_prompt

        prompt = build_summarize_prompt(_turns(2), max_chars=2000)
        assert "正确示例" in prompt or "✅" in prompt, "必须含正确示例"
        assert "错误示例" in prompt or "❌" in prompt, "必须含错误示例"

    def test_build_refine_prompt_also_forbids_code_symbol_names(self):
        """融合轮 prompt 也必须含 hdl 规范（refine 同样重生成 strands）。"""
        from ca.topic_summary import _build_refine_prompt

        summary = {
            "title": "t", "changes": ["c1"], "key_facts": ["k1"],
            "strands": [{"hdl": "s1", "turns": [1], "ooda": {"决策与方案": ["c1"]}}],
        }
        prompt = _build_refine_prompt(summary, _turns(1), max_chars=2000)
        assert "禁止" in prompt, "融合轮 prompt 必须含'禁止'规则"
        assert "ooda" in prompt, "融合轮 prompt 必须要求基于 ooda 总结 hdl"



# ── v6.4 strand 重构: parse_summary_response 透传 ooda_groups/strands (Bug 1) ──


class TestParseSummaryResponseStrand:
    """Bug 1 修复：4B 输出的 ooda_groups / strands 不再被 parse 丢弃。"""

    def test_parse_summary_response_preserves_ooda_groups(self):
        from ca.topic_summary import parse_summary_response

        raw = json.dumps({
            "title": "t", "changes": ["a"],
            "ooda_groups": {"现象与问题": ["a"]},
            "key_facts": ["k"], "consumable": True,
        })
        result = parse_summary_response(raw)
        assert result is not None
        assert result["ooda_groups"] == {"现象与问题": ["a"]}

    def test_parse_summary_response_preserves_strands(self):
        from ca.topic_summary import parse_summary_response

        raw = json.dumps({
            "title": "t", "changes": ["a"],
            "strands": [{"hdl": "s1", "turns": [1, 2], "ooda": {"决策与方案": ["a"]}}],
            "key_facts": ["k"], "consumable": True,
        })
        result = parse_summary_response(raw)
        assert result is not None
        assert result["strands"][0]["hdl"] == "s1"

    def test_parse_summary_response_absent_ooda_groups(self):
        """4B 未输出 ooda_groups/strands → 不写入这两个键（旧行为兼容）。"""
        from ca.topic_summary import parse_summary_response

        raw = json.dumps({"title": "t", "changes": ["a"], "key_facts": ["k"]})
        result = parse_summary_response(raw)
        assert "ooda_groups" not in result
        assert "strands" not in result


# ── v6.4 strand 重构: _apply_hdl_fallback 不清空内容 (Bug 2) ──


class TestApplyHdlFallbackKeepsContent:
    """Bug 2 修复：超预算 hdl 兜底只降级 title，保留 changes/key_facts/ooda。"""

    def test_apply_hdl_fallback_keeps_changes(self):
        from ca.topic_summary import _apply_hdl_fallback

        summary = {"title": "t", "changes": ["c1"], "key_facts": ["k1"],
                   "open_items": ["o1"], "ooda_groups": {"决策与方案": ["c1"]}}
        out = _apply_hdl_fallback(summary, "有意义的话题hdl")
        assert out["changes"] == ["c1"], "hdl fallback 不应清空 changes"
        assert out["key_facts"] == ["k1"]
        assert out["open_items"] == ["o1"]
        assert out["ooda_groups"] == {"决策与方案": ["c1"]}
        # title 降级为 hdl
        assert out["title"] == "有意义的话题hdl"

    def test_apply_hdl_fallback_unmeaningful_keeps_all(self):
        """hdl 质量不过关 → 原摘要原样保留（不改动任何字段）。"""
        from ca.topic_summary import _apply_hdl_fallback

        summary = {"title": "t", "changes": ["c1"], "key_facts": ["k1"],
                   "open_items": ["o1"], "ooda_groups": {"决策与方案": ["c1"]}}
        out = _apply_hdl_fallback(summary, "好")
        assert out is summary
        assert out["changes"] == ["c1"]
        assert out["title"] == "t"


# ── v6.4 strand 重构: _assemble_summary 多 strand 分支 + 单 strand 兜底 ──


def _turns_for_assemble():
    return [{"turn": 1, "hdl": "h", "changes": ["a"], "tags": {}, "ooda_tags": {}}]


class TestAssembleSummaryStrands:
    """_assemble_summary — 4B 输出 strands 时透传；失败时单 strand 兜底。"""

    def test_assemble_summary_with_strands(self):
        from ca.topic_summary import _assemble_summary

        llm_result = {
            "changes": ["a"], "key_facts": ["k"], "title": "t", "consumable": True,
            "strands": [{"hdl": "s1", "turns": [1], "ooda": {"决策与方案": ["a"]}}],
        }
        turns_data = _turns_for_assemble()
        s = _assemble_summary(llm_result, turns_data, "hdl", [], [], "completed",
                              ["a"], ["k"])
        assert len(s["strands"]) == 1
        assert s["strands"][0]["hdl"] == "s1"
        assert s["strands"][0]["turns"] == [1]
        assert s["changes"] == ["a"]  # 扁平聚合保留

    def test_assemble_summary_strand_fallback_single(self):
        """4B 失败（llm_result=None）→ 单 strand 兜底。"""
        from ca.topic_summary import _assemble_summary

        turns_data = _turns_for_assemble()
        s = _assemble_summary(None, turns_data, "h", [], [], "completed", ["a"], ["k"])
        assert len(s["strands"]) == 1
        assert s["strands"][0]["hdl"] == "h"
        assert s["strands"][0]["turns"] == [1]

    def test_assemble_summary_no_strands_uses_ooda_groups(self):
        """4B 返回 ooda_groups 但无 strands → 单 strand 包装（hdl=块 hdl）。"""
        from ca.topic_summary import _assemble_summary

        llm_result = {
            "changes": ["a"], "key_facts": ["k"], "title": "t", "consumable": True,
            "ooda_groups": {"现象与问题": ["a"]},
        }
        turns_data = _turns_for_assemble()
        s = _assemble_summary(llm_result, turns_data, "块hdl", [], [], "completed",
                              ["a"], ["k"])
        assert len(s["strands"]) == 1
        assert s["strands"][0]["hdl"] == "块hdl"
        assert s["strands"][0]["ooda"] == {"现象与问题": ["a"]}

    def test_assemble_summary_changes_flat_from_ooda_dedup(self):
        """changes = 各 strand ooda 四组扁平 concat，0.95 Jaccard 去重。"""
        from ca.topic_summary import _assemble_summary

        llm_result = {
            "changes": [], "key_facts": [], "title": "t", "consumable": True,
            "strands": [
                {"hdl": "s1", "turns": [1], "ooda": {
                    "决策与方案": ["实现A"], "后续行动": ["验证A"]}},
                {"hdl": "s2", "turns": [2], "ooda": {
                    "决策与方案": ["实现A"], "现象与问题": ["发现B"]}},
            ],
        }
        turns_data = _turns_for_assemble()
        s = _assemble_summary(llm_result, turns_data, "hdl", [], [], "completed",
                              [], [])
        # 扁平聚合：4 条 → "实现A" 重复被 0.95 去重 → 3 条
        assert len(s["changes"]) == 3
        assert "实现A" in s["changes"]
        assert "验证A" in s["changes"]
        assert "发现B" in s["changes"]
        # open_items = 各 strand "后续行动" concat
        assert s["open_items"] == ["验证A"]

    def test_assemble_summary_malformed_strand_element_skipped(self):
        """4B 偶发输出 strands 数组含 str 元素（非 dict）→ 跳过该元素，不崩溃。

        2026-08-01 实测：reprocess 全量重跑中 4B 返回
        strands=[{...}, "字符串", {...}]，_flatten_strand_ooda 直接
        AttributeError → 整轮重跑中断。修复：消费端类型防御。
        """
        from ca.topic_summary import _assemble_summary

        llm_result = {
            "changes": [], "key_facts": [], "title": "t", "consumable": True,
            "strands": [
                {"hdl": "s1", "turns": [1], "ooda": {"决策与方案": ["方案A"]}},
                "这是一个字符串，不是 dict",  # 畸形元素
                {"hdl": "s2", "turns": [2], "ooda": {"现象与问题": ["发现B"]}},
            ],
        }
        turns_data = _turns_for_assemble()
        s = _assemble_summary(llm_result, turns_data, "hdl", [], [], "completed",
                              [], [])
        # 畸形元素被跳过，有效 strand 保留
        assert len(s["strands"]) == 3  # 原始透传（含畸形——写库前由调用方过滤）
        assert "方案A" in s["changes"]
        assert "发现B" in s["changes"]

    def test_flatten_strand_ooda_skips_malformed_elements(self):
        """_flatten_strand_ooda / _flatten_strand_open_items / _ooda_from_strands
        对 str 元素直接跳过，不崩溃。"""
        from ca.topic_summary import (
            _flatten_strand_ooda, _flatten_strand_open_items, _ooda_from_strands,
        )

        strands = [
            {"hdl": "s1", "ooda": {"决策与方案": ["方案A"], "后续行动": ["验证A"]}},
            "畸形字符串",
            {"hdl": "s2", "ooda": {"现象与问题": ["发现B"]}},
        ]
        assert _flatten_strand_ooda(strands) == ["方案A", "验证A", "发现B"]
        assert _flatten_strand_open_items(strands) == ["验证A"]
        ooda = _ooda_from_strands(strands)
        assert ooda["决策与方案"] == ["方案A"]
        assert ooda["现象与问题"] == ["发现B"]


# ── v6.4.3: P0+P1 修复（strand 35 超长 hdl 根因）──


class TestAssembleSummaryEmptyChangesFallback:
    """P0-1: 4B 返回 changes=[] 时必须回退代码提取，不吞掉 fallback_changes。"""

    def test_llm_empty_changes_uses_fallback_changes(self):
        """4B 输出 changes=[]（空列表）→ changes 回退 fallback_changes。"""
        from ca.topic_summary import _assemble_summary

        llm_result = {
            "changes": [], "key_facts": ["k"], "title": "t", "consumable": True,
            "strands": [{"hdl": "s1", "turns": [1], "ooda": {}}],
        }
        turns_data = _turns_for_assemble()
        s = _assemble_summary(llm_result, turns_data, "h", [], [], "completed",
                              ["代码提取变更A"], ["k"])
        assert "代码提取变更A" in s["changes"], \
            "4B 空 changes 不得覆盖代码提取的 fallback_changes"

    def test_llm_empty_changes_fallback_ooda_not_empty(self):
        """P0-1 配套：4B 空 changes 时单 strand 包装的 ooda 用 fallback 回溯，非空。"""
        from ca.topic_summary import _assemble_summary

        turns_data = [{
            "turn": 1, "hdl": "h", "changes": ["连接池扩容"],
            "tags": {}, "ooda_tags": {"连接池扩容": "决策与方案"},
        }]
        llm_result = {
            "changes": [], "key_facts": [], "title": "t", "consumable": True,
        }
        s = _assemble_summary(llm_result, turns_data, "块hdl", [], [], "completed",
                              ["连接池扩容"], [])
        strand_ooda = s["strands"][0]["ooda"]
        assert any(strand_ooda.values()), \
            f"fallback ooda 不得为空: {strand_ooda}"


class TestBuildHdlLengthLimit:
    """P0-2: _build_hdl 拼接结果加长度上限，超长截断到短名。"""

    def test_build_hdl_truncates_overlong_combined(self):
        """首末轮 Fct hdl 超长（strand 35 场景）→ 拼接结果截断 ≤30 chars。"""
        from ca.topic_summary import _build_hdl

        turns_data = [
            {"turn": 4, "hdl": "在 query_topic_summaries 的 step 1 和 step 2 中，"
                               "通过 `session_id!=?` 逻辑确保不召回本会话生成的 topic 摘要，"
                               "避免召回冗余内容，满足会话级历史信息不重复"},
            {"turn": 11, "hdl": "将 _run_topic_summarize 触发点调整至会话真正结束时，"
                                "将在下次新会话 turn 1 预热阶段执行，批量归并未入 wiki 的"
                                "completed 摘要，实现语义召回"},
        ]
        hdl = _build_hdl(turns_data)
        assert len(hdl) <= 30, f"hdl 超长未截断: {len(hdl)} chars: {hdl}"
        assert "，" not in hdl, "hdl 不应保留完整句子（含逗号分隔）"

    def test_build_hdl_keeps_short_unchanged(self):
        """短 hdl 不受影响（不误伤正常拼接）。"""
        from ca.topic_summary import _build_hdl

        turns_data = [
            {"turn": 1, "hdl": "连接池扩容"},
            {"turn": 2, "hdl": "超时配置优化"},
        ]
        hdl = _build_hdl(turns_data)
        assert "连接池扩容" in hdl
        assert len(hdl) <= 30


class TestFallbackSingleStrandShortHdl:
    """P1-2: fallback 单 strand 的 hdl 超长时降级为首条 change 摘要。"""

    def test_fallback_single_strand_shortens_overlong_hdl(self):
        """fallback hdl 超长 → 用首条 change 的核心短名替代。"""
        from ca.topic_summary import _fallback_single_strand

        turns_data = [
            {"turn": 4,
             "changes": ["在 query_topic_summaries 的 step 1 和 step 2 中，"
                         "通过 `session_id!=?` 逻辑确保不召回本会话生成的 topic 摘要"],
             "tags": {}, "ooda_tags": {}},
        ]
        long_hdl = "在 query_topic_summaries 的 step 1 和 step 2 中，通过 `session_id!=? → " \
                   "将 _run_topic_summarize 触发点调整至会话真正结束时，将在下次新会话 " \
                   "turn 1 预热阶段执行。"
        strands = _fallback_single_strand(turns_data, long_hdl)
        out_hdl = strands[0]["hdl"]
        assert len(out_hdl) <= 30, f"fallback hdl 未缩短: {len(out_hdl)}: {out_hdl}"


class TestSummarizeChunkRetryDegenerate:
    """P1-1: 4B 返回退化输出（无 strands 且无 ooda_groups）→ 重试一次。"""

    def test_retries_once_on_degenerate_llm_output(self):
        """4B 返回 {}（无 strands/ooda）→ 自动重试，第二次正常输出被采用。"""
        from unittest.mock import patch

        from ca.topic_summary import summarize_topic_chunk

        good = _llm_summary()
        good["strands"] = [{"hdl": "s1", "turns": [1],
                            "ooda": {"决策与方案": ["变更项0"]}}]
        with patch("ca.topic_summary.call_llm_for_summary",
                   side_effect=[{}, good]) as mock_llm:
            result = summarize_topic_chunk(_turns(1), max_chars=2000)
        assert mock_llm.call_count == 2, "退化输出必须重试一次"
        assert result is not None
        assert result["strands"][0]["hdl"] == "s1", "重试结果必须被采用"

    def test_no_retry_when_llm_output_has_ooda_groups(self):
        """4B 有 ooda_groups（虽无 strands）→ 不触发重试（单 strand 包装即可）。"""
        from unittest.mock import patch

        from ca.topic_summary import summarize_topic_chunk

        normal = _llm_summary()  # 含 ooda_groups
        with patch("ca.topic_summary.call_llm_for_summary",
                   return_value=normal) as mock_llm:
            result = summarize_topic_chunk(_turns(1), max_chars=2000)
        assert mock_llm.call_count == 1, "有 ooda_groups 不重试"
        assert result is not None
