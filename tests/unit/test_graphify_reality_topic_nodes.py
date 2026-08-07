"""第三轮 T1：sync_realities_to_graph 补建 topic source 节点（悬挂边根治）。

背景：增量 sync 只加 merged_into 边引用 topic_{sid}_S{strand_id} 而不建节点
→ 实测 592/592 悬挂边。T1 修复：构造边时同步补建 topic 节点（幂等），
节点格式对齐 build_wiki_subgraph（file_type=knowledge、_origin=wiki）。
"""

import json
import re

from ca.store import create_reality, update_reality
from ca.graphify_sync import sync_realities_to_graph

_STRAND_TOPIC_RE = re.compile(r"^topic_[^_]+_S\d+$")


def _mk_reality(db, session="s1", strand_id=10):
    return create_reality(
        profile="tester", name="连接池优化", hdl="完成参数优化并验证",
        current_status={"current_state": ["连接池上限调至 200"],
                        "key_facts": ["连接池耗尽导致超时"],
                        "goals": ["压测报告待输出"]},
        timeline_entry={"seq": 1, "topic_id": 1, "turns": [7, 8],
                        "session_id": session, "overview": "完成参数优化"},
        source_strand={"session_id": session, "strand_id": strand_id},
        centroid_json="[0.1, 0.2]",
        db_path=db,
    )


class TestSyncRealityTopicNodes:
    def test_sync_builds_topic_nodes(self, tmp_path):
        """sync 后 strand-topic 节点数 = source_strands 引用的 strand 数。"""
        db = tmp_path / "ca_topics.db"
        rid = _mk_reality(db, session="s1", strand_id=10)
        update_reality(reality_id=rid,
                       source_strand={"session_id": "s2", "strand_id": 20},
                       db_path=db)

        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}),
                              encoding="utf-8")

        added_nodes, added_links = sync_realities_to_graph(
            [rid], graph_path, db_path=db)
        assert added_nodes == 3  # 1 reality + 2 strand-topic
        assert added_links == 2

        g = json.loads(graph_path.read_text(encoding="utf-8"))
        strand_topics = [
            n["id"] for n in g["nodes"]
            if n.get("file_type") == "knowledge"
            and _STRAND_TOPIC_RE.match(n["id"])
        ]
        assert sorted(strand_topics) == ["topic_s1_S10", "topic_s2_S20"]
        # 节点格式对齐 build_wiki_subgraph
        node = next(n for n in g["nodes"] if n["id"] == "topic_s1_S10")
        assert node["label"] == "Strand s1/S10"
        assert node["source_file"] == "strand_summaries/s1/S10"
        assert node["_origin"] == "wiki"
        assert node["community"] == 0
        # 悬挂边清零
        ids = {n["id"] for n in g["nodes"]}
        assert all(l["source"] in ids and l["target"] in ids
                   for l in g["links"])

    def test_sync_idempotent_with_existing_topic_nodes(self, tmp_path):
        """幂等：二次 sync 不重复建节点/边。"""
        db = tmp_path / "ca_topics.db"
        rid = _mk_reality(db)
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}),
                              encoding="utf-8")

        sync_realities_to_graph([rid], graph_path, db_path=db)
        added_nodes, added_links = sync_realities_to_graph(
            [rid], graph_path, db_path=db)
        assert (added_nodes, added_links) == (0, 0)
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        assert len(g["nodes"]) == 2  # reality + topic
        assert len(g["links"]) == 1

    def test_sync_does_not_touch_ast_nodes(self, tmp_path):
        """代码 AST 节点（file_type=code）不受影响（T2 红线前置）。"""
        db = tmp_path / "ca_topics.db"
        rid = _mk_reality(db)
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({
            "nodes": [
                {"id": "topic_manager", "label": "topic_manager.py",
                 "file_type": "code", "_origin": "ast"},
                {"id": "topic_manager_compute_centroid",
                 "label": "_compute_centroid()", "file_type": "code",
                 "_origin": "ast"},
            ],
            "links": [],
        }), encoding="utf-8")

        sync_realities_to_graph([rid], graph_path, db_path=db)
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        assert "topic_manager" in ids
        assert "topic_manager_compute_centroid" in ids
        assert "topic_s1_S10" in ids
