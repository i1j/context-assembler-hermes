"""ov_roots 表 + 种子迁移（决策 44 前置 + 决策 44 续 R7 user_version 门控）。

覆盖：
  - schema 字段（root_uri/filters/enabled/origin/added_at/last_seen）
  - 种子 3 行（context-assembler/windows enabled=1，irobot enabled=0）
  - 全新库 → 种子 3 行 + PRAGMA user_version=1（同一 executescript）
  - 存量库（uv=0 且表有行）→ 仅置位不播种不改写
  - remove 后不再以 enabled=1 复活（uv>=1 早退；w2g 子进程路径不播种）
  - filters JSON 容错（读取层降级为空 dict，不崩）
"""

import json
import sqlite3

import scripts.wiki_to_graph as w2g  # noqa: F401  (读取层容错验证)

from ca.store import (
    _SCHEMA_SQL_STRANDS,
    _get_topic_conn,
    _migrate_ov_roots_seed,
)

CA_ROOT = "viking://resources/projects/context-assembler"


class TestOvRootsSeed:
    def test_schema_columns(self, tmp_path):
        """ov_roots 表字段齐全。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ov_roots)")}
        assert {"root_uri", "filters", "enabled", "origin",
                "added_at", "last_seen"} <= cols

    def test_ov_roots_seed_sqlite_idempotent(self, tmp_path):
        """全新库种子 3 行 + user_version=1；二次迁移不重复、不覆盖用户修改。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        # R7: user_version 置位
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        rows = conn.execute(
            "SELECT root_uri, filters, enabled, origin FROM ov_roots "
            "ORDER BY root_uri").fetchall()
        assert len(rows) == 3
        by_uri = {r[0]: r for r in rows}
        assert by_uri[CA_ROOT][2] == 1
        assert by_uri["viking://resources/projects/windows"][2] == 1
        assert by_uri["viking://resources/projects/irobot"][2] == 0
        # windows 种子带 exclude /code/ 过滤
        assert json.loads(by_uri["viking://resources/projects/windows"][1]) \
            == {"exclude": ["/code/"]}

        # 二次迁移：不重复（uv>=1 早退）
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
        assert any(r["root_uri"] == CA_ROOT for r in roots)


class TestOvRootsSeedGate:
    """R7: 种子迁移 user_version 门控（4 库状态 parametrize 语义）。"""

    def test_fresh_db_seeds_and_sets_user_version(self, tmp_path):
        """① 全新库 → 种子 3 行 + user_version=1。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 3
        assert conn.execute(
            "SELECT enabled FROM ov_roots WHERE root_uri=?",
            (CA_ROOT,)).fetchone()[0] == 1

    def test_existing_db_only_sets_flag_no_seed(self, tmp_path):
        """② 存量库（uv=0 且表有行）→ 仅置位不播种不改写。"""
        db = tmp_path / "ca_topics.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_SCHEMA_SQL_STRANDS)  # 建表（含 ov_roots）
        conn.execute(
            "INSERT INTO ov_roots (root_uri, filters, enabled, origin, added_at) "
            "VALUES (?, '{}', 0, 'manual', ?)",
            ("viking://resources/projects/manual", 100.0))
        conn.commit()
        conn.close()

        _migrate_ov_roots_seed(_get_topic_conn(db))
        conn = _get_topic_conn(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        rows = conn.execute(
            "SELECT root_uri, origin FROM ov_roots").fetchall()
        assert len(rows) == 1  # 不播种
        assert rows[0][0] == "viking://resources/projects/manual"
        assert rows[0][1] == "manual"

    def test_remove_seed_root_not_reseeded(self, tmp_path):
        """③ remove 种子根后表非空 → 不改写（uv>=1 早退，不复活）。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)  # 种子 3 行 + uv=1
        conn.execute("DELETE FROM ov_roots WHERE root_uri=?", (CA_ROOT,))
        conn.commit()
        _migrate_ov_roots_seed(conn)
        assert conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 2
        assert conn.execute(
            "SELECT 1 FROM ov_roots WHERE root_uri=?", (CA_ROOT,)
        ).fetchone() is None

    def test_remove_all_rows_not_reseeded(self, tmp_path):
        """④ remove 全部行 → 不改写（不复活）。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        conn.execute("DELETE FROM ov_roots")
        conn.commit()
        _migrate_ov_roots_seed(conn)
        assert conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    def test_removed_root_not_revived_via_w2g(self, tmp_path, monkeypatch):
        """remove 后经 _get_conn/_ensure_ov_roots 不复活（w2g 路径不播种）。"""
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        conn.execute("DELETE FROM ov_roots WHERE root_uri=?", (CA_ROOT,))
        conn.commit()
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        wconn = w2g._get_conn()
        try:
            assert wconn.execute(
                "SELECT 1 FROM ov_roots WHERE root_uri=?", (CA_ROOT,)
            ).fetchone() is None
        finally:
            wconn.close()

    def test_w2g_fresh_db_no_seed(self, tmp_path, monkeypatch):
        """全新库经 w2g 仅建表不播种（种子权威在 store 迁移）。"""
        db = tmp_path / "ca_topics.db"
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        conn = w2g._get_conn()
        try:
            assert conn.execute("SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 0
        finally:
            conn.close()
