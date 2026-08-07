"""第三轮 T2：merge_into_main_graph 合并前 knowledge 域一致性清理。

覆盖（任务书 T2）：
  - 僵尸 reality 节点（图有 DB 无）删除 + 连带边删除
  - theme_ 残留节点/边全删（themes 冻结）
  - strand-topic 节点以 DB source_strands 引用集为权威，不在集合内删除
  - 悬挂边（source/target 无节点）删除
  - 代码 AST 节点（topic_manager_*，file_type=code）严禁删除（红线）
  - DB 不可读 → 跳过清理不删（容错）
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts import wiki_to_graph as w2g  # noqa: E402
from ca.store import create_reality, update_reality  # noqa: E402


def _mk_reality(db):
    rid = create_reality(
        profile="tester", name="连接池优化", hdl="完成参数优化并验证",
        current_status={"current_state": ["连接池上限调至 200"],
                        "key_facts": ["连接池耗尽导致超时"],
                        "goals": ["压测报告待输出"]},
        timeline_entry={"seq": 1, "topic_id": 1, "turns": [7, 8],
                        "session_id": "s1", "overview": "完成参数优化"},
        source_strand={"session_id": "s1", "strand_id": 10},
        centroid_json="[0.1, 0.2]",
        db_path=db,
    )
    # 含下划线的 session_id（生产格式）也纳入引用集
    update_reality(reality_id=rid,
                   source_strand={"session_id": "20260619_124810_e21229",
                                  "strand_id": 20},
                   db_path=db)
    return rid


def _fixture_graph():
    """构造含僵尸/残留/悬挂/代码节点的主图 fixture。"""
    return {
        "nodes": [
            {"id": "reality_1", "label": "[知识] 连接池优化",
             "file_type": "knowledge", "_origin": "wiki"},
            {"id": "reality_420", "label": "[知识] 僵尸",  # 僵尸（DB 无 420）
             "file_type": "knowledge", "_origin": "reality_merge"},
            {"id": "theme_19", "label": "[知识] 旧主题",  # theme 残留
             "file_type": "knowledge", "_origin": "theme_merge"},
            {"id": "topic_s1_S10", "label": "Strand s1/S10",  # DB 引用集内
             "file_type": "knowledge", "_origin": "wiki"},
            {"id": "topic_20260619_124810_e21229_S20",
             "label": "Strand 20260619_124810_e21229/S20",  # DB 引用集内（下划线 sid）
             "file_type": "knowledge", "_origin": "wiki"},
            {"id": "topic_old_S99", "label": "Strand old/S99",  # 陈旧（不在引用集）
             "file_type": "knowledge", "_origin": "wiki"},
            {"id": "topic_manager", "label": "topic_manager.py",  # 代码 AST 节点
             "file_type": "code", "_origin": "ast"},
            {"id": "topic_manager_compute_centroid",
             "label": "_compute_centroid()", "file_type": "code",
             "_origin": "ast"},
            {"id": "doc_conn_pool", "label": "[知识] 连接池配置优化方案",
             "file_type": "knowledge", "_origin": "trace"},
        ],
        "links": [
            {"source": "topic_s1_S10", "target": "reality_1",
             "relation": "merged_into"},                       # 有效
            {"source": "topic_20260619_124810_e21229_S20",
             "target": "reality_1", "relation": "merged_into"},  # 有效（下划线 sid）
            {"source": "topic_old_S99", "target": "reality_1",
             "relation": "merged_into"},                       # 悬挂（source 待删）
            {"source": "topic_hanging_S5", "target": "reality_1",
             "relation": "merged_into"},                       # 悬挂（source 无节点）
            {"source": "reality_46", "target": "reality_420",
             "relation": "co_occurs_with"},                    # 僵尸连带边
            {"source": "topic_s1_S10", "target": "theme_19",
             "relation": "merged_into"},                       # theme 残留边
            {"source": "topic_manager", "target": "topic_manager_compute_centroid",
             "relation": "calls"},                             # 代码边（保留）
            {"source": "topic_s1_S10", "target": "doc_conn_pool",
             "relation": "trace"},                             # ov_doc 边（保留）
        ],
    }


class TestMergeKnowledgeCleanup:
    def test_cleanup_removes_zombie_theme_dangling(self, tmp_path):
        """僵尸/theme 残留/悬挂/陈旧 strand-topic 清零；代码节点与 ov_doc 保留。"""
        db = tmp_path / "ca_topics.db"
        rid = _mk_reality(db)
        assert rid == 1

        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps(_fixture_graph(), ensure_ascii=False),
                      encoding="utf-8")

        ok = w2g.merge_into_main_graph({"nodes": [], "edges": []},
                                       str(gp), str(db))
        assert ok
        g = json.loads(gp.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        rels = {(l["source"], l["target"], l["relation"]) for l in g["links"]}

        # 删除：僵尸 reality_420 / theme_19 / 陈旧 topic_old_S99
        assert "reality_420" not in ids
        assert "theme_19" not in ids
        assert "topic_old_S99" not in ids
        # 保留：DB 内 reality / strand-topic（含下划线 sid）/ ov_doc
        assert "reality_1" in ids
        assert "topic_s1_S10" in ids
        assert "topic_20260619_124810_e21229_S20" in ids
        assert "doc_conn_pool" in ids
        # 悬挂边（source 无节点）删除
        assert ("topic_hanging_S5", "reality_1", "merged_into") not in rels
        # 僵尸连带 cooc 边删除
        assert ("reality_46", "reality_420", "co_occurs_with") not in rels
        # theme 残留边删除
        assert ("topic_s1_S10", "theme_19", "merged_into") not in rels
        # 有效边保留
        assert ("topic_s1_S10", "reality_1", "merged_into") in rels
        assert ("topic_20260619_124810_e21229_S20", "reality_1",
                "merged_into") in rels
        assert ("topic_s1_S10", "doc_conn_pool", "trace") in rels

    def test_code_ast_nodes_survive_cleanup(self, tmp_path):
        """红线：topic_manager_* 代码 AST 节点 + 其边完好。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps(_fixture_graph(), ensure_ascii=False),
                      encoding="utf-8")

        w2g.merge_into_main_graph({"nodes": [], "edges": []}, str(gp), str(db))
        g = json.loads(gp.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        assert "topic_manager" in ids
        assert "topic_manager_compute_centroid" in ids
        assert ("topic_manager", "topic_manager_compute_centroid",
                "calls") in {(l["source"], l["target"], l["relation"])
                             for l in g["links"]}

    def test_db_unavailable_skips_cleanup(self, tmp_path):
        """DB 不可读 → 跳过清理（不产生破坏性删除），merge 仍可用。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps(_fixture_graph(), ensure_ascii=False),
                      encoding="utf-8")

        ok = w2g.merge_into_main_graph({"nodes": [], "edges": []},
                                       str(gp), str(tmp_path / "missing.db"))
        assert ok
        g = json.loads(gp.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        # 清理跳过 → 节点原样保留
        assert "reality_420" in ids
        assert "theme_19" in ids
        assert "topic_manager" in ids
