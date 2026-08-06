"""决策 38 v7：共现边表 + graphify 共现边入图测试。

覆盖:
  - record_block_cooccurrences: 复合键幂等 / 跨 session 同名 topic / profile 隔离
  - query_cooccurrences: 聚合 / deg / n_blocks / 过滤
  - sync_cooccurrences_to_graph: 节点补建 / 边添加 / weight 更新 / 幂等

设计对照:
  → 决策 38 §三 图模型（共现边 w = 同块共现次数）
  → 决策 38 §九.4 聚合键铁律（(session_id, topic_id) 复合键）
"""
import json

import pytest


@pytest.fixture
def cooc_db(tmp_path):
    from ca.store import _get_topic_conn
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


def _graph(tmp_path):
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    return gp


class TestRecordCooccurrences:
    """record_block_cooccurrences — 复合键幂等记录。"""

    def test_records_all_pairs(self, cooc_db):
        from ca.store import record_block_cooccurrences
        n = record_block_cooccurrences("s1", 1, [3, 1, 2], profile="t",
                                       db_path=cooc_db)
        assert n == 3  # (1,2) (1,3) (2,3)

    def test_same_block_idempotent(self, cooc_db):
        from ca.store import record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2, 3], profile="t", db_path=cooc_db)
        n = record_block_cooccurrences("s1", 1, [1, 2, 3], profile="t",
                                       db_path=cooc_db)
        assert n == 0  # UNIQUE 复合键幂等

    def test_same_topic_cross_session_distinct(self, cooc_db):
        from ca.store import record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="t", db_path=cooc_db)
        n = record_block_cooccurrences("s2", 1, [1, 2], profile="t",
                                       db_path=cooc_db)
        assert n == 1  # 跨 session 同名 topic 是不同块（聚合键铁律）

    def test_profile_isolated(self, cooc_db):
        from ca.store import record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="a", db_path=cooc_db)
        n = record_block_cooccurrences("s1", 1, [1, 2], profile="b",
                                       db_path=cooc_db)
        assert n == 1

    def test_less_than_two_noop(self, cooc_db):
        from ca.store import record_block_cooccurrences
        assert record_block_cooccurrences("s1", 1, [5], profile="t",
                                          db_path=cooc_db) == 0
        assert record_block_cooccurrences("s1", 1, [], profile="t",
                                          db_path=cooc_db) == 0


class TestQueryCooccurrences:
    """query_cooccurrences — 聚合边 / deg / n_blocks。"""

    def test_aggregation(self, cooc_db):
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2, 3], profile="t", db_path=cooc_db)
        record_block_cooccurrences("s2", 1, [1, 2], profile="t", db_path=cooc_db)
        edges, deg, n_blocks = query_cooccurrences(profile="t", db_path=cooc_db)
        assert edges == {(1, 2): 2, (1, 3): 1, (2, 3): 1}
        assert deg == {1: 3, 2: 3, 3: 2}
        assert n_blocks == 2

    def test_profile_filter(self, cooc_db):
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="a", db_path=cooc_db)
        record_block_cooccurrences("s1", 1, [1, 2], profile="b", db_path=cooc_db)
        edges, _, _ = query_cooccurrences(profile="a", db_path=cooc_db)
        assert edges == {(1, 2): 1}

    def test_since_filter(self, cooc_db):
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="t", db_path=cooc_db)
        edges, _, _ = query_cooccurrences(profile="t", since=10 ** 15,
                                          db_path=cooc_db)
        assert edges == {}


class TestGraphCooccurrences:
    """sync_cooccurrences_to_graph — 共现边入图（节点补建/weight 更新/幂等）。"""

    def test_adds_nodes_and_links(self, tmp_path, cooc_db):
        from ca.graphify_sync import sync_cooccurrences_to_graph
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="t", db_path=cooc_db)
        edges, _, _ = query_cooccurrences(profile="t", db_path=cooc_db)
        gp = _graph(tmp_path)
        nn, nl = sync_cooccurrences_to_graph(
            edges, gp, db_path=cooc_db, node_titles={1: "甲", 2: "乙"})
        assert (nn, nl) == (2, 1)
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert {n["id"] for n in g["nodes"]} == {"theme_1", "theme_2"}
        link = g["links"][0]
        assert (link["source"], link["target"], link["relation"]) == (
            "theme_1", "theme_2", "co_occurs_with")
        assert link["weight"] == 1.0

    def test_weight_update_not_duplicate(self, tmp_path, cooc_db):
        from ca.graphify_sync import sync_cooccurrences_to_graph
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="t", db_path=cooc_db)
        gp = _graph(tmp_path)
        edges, _, _ = query_cooccurrences(profile="t", db_path=cooc_db)
        sync_cooccurrences_to_graph(edges, gp, db_path=cooc_db,
                                    node_titles={1: "甲", 2: "乙"})
        # 新块使 (1,2) 共现 2 次 → weight 更新，不重复加边
        record_block_cooccurrences("s9", 9, [1, 2], profile="t", db_path=cooc_db)
        edges2, _, _ = query_cooccurrences(profile="t", db_path=cooc_db)
        nn, nl = sync_cooccurrences_to_graph(edges2, gp, db_path=cooc_db,
                                             node_titles={1: "甲", 2: "乙"})
        assert (nn, nl) == (0, 1)  # 仅 weight 更新
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert len(g["links"]) == 1
        assert g["links"][0]["weight"] == 2.0

    def test_idempotent_same_edges(self, tmp_path, cooc_db):
        from ca.graphify_sync import sync_cooccurrences_to_graph
        from ca.store import query_cooccurrences, record_block_cooccurrences
        record_block_cooccurrences("s1", 1, [1, 2], profile="t", db_path=cooc_db)
        gp = _graph(tmp_path)
        edges, _, _ = query_cooccurrences(profile="t", db_path=cooc_db)
        sync_cooccurrences_to_graph(edges, gp, db_path=cooc_db,
                                    node_titles={1: "甲", 2: "乙"})
        nn, nl = sync_cooccurrences_to_graph(edges, gp, db_path=cooc_db,
                                             node_titles={1: "甲", 2: "乙"})
        assert (nn, nl) == (0, 0)
