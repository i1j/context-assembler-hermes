"""Store 层 strand schema — strand_summaries / wiki_strand_map / topic_wiki.source_strands。

设计决策对照:
  → v6.4 strand 重构: 摘要单元从话题块(topic) → 事务级 strand
  → 不向前兼容: topic_summaries/wiki_topic_map 旧表不再创建，旧数据丢弃

覆盖:
  - schema: strand_summaries / wiki_strand_map 表存在; topic_wiki 有 source_strands 列
  - write_strand_summary / query_strands_by_session: 基本读写
  - update_strand_centroid: 写入 centroid
  - find_unmerged_strands: 只返回 completed 且未 merge 的 strand
  - merge_strand_into_entry / create_wiki_entry_from_strand: source_strands 引用
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def strand_db(tmp_path):
    """临时 topic DB，自动创建新 schema。"""
    from ca.store import _get_topic_conn

    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


class TestStrandSchema:
    """v6.5 schema 存在性（themes/theme_strand_map 替代旧 wiki 表）。"""

    def test_strand_schema_tables_exist(self, strand_db):
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(strand_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "strand_summaries" in tables
        assert "themes" in tables
        assert "theme_strand_map" in tables

    def test_old_wiki_tables_not_created(self, strand_db):
        """v6.5 不向前兼容：topic_wiki / wiki_strand_map 不再创建。"""
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(strand_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "topic_wiki" not in tables
        assert "wiki_strand_map" not in tables

    def test_old_tables_not_created(self, strand_db):
        """旧表 topic_summaries / wiki_topic_map 不再创建。"""
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(strand_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "topic_summaries" not in tables
        assert "wiki_topic_map" not in tables


class TestStrandReadWrite:
    """write_strand_summary / query_strands_by_session / update_strand_centroid。"""

    def test_write_and_query_strand(self, strand_db):
        from ca.store import write_strand_summary, query_strands_by_session

        sid = write_strand_summary(
            "sess1", 3, "tester", hdl="连接池优化",
            turns=[12], ooda_json='{"决策与方案":["x"]}',
            changes_json='["x"]', key_facts_json='[]',
            db_path=strand_db,
        )
        assert sid > 0
        rows = query_strands_by_session("sess1", db_path=strand_db)
        assert len(rows) == 1
        assert rows[0]["hdl"] == "连接池优化"
        assert rows[0]["turns"] == [12]
        assert rows[0]["topic_id"] == 3
        assert rows[0]["profile"] == "tester"

    def test_write_multiple_strands_same_topic(self, strand_db):
        """同一话题块可写多个 strand（宁多勿少）。"""
        from ca.store import write_strand_summary, query_strands_by_session

        write_strand_summary("sess1", 1, "tester", hdl="sA", turns=[1, 2],
                             ooda_json="{}", changes_json="[]",
                             key_facts_json="[]", db_path=strand_db)
        write_strand_summary("sess1", 1, "tester", hdl="sB", turns=[2],
                             ooda_json="{}", changes_json="[]",
                             key_facts_json="[]", db_path=strand_db)
        rows = query_strands_by_session("sess1", db_path=strand_db)
        assert {r["hdl"] for r in rows} == {"sA", "sB"}

    def test_update_strand_centroid(self, strand_db):
        from ca.store import (write_strand_summary, update_strand_centroid,
                              _get_topic_conn)

        sid = write_strand_summary("s1", 1, "tester", hdl="h",
                                   turns=[1], ooda_json="{}",
                                   changes_json="[]", key_facts_json="[]",
                                   db_path=strand_db)
        ok = update_strand_centroid(sid, "[0.1, 0.2]", db_path=strand_db)
        assert ok
        conn = _get_topic_conn(strand_db)
        row = conn.execute(
            "SELECT centroid_json FROM strand_summaries WHERE strand_id=?",
            (sid,)).fetchone()
        assert row[0] == "[0.1, 0.2]"


class TestStrandThemeMerge:
    """find_unmerged_strands（v6.5：LEFT JOIN theme_strand_map）。"""

    def test_find_unmerged_strands(self, strand_db):
        from ca.store import (write_strand_summary, find_unmerged_strands,
                              insert_theme_strand_map)

        sid = write_strand_summary("s1", 1, "tester", hdl="h",
                                   turns=[1], ooda_json="{}",
                                   changes_json='["c1"]',
                                   key_facts_json="[]",
                                   status="completed", db_path=strand_db)
        unmerged = find_unmerged_strands("tester", db_path=strand_db)
        assert len(unmerged) == 1
        assert unmerged[0]["strand_id"] == sid

        # merge 后不再返回
        tid = 1
        conn_ok = insert_theme_strand_map("s1", sid, tid, db_path=strand_db)
        assert conn_ok
        assert find_unmerged_strands("tester", db_path=strand_db) == []

    def test_skip_status_not_unmerged(self, strand_db):
        """status=skip 的 strand 不进 unmerged。"""
        from ca.store import write_strand_summary, find_unmerged_strands

        write_strand_summary("s1", 1, "tester", hdl="h", turns=[1],
                             ooda_json="{}", changes_json="[]",
                             key_facts_json="[]", status="skip",
                             db_path=strand_db)
        assert find_unmerged_strands("tester", db_path=strand_db) == []
