"""theme 归并决策 — ca/theme.py（单向否决，v6.5 重构版）。

设计决策对照（wiki theme 重构方案 v4/v5）:
  → 归并决策 = 向量 0.70 门槛（≥0.70 才成候选）+ 4B 在候选中选一或全拒新建
  → 单向否决（merge 窄）：4B 不主动拉入 <0.70 的 theme；
    无候选的 strand 直接 new（不进 4B 决策）
  → 宁分不并：不确定时选 new（例外留给精炼轮）
  → 每主题块 1 次轻量决策调用（只含"有候选"的 strands）

覆盖:
  - decide prompt 构建: strand + 候选（title/overview/sim 标注）+ merge/new 选项
  - 无候选 strand 不进入决策（直接 new）— 单向否决
  - 解析: 正常 JSON / merge 无 target / 非法 JSON → 全 new 兜底
  - 不确定时宁分不并（prompt 规则）
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.theme as theme_mod


def _candidate(title: str, sim: float, overview: str = "") -> dict:
    # v7 (决策 38): 候选标注从 sim 改为 s_score（图接近度）
    return {"title": title, "overview": overview, "s_score": sim}


def _strand(hdl: str = "连接池优化", candidates: list | None = None) -> dict:
    st = {
        "hdl": hdl,
        "turns": [7, 8],
        "ooda": {"决策与方案": ["连接池上限调至 200"]},
    }
    if candidates is not None:
        st["candidates"] = candidates
    return st


class TestBuildDecidePrompt:
    """决策 prompt 构建。"""

    def test_contains_strand_hdl(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [_candidate("连接池与超时配置优化", 0.85)])])
        assert "连接池优化" in prompt

    def test_contains_candidate_title_and_sim(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [_candidate("连接池与超时配置优化", 0.85)])])
        assert "连接池与超时配置优化" in prompt
        assert "0.85" in prompt

    def test_contains_candidate_overview(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化",
                     [_candidate("连接池与超时配置优化", 0.85, "围绕连接池耗尽调优")])])
        assert "围绕连接池耗尽调优" in prompt

    def test_merge_and_new_options(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [_candidate("连接池与超时配置优化", 0.85)])])
        assert "merge" in prompt
        assert "new" in prompt

    def test_multiple_candidates_indexed(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [
                _candidate("候选甲", 0.88),
                _candidate("候选乙", 0.71),
            ])])
        assert "候选甲" in prompt and "候选乙" in prompt

    def test_dont_merge_rule(self):
        """宁分不并：不确定时选 new。"""
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [_candidate("连接池与超时配置优化", 0.85)])])
        assert "new" in prompt
        assert "不确定" in prompt or "宁分" in prompt or "另立" in prompt

    def test_json_instruction(self):
        prompt = theme_mod.build_decide_prompt(
            [_strand("连接池优化", [_candidate("连接池与超时配置优化", 0.85)])])
        assert "JSON" in prompt and "assignments" in prompt


class TestSingleVeto:
    """单向否决：无候选 strand 直接 new，不进 4B 决策。"""

    def test_strand_without_candidates_not_in_prompt(self):
        """无候选（sim < 0.70）→ 决策 prompt 不含该 strand。"""
        strands = [
            _strand("有候选的", [_candidate("主题甲", 0.85)]),
            _strand("孤单工作线", []),        # < 0.70，无候选
        ]
        prompt = theme_mod.build_decide_prompt(strands)
        assert "有候选的" in prompt
        assert "孤单工作线" not in prompt

    def test_filter_returns_strands_without_candidates(self):
        """filter 层：返回无候选的 strand（调用方直接 new）。"""
        strands = [
            _strand("有候选的", [_candidate("主题甲", 0.85)]),
            _strand("无候选的", []),
        ]
        no_cand = theme_mod.filter_strands_without_candidates(strands)
        assert [s["hdl"] for s in no_cand] == ["无候选的"]

    def test_assignments_default_new_for_no_candidates(self):
        """决策结果：无候选 strand 归为 new。"""
        strands = [
            _strand("无候选的", []),
        ]
        result = theme_mod.resolve_assignments(strands, {})
        assert result["无候选的"]["action"] == "new"


class TestParseAssignments:
    """解析：正常 / 容错 / 兜底。"""

    def test_parse_normal(self):
        text = json.dumps({
            "assignments": {
                "strand_1": {"action": "merge", "target": 0},
                "strand_2": {"action": "new"},
            }
        })
        parsed = theme_mod.parse_assignments(text)
        assert parsed["strand_1"] == {"action": "merge", "target": 0}
        assert parsed["strand_2"]["action"] == "new"

    def test_parse_merge_without_target_defaults_new(self):
        """merge 但无 target（或 target 非法）→ 降级 new（宁分不并）。"""
        text = json.dumps({
            "assignments": {"strand_1": {"action": "merge"}}
        })
        parsed = theme_mod.parse_assignments(text)
        assert parsed["strand_1"]["action"] == "new"

    def test_parse_merge_target_out_of_range_preserved(self):
        """target=99 是合法 int，parse 保留；越界校验在 resolve 层（有候选数上下文）。"""
        text = json.dumps({
            "assignments": {"strand_1": {"action": "merge", "target": 99}}
        })
        parsed = theme_mod.parse_assignments(text)
        assert parsed["strand_1"] == {"action": "merge", "target": 99}

    def test_parse_invalid_json_returns_empty(self):
        assert theme_mod.parse_assignments("not json{{{") == {}
        assert theme_mod.parse_assignments("") == {}
        assert theme_mod.parse_assignments(None) == {}

    def test_parse_missing_assignments_key_returns_empty(self):
        assert theme_mod.parse_assignments('{"foo": 1}') == {}

    def test_parse_unknown_action_defaults_new(self):
        text = json.dumps({
            "assignments": {"strand_1": {"action": "maybe"}}
        })
        parsed = theme_mod.parse_assignments(text)
        assert parsed["strand_1"]["action"] == "new"


class TestResolveAssignments:
    """决策结果合并：4B 结果 + 无候选默认 new → 完整 assignments。"""

    def test_resolve_merges_llm_and_default(self):
        strands = [
            _strand("有候选的", [_candidate("主题甲", 0.85)]),
            _strand("无候选的", []),
        ]
        llm_result = {"有候选的": {"action": "merge", "target": 0}}
        resolved = theme_mod.resolve_assignments(strands, llm_result)
        assert resolved["有候选的"] == {"action": "merge", "target": 0}
        assert resolved["无候选的"]["action"] == "new"

    def test_resolve_all_new_on_llm_failure(self):
        strands = [
            _strand("有候选的", [_candidate("主题甲", 0.85)]),
            _strand("无候选的", []),
        ]
        resolved = theme_mod.resolve_assignments(strands, {})
        assert resolved["有候选的"]["action"] == "new"
        assert resolved["无候选的"]["action"] == "new"

    def test_resolve_rejects_out_of_range_target(self):
        """target 越界（超出候选数）→ 降级 new（宁分不并）。"""
        strands = [_strand("工作线甲", [_candidate("主题甲", 0.9)])]  # 1 个候选
        resolved = theme_mod.resolve_assignments(
            strands, {"strand_1": {"action": "merge", "target": 5}})
        assert resolved["工作线甲"]["action"] == "new"

    def test_resolve_keys_by_hdl(self):
        """assignments 以 strand hdl 为 key。"""
        strands = [_strand("工作线甲", [_candidate("主题甲", 0.9)])]
        resolved = theme_mod.resolve_assignments(strands, {"工作线甲": {"action": "merge", "target": 0}})
        assert resolved["工作线甲"]["action"] == "merge"
