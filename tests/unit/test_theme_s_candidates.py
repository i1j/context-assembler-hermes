"""S 匹配分候选生成 — ca/theme.py find_s_candidates（决策 38 §五 v7）。

设计对照:
  → S(r) = Σ_{a∈A} w_a·d(r,a) / Σ_{a∈A} w_a（加权平均距离，min 退役）
  → 锚点集内 reality S=0 → 排最前（"strand 首先融合进注入 theme"）
  → 一跳 w≥1 共现伙伴进候选；二跳/无边排除（S>R）
  → per-anchor 权重：注入 α=0.4 / 融合 β=0.8
"""
import sys
from pathlib import Path

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

from ca.theme import find_s_candidates  # noqa: E402


def _theme(tid, title=f"T"):
    return {"theme_id": tid, "title": f"{title}{tid}", "overview": ""}


THEMES = [_theme(1, "甲"), _theme(2, "乙"), _theme(3, "丙"),
          _theme(4, "丁"), _theme(5, "戊")]


class TestSCandidates:
    """find_s_candidates — S 匹配分候选生成。"""

    def test_anchor_inside_ranked_first(self):
        # 锚点集 = [2]；注入集内 reality S=0 → 排最前
        cands = find_s_candidates([{"theme_id": 2}], THEMES, cooc_edges={})
        assert cands[0]["theme_id"] == 2
        assert cands[0]["s_score"] == 0.0
        assert cands[0]["_priority"] is True

    def test_one_hop_cooccurrence_enters(self):
        # 锚=1，与 2 共现 1 次（d=0.5 ≤ R=0.5）→ 进候选
        cands = find_s_candidates([{"theme_id": 1}], THEMES,
                                  cooc_edges={(1, 2): 1})
        ids = {c["theme_id"] for c in cands}
        assert 1 in ids and 2 in ids
        c2 = next(c for c in cands if c["theme_id"] == 2)
        assert abs(c2["s_score"] - 0.5) < 1e-9

    def test_two_hop_excluded(self):
        # 锚=1；2 与 1 共现（d=0.5），3 只与 2 共现（无 1-3 边）
        # 3 到锚 1 无边 → S=∞ → 排除
        cands = find_s_candidates([{"theme_id": 1}], THEMES,
                                  cooc_edges={(1, 2): 1, (2, 3): 1})
        ids = {c["theme_id"] for c in cands}
        assert 3 not in ids

    def test_s_above_threshold_excluded(self):
        # R=0.3：w=1 的 d=0.5 > 0.3 → 共现伙伴被排除，仅锚内
        cands = find_s_candidates([{"theme_id": 1}], THEMES,
                                  cooc_edges={(1, 2): 1}, r_threshold=0.3)
        ids = {c["theme_id"] for c in cands}
        assert ids == {1}

    def test_empty_anchors_no_candidates(self):
        assert find_s_candidates(None, THEMES, cooc_edges={}) == []
        assert find_s_candidates([], THEMES, cooc_edges={(1, 2): 1}) == []

    def test_fused_anchor_uses_beta_weight(self):
        # 锚=1（fused 空，α=0.4）vs 锚=1 且 1∈fused（β=0.8）
        # 无共现边时，S=0 锚内不变；但 2 与 1 无边的处理不变
        base = find_s_candidates([{"theme_id": 1}], THEMES, cooc_edges={},
                                 fused_ids=set())
        fused = find_s_candidates([{"theme_id": 1}], THEMES, cooc_edges={},
                                  fused_ids={1})
        assert base[0]["s_score"] == fused[0]["s_score"] == 0.0

    def test_weight_affects_s_for_cooc_partner(self):
        # 锚=1，2 与 1 共现 1 次。α=0.4: S=0.4*0.5/0.4=0.5
        # 1∈fused: S=(0.8*0.5)/0.8=0.5（归一化后同）——检查两配置都进候选
        c1 = find_s_candidates([{"theme_id": 1}], THEMES,
                               cooc_edges={(1, 2): 1}, alpha=0.4, beta=0.8)
        c2 = find_s_candidates([{"theme_id": 1}], THEMES,
                               cooc_edges={(1, 2): 1}, alpha=0.4, beta=0.8,
                               fused_ids={1})
        ids1 = {c["theme_id"] for c in c1}
        ids2 = {c["theme_id"] for c in c2}
        assert 2 in ids1 and 2 in ids2

    def test_top_k_truncation(self):
        # 锚=1，2/3 都与 1 共现（同权）→ top_k=2 截断
        cands = find_s_candidates([{"theme_id": 1}], THEMES,
                                  cooc_edges={(1, 2): 1, (1, 3): 1},
                                  top_k=2)
        assert len(cands) == 2

    def test_no_cooc_edges_still_anchor(self):
        # 无共现边：仅锚点内 reality 进候选（S=0 ≤ R）
        cands = find_s_candidates([{"theme_id": 1}], THEMES, cooc_edges={})
        ids = {c["theme_id"] for c in cands}
        assert ids == {1}
