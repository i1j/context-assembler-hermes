"""第三轮 T4：R-1 图路候选扩展（决策 42）——pick_injection_realities 图邻居。

覆盖：
  - 含 co_occurs_with 边的测试图 → 候选池含邻居 reality（recall augmentation）
  - 无 graph.json / 无边 → 行为与现状一致（候选池不含邻居，回归）
  - 邻居只在 candidates 中（范围外）也补入
"""

import json

import ca.inject as inject_mod


def _mk_db(db, realities):
    """realities: [(reality_id, name, query_centroid)]。"""
    from ca.store import create_reality, update_reality
    for rid, name, qc in realities:
        rid_created = create_reality(
            profile="tester", name=name, hdl=f"H{rid}",
            current_status={"current_state": [f"状态{rid}"],
                            "key_facts": [], "goals": [], "context": []},
            timeline_entry=None, source_strand=None,
            centroid_json=json.dumps([0.0, 1.0]), db_path=db)
        assert rid_created == rid
        update_reality(reality_id=rid, query_centroid_json=json.dumps(qc),
                       db_path=db)


def _write_graph(gp, edges):
    gp.write_text(json.dumps({"nodes": [], "links": edges}),
                  encoding="utf-8")


class TestInjectGraphRoute:
    def test_neighbor_added_to_budget(self, tmp_path, monkeypatch):
        """cooc 边邻居（范围外但在 candidates）补入 4B 候选池。"""
        db = tmp_path / "ca_topics.db"
        _mk_db(db, [
            (1, "缓存方案重构", [1.0, 0.0]),   # 提问云距离 d=0 → in range
            (2, "连接池方案", [0.0, 1.0]),     # d=1.0 → 范围外，仅 candidates
            (3, "压测报告", [0.0, 1.0]),       # 无关
        ])
        gp = tmp_path / "graph.json"
        _write_graph(gp, [
            {"source": "reality_1", "target": "reality_2",
             "relation": "co_occurs_with"},
        ])
        monkeypatch.setattr(inject_mod, "_default_graph_path", lambda: gp)

        seen_budgets = []

        def fake_pick(query, budget, limit):
            seen_budgets.append([c["reality_id"] for c in budget])
            # 4B 选中 reality_1
            r1 = next(c for c in budget if c["reality_id"] == 1)
            return [r1]

        monkeypatch.setattr(inject_mod, "_pick_by_4b", fake_pick)

        result = inject_mod.pick_injection_realities(
            "缓存方案怎么重构？", [1.0, 0.0], "tester", limit=3, db_path=db)
        assert seen_budgets, "4B 应被调用"
        budget_ids = seen_budgets[0]
        assert 1 in budget_ids
        assert 2 in budget_ids, "cooc 邻居应补入候选池"
        assert result and result[0]["reality_id"] == 1

    def test_no_graph_behavior_unchanged(self, tmp_path, monkeypatch):
        """无 graph.json → 候选池不含邻居（行为与现状一致，回归）。"""
        db = tmp_path / "ca_topics.db"
        _mk_db(db, [
            (1, "缓存方案重构", [1.0, 0.0]),
            (2, "连接池方案", [0.0, 1.0]),
        ])
        gp = tmp_path / "missing_graph.json"  # 不存在
        monkeypatch.setattr(inject_mod, "_default_graph_path", lambda: gp)

        seen_budgets = []

        def fake_pick(query, budget, limit):
            seen_budgets.append([c["reality_id"] for c in budget])
            return [next(c for c in budget if c["reality_id"] == 1)]

        monkeypatch.setattr(inject_mod, "_pick_by_4b", fake_pick)
        inject_mod.pick_injection_realities(
            "缓存方案怎么重构？", [1.0, 0.0], "tester", limit=3, db_path=db)
        assert seen_budgets
        assert seen_budgets[0] == [1], "无图时候选池 = 距离 top-15（仅 reality_1）"

    def test_no_reality_edges_no_extension(self, tmp_path, monkeypatch):
        """图存在但无 reality 间边 → 无扩展。"""
        db = tmp_path / "ca_topics.db"
        _mk_db(db, [
            (1, "缓存方案重构", [1.0, 0.0]),
            (2, "连接池方案", [0.0, 1.0]),
        ])
        gp = tmp_path / "graph.json"
        _write_graph(gp, [
            {"source": "code_a", "target": "code_b", "relation": "calls"},
            {"source": "reality_1", "target": "reality_99",
             "relation": "co_occurs_with"},   # 邻居 99 不在 candidates
        ])
        monkeypatch.setattr(inject_mod, "_default_graph_path", lambda: gp)

        seen_budgets = []

        def fake_pick(query, budget, limit):
            seen_budgets.append([c["reality_id"] for c in budget])
            return [next(c for c in budget if c["reality_id"] == 1)]

        monkeypatch.setattr(inject_mod, "_pick_by_4b", fake_pick)
        inject_mod.pick_injection_realities(
            "缓存方案怎么重构？", [1.0, 0.0], "tester", limit=3, db_path=db)
        assert seen_budgets[0] == [1]

    def test_corrupt_graph_skips_route(self, tmp_path, monkeypatch):
        """graph.json 解析失败 → 图路跳过（容错，不崩）。"""
        db = tmp_path / "ca_topics.db"
        _mk_db(db, [(1, "缓存方案重构", [1.0, 0.0])])
        gp = tmp_path / "graph.json"
        gp.write_text("{corrupt json", encoding="utf-8")
        monkeypatch.setattr(inject_mod, "_default_graph_path", lambda: gp)

        seen_budgets = []

        def fake_pick(query, budget, limit):
            seen_budgets.append([c["reality_id"] for c in budget])
            return [next(c for c in budget if c["reality_id"] == 1)]

        monkeypatch.setattr(inject_mod, "_pick_by_4b", fake_pick)
        inject_mod.pick_injection_realities(
            "缓存方案怎么重构？", [1.0, 0.0], "tester", limit=3, db_path=db)
        assert seen_budgets[0] == [1]
