"""graphify 增量同步（v6.5.3 theme 适配版）测试。"""
import json
import pytest


@pytest.fixture
def theme_db(tmp_path):
    from ca.store import _get_topic_conn
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


def _mk_theme(db_path, tid_hint=1, title="测试主题", session="s1", strand_id=10):
    from ca.store import create_theme
    return create_theme(
        profile="tester", title=title, overview="ov", ooda={},
        key_facts=[], open_items=[],
        timeline_entry={"topic_id": 1, "turns": [1],
                        "session_id": session, "overview": "ov"},
        source_strand={"session_id": session, "strand_id": strand_id},
        centroid_json=None, db_path=db_path,
    )


class TestGraphifySyncThemes:
    """ca.graphify_sync.sync_themes_to_graph：themes 增量同步 graph.json。"""

    def test_sync_adds_theme_node_and_link(self, tmp_path, theme_db):
        from ca.graphify_sync import sync_themes_to_graph

        tid = _mk_theme(theme_db)
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")

        added_nodes, added_links = sync_themes_to_graph([tid], graph_path, db_path=theme_db)
        assert added_nodes == 1
        assert added_links == 1

        g = json.loads(graph_path.read_text(encoding="utf-8"))
        node = next(n for n in g["nodes"] if n["id"] == f"theme_{tid}")
        assert node["file_type"] == "knowledge"
        assert "测试主题" in node["label"]
        link = g["links"][0]
        assert link["source"] == "topic_s1_S10"
        assert link["target"] == f"theme_{tid}"
        assert link["relation"] == "merged_into"

    def test_sync_idempotent(self, tmp_path, theme_db):
        from ca.graphify_sync import sync_themes_to_graph

        tid = _mk_theme(theme_db)
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        sync_themes_to_graph([tid], graph_path, db_path=theme_db)

        added_nodes, added_links = sync_themes_to_graph([tid], graph_path, db_path=theme_db)
        assert (added_nodes, added_links) == (0, 0)
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        assert len(g["nodes"]) == 1
        assert len(g["links"]) == 1

    def test_sync_multisession_theme(self, tmp_path, theme_db):
        """跨 session theme：每个 strand 一条 merged_into 边。"""
        from ca.graphify_sync import sync_themes_to_graph
        from ca.store import update_theme

        tid = _mk_theme(theme_db, session="s1", strand_id=10)
        update_theme(theme_id=tid, overview="v2",
                     timeline_entry={"topic_id": 2, "turns": [5],
                                     "session_id": "s2", "overview": "v2"},
                     source_strand={"session_id": "s2", "strand_id": 20},
                     db_path=theme_db)
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")

        added_nodes, added_links = sync_themes_to_graph([tid], graph_path, db_path=theme_db)
        assert added_links == 2
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        sources = {l["source"] for l in g["links"]}
        assert "topic_s1_S10" in sources
        assert "topic_s2_S20" in sources

    def test_missing_theme_id_skipped(self, tmp_path, theme_db):
        """不存在的 theme_id → 不产生节点/边（无异常）。"""
        from ca.graphify_sync import sync_themes_to_graph

        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        added_nodes, added_links = sync_themes_to_graph([9999], graph_path, db_path=theme_db)
        assert (added_nodes, added_links) == (0, 0)
