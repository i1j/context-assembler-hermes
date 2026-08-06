"""reality 归并链路测试 — 决策 41 §4.3 规划 M-1~M-5。

背景（审计 BUG-01/D2）：find_s_candidates 只读 theme_id 键，reality 链路
传入 reality_id 键 dict → 恒空候选 → 运行时零归并（全部 create new）。
本文件覆盖：
  M-1 注入锚 S=0 必进（reality 键输入回归）
  M-2 共现边候选（一跳 S=0.25 进候选；无边排除）
  M-3 4B decide merge 分组（消费点 cand.get('reality_id') 生效）
  M-4 create 兜底（无候选 → 宁分不并新建）
  M-5 空注入冷启动（空锚无候选 + 余弦退化候选）

设计对照:
  → 决策 38 §五：S(r) = Σ w_a·d(r,a) / Σ w_a（注入 α / 融合 β）
  → 决策 41 §2.4b ④：范围空 → 空注入宁缺勿错
  → 决策 38 §8：无注入锚（空注入/冷启动）退化余弦保持可用
"""

import json
import sys
from pathlib import Path

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

from ca.reality import run_reality_merge  # noqa: E402
from ca.store import create_reality, load_all_realities  # noqa: E402
from ca.theme import find_s_candidates  # noqa: E402


def _reality(rid: int, name: str = "") -> dict:
    return {
        "reality_id": rid,
        "name": name or f"R{rid}",
        "hdl": f"H{rid}",
        "current_status": {
            "current_state": [f"状态{rid}"],
            "key_facts": [],
            "goals": [f"目标{rid}"],
            "context": [],
        },
        "timeline": [],
        "source_strands": {},
        "profile": "test",
        "centroid": None,
        "query_centroid": None,
        "query_count": 0,
        "health_score": 0,
        "flagged_for_review": 0,
        "topic_count": 0,
    }


def _strand(**overrides) -> dict:
    strand = {
        "hdl": "GGUF 模型加载链路修复",
        "turns": [1, 2],
        "topic_id": 1,
        "session_id": "sess-A",
        "strand_id": 101,
        "ooda": {
            "现象与问题": ["GGUF 加载 OOM"],
            "背景与约束": ["内存受限"],
            "决策与方案": ["分块加载"],
            "后续行动": ["验证分块加载"],
        },
        "changes": ["确认分块加载方案"],
        "query_text": "GGUF 模型加载失败怎么排查？",
    }
    strand.update(overrides)
    return strand


_MERGE_LLM_RESPONSE = json.dumps({
    "merge": True,
    "name": "R1 更新",
    "hdl": "新锚点",
    "current_status": {
        "current_state": ["已更新"],
        "key_facts": [],
        "goals": [],
        "context": [],
    },
})


class _FakeEmbed:
    """固定向量伪嵌入：strand 与 reality centroid 完全重合（sim=1 → s=0）。"""

    def embed(self, text):
        return [1.0, 0.0]


class TestRealityMerge:
    def test_m1_anchor_s0_must_enter(self):
        """M-1 注入锚 S=0 必进（BUG-01 回归：reality_id 键输入产生候选）。"""
        realities = [_reality(1), _reality(2), _reality(3)]
        cands = find_s_candidates(
            [{"reality_id": 1}], realities,
            cooc_edges={(1, 2): 1}, id_key="reality_id")
        assert cands, "注入锚 reality 键输入应返回非空候选"
        assert cands[0]["reality_id"] == 1
        assert cands[0]["s_score"] == 0.0
        assert cands[0]["_priority"] is True
        ids = {c["reality_id"] for c in cands}
        assert 1 in ids and 2 in ids

    def test_m2_cooc_edge_candidate(self):
        """M-2 共现边候选：一跳伙伴 S=0.25 进候选；无边 reality 排除。"""
        realities = [_reality(1), _reality(2), _reality(3)]
        cands = find_s_candidates(
            [{"reality_id": 1}], realities,
            cooc_edges={(1, 2): 3}, id_key="reality_id")
        c2 = next(c for c in cands if c["reality_id"] == 2)
        assert abs(c2["s_score"] - 0.25) < 1e-9
        assert 3 not in {c["reality_id"] for c in cands}

    def test_m3_4b_decide_merge_grouping(self, tmp_path):
        """M-3 4B decide merge 分组：消费点 cand.get('reality_id') 生效。"""
        db = tmp_path / "ca_topics.db"
        # 预置 reality（生产路径：load_all_realities 返回库内 reality）
        for rid in (1, 2):
            create_reality(
                profile="test", name=f"R{rid}", hdl=f"H{rid}",
                current_status=_reality(rid)["current_status"],
                timeline_entry=None, source_strand=None,
                centroid_json=None, db_path=db)
        realities = load_all_realities(db_path=db)
        priority = [{"reality_id": 1, "name": "R1", "hdl": "H1"}]

        def fake_llm(prompt):
            if "assignments" in prompt:
                return '{"assignments": {"strand_1": {"action": "merge", "target": 0}}}'
            return _MERGE_LLM_RESPONSE

        stats = run_reality_merge(
            [_strand()], realities, embed_client=None,
            max_chars=4000, llm_call=fake_llm, db_path=db,
            profile="test", priority_realities=priority,
        )
        assert stats["merged"] == 1, stats
        assert stats["created"] == 0
        assert stats["reality_ids"] == [1]
        loaded = load_all_realities(db_path=db)
        r1 = next(r for r in loaded if r["reality_id"] == 1)
        assert r1["name"] == "R1 更新"

    def test_m4_create_fallback_when_no_candidates(self, tmp_path):
        """M-4 create 兜底：无候选/空注入 → 新建 reality（宁分不并）。"""
        db = tmp_path / "ca_topics.db"
        stats = run_reality_merge(
            [_strand(strand_id=202)], [], embed_client=None,
            llm_call=lambda p: json.dumps({
                "name": "新工作线",
                "hdl": "新锚",
                "current_status": {
                    "current_state": ["s"], "key_facts": [],
                    "goals": [], "context": [],
                },
            }),
            db_path=db, profile="test",
        )
        assert stats["created"] == 1, stats
        assert stats["merged"] == 0

    def test_m5_empty_injection_cold_start(self, tmp_path):
        """M-5 空注入冷启动：空锚无候选；embed 可用时余弦候选进 merge。"""
        # 空注入 → 空锚 → 无候选（决策 41 §2.4b ④ 宁缺勿错）
        assert find_s_candidates(
            [], [_reality(1)], cooc_edges={(1, 2): 1},
            id_key="reality_id") == []
        # 冷启动余弦退化（决策 38 §8）：strand 向量与 reality centroid 重合 → s=0
        db = tmp_path / "ca_topics.db"
        create_reality(
            profile="test", name="R1", hdl="H1",
            current_status=_reality(1)["current_status"],
            timeline_entry=None, source_strand=None,
            centroid_json=json.dumps([1.0, 0.0]), db_path=db)
        realities = load_all_realities(db_path=db)

        def fake_llm(prompt):
            if "assignments" in prompt:
                return '{"assignments": {"strand_1": {"action": "merge", "target": 0}}}'
            return _MERGE_LLM_RESPONSE

        stats = run_reality_merge(
            [_strand()], realities, embed_client=_FakeEmbed(),
            llm_call=fake_llm, db_path=db, profile="test",
        )
        assert stats["merged"] == 1, stats
        assert stats["created"] == 0
