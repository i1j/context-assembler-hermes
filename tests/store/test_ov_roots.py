"""ov_roots 表 + 种子迁移（决策 44 前置，2026-08-08）。

覆盖：
  - schema 字段（root_uri/filters/enabled/origin/added_at/last_seen）
  - 种子 3 行（context-assembler/windows enabled=1，irobot enabled=0）
  - INSERT OR IGNORE 幂等：二次迁移不重复、不覆盖用户修改
  - filters JSON 容错（读取层降级为空 dict，不崩）
"""

import json
import sqlite3

import scripts.wiki_to_graph as w2g  # noqa: F401  (读取层容错验证)

from ca.store import _get_topic_conn, _migrate_ov_roots_seed


class TestOvRootsSeed:
    def test_schema_columns(self, tmp_path):
        """ov_roots 表字段齐全。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ov_roots)")}
        assert {"root_uri", "filters", "enabled", "origin",
                "added_at", "last_seen"} <= cols

    def test_ov_roots_seed_sqlite_idempotent(self, tmp_path):
        """种子 3 行 + INSERT OR IGNORE 二次执行不重复、不覆盖用户修改。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        rows = conn.execute(
            "SELECT root_uri, filters, enabled, origin FROM ov_roots "
            "ORDER BY root_uri").fetchall()
        assert len(rows) == 3
        by_uri = {r[0]: r for r in rows}
        assert by_uri["viking://resources/projects/context-assembler"][2] == 1
        assert by_uri["viking://resources/projects/windows"][2] == 1
        assert by_uri["viking://resources/projects/irobot"][2] == 0
        # windows 种子带 exclude /code/ 过滤
        assert json.loads(by_uri["viking://resources/projects/windows"][1]) \
            == {"exclude": ["/code/"]}

        # 二次迁移：不重复
        _migrate_ov_roots_seed(conn)
        assert conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 3
        # 幂等：不覆盖用户修改（enabled 保持 0）
        conn.execute(
            "UPDATE ov_roots SET enabled=0 "
            "WHERE root_uri='viking://resources/projects/windows'")
        conn.commit()
        _migrate_ov_roots_seed(conn)
        assert conn.execute(
            "SELECT enabled FROM ov_roots "
            "WHERE root_uri='viking://resources/projects/windows'"
        ).fetchone()[0] == 0

    def test_filters_json_tolerant(self, tmp_path, monkeypatch):
        """filters JSON 容错：非法 JSON 读取层降级为空 dict（不崩、不丢根）。"""
        db = tmp_path / "ca_topics.db"
        _get_topic_conn(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT OR REPLACE INTO ov_roots "
            "(root_uri, filters, enabled, origin, added_at) "
            "VALUES (?,?,1,'manual',?)",
            ("viking://resources/projects/bad", "{not-json", 100.0))
        conn.commit()
        conn.close()

        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        roots = w2g._load_ov_roots()
        bad = next(r for r in roots
                   if r["root_uri"] == "viking://resources/projects/bad")
        assert bad["filters"] == {}
        # 合法根不受影响
        assert any(r["root_uri"] == "viking://resources/projects/context-assembler"
                   for r in roots)
