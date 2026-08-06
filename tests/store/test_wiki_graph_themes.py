"""v6.5.4 修复回归测试 — wiki 子图全量链路适配 themes。

覆盖:
  - wiki_to_graph.build_wiki_subgraph: 读 themes 表（原 topic_wiki 已废弃），
    节点 id 对齐 graphify_sync 增量命名 theme_{theme_id}
  - store.build_wiki_associations: 读 themes 表 + 写入 wiki_associations.theme_id
  - store._migrate_wiki_associations_column: 旧库 entry_id 列 → theme_id 迁移
  - store.query_wiki_associations: 按 theme_id 查询

背景: v6.5 schema 迁移（topic_wiki → themes）只同步了增量链路
(graphify_sync.py)，全量重建链路（wiki_to_graph.py）漏迁移 → 表不存在崩溃，
主图 427 知识节点无法重建。本文件为修复后的回归测试。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from ca.store import (
    _get_topic_conn,
    build_wiki_associations,
    create_theme,
    insert_theme_strand_map,
    query_wiki_associations,
    write_strand_summary,
)


@pytest.fixture
def theme_db(tmp_path):
    """临时 topic DB（v6.5 schema）。"""
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


def _mk_theme(db, title="连接池优化", sid="sess1", strand=1, profile="tester"):
    return create_theme(
        profile=profile, title=title, overview="完成参数优化并验证",
        ooda={"决策与方案": ["连接池上限调至 200"]},
        key_facts=["连接池耗尽导致超时"], open_items=["压测报告待输出"],
        timeline_entry={"seq": 1, "topic_id": 1, "turns": [7, 8],
                        "session_id": sid, "overview": "完成参数优化"},
        source_strand={"session_id": sid, "strand_id": strand},
        centroid_json="[0.1, 0.2]",
        db_path=db,
    )


class TestWikiToGraphBuildSubgraph:
    """wiki_to_graph.build_wiki_subgraph 读 themes 表。"""

    def test_build_subgraph_reads_themes(self, theme_db, monkeypatch):
        _mk_theme(theme_db, title="连接池与超时配置优化")
        _mk_theme(theme_db, title="Wiki 结构梳理", sid="sess2", strand=9)

        # 指向临时 DB（wiki_to_graph 默认读 profile ca_topics.db）
        monkeypatch.setenv("CA_CACHE_DIR", str(theme_db.parent))

        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts import wiki_to_graph as w2g

        # 临时替换 DB 路径常量
        orig = w2g.CA_TOPICS_DB
        w2g.CA_TOPICS_DB = str(theme_db)
        try:
            sub = w2g.build_wiki_subgraph()
        finally:
            w2g.CA_TOPICS_DB = orig

        nodes = sub["nodes"]
        knowledge = [n for n in nodes if n.get("file_type") == "knowledge"]
        # 2 theme 节点 + 2 strand 节点
        assert len([n for n in nodes if n["id"].startswith("theme_")]) == 2
        # 节点 id 对齐增量命名 theme_{id}
        ids = {n["id"] for n in knowledge}
        assert any(i.startswith("theme_") for i in ids)
        # merged_into 边：strand → theme
        edges = sub["edges"]
        merged = [e for e in edges if e.get("relation") == "merged_into"]
        assert len(merged) >= 2
        for e in merged:
            assert e["source"].startswith("topic_")
            assert e["target"].startswith("theme_")
            assert e["source_file"] == "themes"

    def test_build_subgraph_empty_db(self, theme_db, monkeypatch):
        """空库不崩溃，返回空子图。"""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts import wiki_to_graph as w2g

        orig = w2g.CA_TOPICS_DB
        w2g.CA_TOPICS_DB = str(theme_db)
        try:
            sub = w2g.build_wiki_subgraph()
        finally:
            w2g.CA_TOPICS_DB = orig

        assert sub["nodes"] == []
        assert sub["edges"] == []


class TestWikiAssociationsThemes:
    """build_wiki_associations 读 themes + 写入 theme_id 列。"""

    def _write_graph(self, theme_db, tmp_path):
        """构造最小 graph.json：英文 code 节点 + 中文 document 节点 + knowledge 节点。

        真实主图匹配主要靠 document 节点（中文标题，如 docs/wiki/*.md），
        code 节点 label 为英文函数名，与中文 theme 标题天然难匹配。
        """
        graph = {
            "nodes": [
                {"id": "fn_connect_pool", "label": "connect_pool_setup()",
                 "file_type": "code", "source_file": "ca/store.py",
                 "community": 1},
                {"id": "doc_conn_pool", "label": "连接池配置优化方案",
                 "file_type": "document",
                 "source_file": "docs/wiki/decisions/conn-pool.md",
                 "community": 2},
                {"id": "theme_1", "label": "[知识] 连接池优化",
                 "file_type": "knowledge", "_origin": "wiki"},
            ],
            "links": [],
        }
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        return gp

    def test_build_associations_reads_themes(self, theme_db, tmp_path):
        tid = _mk_theme(theme_db, title="连接池优化")
        assert tid is not None and tid > 0
        gp = self._write_graph(theme_db, tmp_path)

        n = build_wiki_associations(str(gp), db_path=theme_db)
        assert n > 0

        conn = _get_topic_conn(theme_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_associations)")}
        assert "theme_id" in cols
        assert "entry_id" not in cols
        rows = conn.execute(
            "SELECT theme_id, graph_node_id FROM wiki_associations").fetchall()
        assert all(r[0] == tid for r in rows)
        # 匹配到中文 document 节点（真实场景主匹配路径）
        assert any(r[1] == "doc_conn_pool" for r in rows)

    def test_query_associations_by_theme_id(self, theme_db, tmp_path):
        tid = _mk_theme(theme_db, title="连接池优化")
        assert tid is not None
        gp = self._write_graph(theme_db, tmp_path)
        build_wiki_associations(str(gp), db_path=theme_db)

        res = query_wiki_associations([tid], limit=5, db_path=theme_db)
        assert len(res) >= 1
        assert any(r["node_id"] == "doc_conn_pool" for r in res)

    def test_migrate_old_entry_id_column(self, tmp_path):
        """旧库（v6.5.3 前）wiki_associations 为 entry_id 列 → 自动迁移。"""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE wiki_associations (
                entry_id      INTEGER NOT NULL,
                graph_node_id TEXT    NOT NULL,
                node_label    TEXT    NOT NULL DEFAULT '',
                node_type     TEXT    NOT NULL DEFAULT 'code',
                relation      TEXT    NOT NULL DEFAULT 'related',
                strength      REAL    NOT NULL DEFAULT 0.5,
                source_file   TEXT    NOT NULL DEFAULT '',
                ov_uri        TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (entry_id, graph_node_id)
            )"""
        )
        conn.execute(
            "INSERT INTO wiki_associations VALUES (1,'n1','l','code','related',0.5,'','')")
        conn.commit()
        conn.close()

        # 触发 _get_topic_conn → executescript 建其余表 + 迁移
        conn2 = _get_topic_conn(db)
        cols = {r[1] for r in conn2.execute("PRAGMA table_info(wiki_associations)")}
        assert "theme_id" in cols
        assert "entry_id" not in cols
        # 数据保留
        row = conn2.execute(
            "SELECT theme_id, graph_node_id FROM wiki_associations").fetchone()
        assert row == (1, "n1")
        conn2.close()


class TestNoTopicWikiReference:
    """生产代码不应再引用已废弃的 topic_wiki / wiki_strand_map 表。"""

    def test_store_no_topic_wiki_sql(self):
        src = Path(__file__).resolve().parent.parent.parent / "ca" / "store.py"
        text = src.read_text(encoding="utf-8")
        assert "FROM topic_wiki" not in text
        assert "INTO topic_wiki" not in text
        assert "UPDATE topic_wiki" not in text
        assert "FROM wiki_strand_map" not in text
        assert "INTO wiki_strand_map" not in text

    def test_wiki_to_graph_no_topic_wiki_sql(self):
        src = (Path(__file__).resolve().parent.parent.parent
               / "scripts" / "wiki_to_graph.py")
        text = src.read_text(encoding="utf-8")
        assert "FROM topic_wiki" not in text


class TestWikiMetaConstants:
    """v6.5.4 回归：死代码清理不得误删活跃函数依赖的模块级常量。

    教训：cleanup 脚本按 def 块删除，模块级常量（WIKI_META_SCHEMA /
    WIKI_MERGE_THRESHOLD_DEFAULT）恰好落在死函数与活跃函数之间被误删，
    导致 get_wiki_threshold 运行时 NameError。
    """

    def test_wiki_meta_constants_exist(self, theme_db):
        from ca.store import (WIKI_META_SCHEMA, WIKI_MERGE_THRESHOLD_DEFAULT,
                              get_wiki_threshold, set_wiki_threshold)

        assert "CREATE TABLE IF NOT EXISTS wiki_meta" in WIKI_META_SCHEMA
        assert WIKI_MERGE_THRESHOLD_DEFAULT == 0.70

        # 运行时链路：get/set 不崩
        assert get_wiki_threshold(db_path=theme_db) == 0.70
        assert set_wiki_threshold(0.75, db_path=theme_db)
        assert get_wiki_threshold(db_path=theme_db) == 0.75
        set_wiki_threshold(0.70, db_path=theme_db)


class TestTraceMatchStrategy:
    """v6.5.4 trace 边匹配策略：中文 bigram + log-IDF 加权。

    背景：原整串 `\w+` 匹配对中文无分词能力（'话题切换' vs '话题摘要' 零重叠），
    trace 边恒 0。改为字符级 bigram + IDF 加权，专有词（提交/摘要）权重高于
    泛词（优化/机制/话题）。
    """

    def test_extract_bigrams_chinese(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts import wiki_to_graph as w2g

        bgs = w2g._extract_bigrams("话题摘要化设计")
        # 连续 2 字窗口
        assert "摘要" in bgs
        assert "话题" in bgs
        assert "设计" in bgs
        # 英文 token 小写化
        bgs2 = w2g._extract_bigrams("OpenViking 提交")
        assert "openviking" in bgs2
        assert "提交" in bgs2

    def test_bigram_idf_weights_special_words(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts import wiki_to_graph as w2g

        # 模拟真实分布：'优化' 高频（泛词 df 大），'提交' 低频（专有词 df 小）
        titles = [
            "连接池优化", "话题切换优化", "路由优化", "缓存优化", "摘要优化",
            "资源提交冲突", "话题提交功能",
        ]
        idf = w2g._bigram_idf(titles)
        # 专有词（df=2/7）权重大于泛词（df=5/7）
        assert idf.get("提交", 0) > idf.get("优化", 0)
        assert idf.get("连接", 0) > idf.get("优化", 0)

    def test_build_subgraph_generates_trace_edges(self, theme_db, monkeypatch):
        """真实链路：含'提交/摘要'主题应生成 ov_doc trace 边。

        不依赖真实 OV 和真实 IDF 分布：mock _fetch_ov_raw 与 _bigram_idf，
        专注验证匹配规则（专有词命中 → trace 边，无重叠 → 无边）。
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts import wiki_to_graph as w2g

        # mock OV 文档内容（两个文档，标题与构造主题匹配）
        fake_docs = {
            w2g.TRACE_SOURCES[0]:
                "# 话题摘要化设计 v3\n\n## 背景\n摘要机制重构",
            w2g.TRACE_SOURCES[1]:
                "# CA OpenViking 话题自动提交\n\n## 目标\n提交方案",
        }
        monkeypatch.setattr(w2g, "_fetch_ov_raw",
                            lambda uri: fake_docs.get(uri, ""))
        # mock IDF：模拟真实库分布（话题=泛词中偏高，摘要/提交=专有词）
        monkeypatch.setattr(w2g, "_bigram_idf", lambda titles: {
            "话题": 2.55, "摘要": 3.62, "提交": 4.41, "设计": 2.80,
            "优化": 1.36, "机制": 2.17, "连接": 4.22,
        })

        # 构造主题：与两个 OV 文档标题可匹配
        _mk_theme(theme_db, title="话题提交功能实现与异步流程设计", sid="s1", strand=1)
        _mk_theme(theme_db, title="话题级摘要能力升级", sid="s2", strand=2)
        _mk_theme(theme_db, title="连接池优化", sid="s3", strand=3)  # 不应匹配

        orig = w2g.CA_TOPICS_DB
        w2g.CA_TOPICS_DB = str(theme_db)
        try:
            sub = w2g.build_wiki_subgraph()
        finally:
            w2g.CA_TOPICS_DB = orig

        edges = sub["edges"]
        trace = [e for e in edges if e.get("relation") == "trace"]
        assert len(trace) >= 2, f"期望 ≥2 条 trace 边，实际 {len(trace)}"

        # 提交主题 → 提交文档
        assert any(
            e["source"] == "theme_1" and "提交" in e["target"]
            for e in trace
        )
        # 摘要主题 → 摘要文档
        assert any(
            e["source"] == "theme_2" and "摘要" in e["target"]
            for e in trace
        )
        # 连接池优化不产生 trace（'连接'+'优化' 无 doc 重叠词）
        assert not any(e["source"] == "theme_3" for e in trace)
