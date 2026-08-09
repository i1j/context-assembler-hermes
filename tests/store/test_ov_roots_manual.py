"""ov_roots 手动接口（决策 44 续 R5）— T5。

覆盖：
  - set_ov_root_filters：合法 list[str] 生效 / 非法（非 dict、include/exclude
    非 list[str]）拒绝不写 / root_uri 不在表 → False
  - list_ov_roots：全表行形状 [{root_uri, filters(dict), enabled, origin,
    added_at, last_seen}] + filters JSON 容错
  - build 应用由既有 test_filters_include_exclude_code（test_ov_import）承接
"""

import json

import ca.store as store

from ca.store import _get_topic_conn, list_ov_roots, set_ov_root_filters

CA_ROOT = "viking://resources/projects/context-assembler"
WIN_ROOT = "viking://resources/projects/windows"


def _manual_conn(monkeypatch, tmp_path):
    """tmp DB + 把 store._get_topic_conn 指向该 conn（set/list 无 db_path 参数）。"""
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    monkeypatch.setattr(store, "_get_topic_conn", lambda *a, **k: conn)
    return conn


class TestSetOvRootFilters:
    """T5 set_ov_root_filters：校验严格、合法生效、非法拒绝。"""

    def test_valid_filters_applied(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        filters = {"include": ["comfyui-model-setup"], "exclude": ["/code/"]}
        assert set_ov_root_filters(WIN_ROOT, filters) is True
        row = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN_ROOT,)
        ).fetchone()
        assert json.loads(row[0]) == filters

    def test_empty_lists_valid(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        assert set_ov_root_filters(WIN_ROOT, {"include": [], "exclude": []}) \
            is True
        row = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN_ROOT,)
        ).fetchone()
        assert json.loads(row[0]) == {"include": [], "exclude": []}

    def test_rejects_non_dict(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        before = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN_ROOT,)
        ).fetchone()[0]
        assert set_ov_root_filters(WIN_ROOT, "not-dict") is False  # type: ignore[arg-type]  # 防御：非法类型拒绝
        assert set_ov_root_filters(WIN_ROOT, None) is False  # type: ignore[arg-type]
        # 不写
        assert conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN_ROOT,)
        ).fetchone()[0] == before

    def test_rejects_bad_values(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        assert set_ov_root_filters(
            WIN_ROOT, {"include": "not-list"}) is False
        assert set_ov_root_filters(
            WIN_ROOT, {"include": [1, 2]}) is False  # 非 str 元素
        assert set_ov_root_filters(
            WIN_ROOT, {"exclude": ["ok"], "include": 5}) is False

    def test_unknown_root_rejected(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        assert set_ov_root_filters(
            "viking://resources/projects/ghost", {"include": []}) is False
        # 未知根不落表
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots WHERE root_uri=?",
            ("viking://resources/projects/ghost",)
        ).fetchone()[0] == 0


class TestListOvRoots:
    """T5 list_ov_roots：形状 + filters JSON 容错。"""

    def test_list_shape(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        rows = list_ov_roots()
        assert len(rows) == 3  # 种子 3 行
        for r in rows:
            assert set(r.keys()) == {"root_uri", "filters", "enabled",
                                     "origin", "added_at", "last_seen"}
            assert isinstance(r["filters"], dict)
            assert r["origin"] == "seed"
        by_uri = {r["root_uri"]: r for r in rows}
        assert by_uri[CA_ROOT]["enabled"] == 1
        assert by_uri[WIN_ROOT]["filters"] == {"exclude": ["/code/"]}

    def test_list_filters_json_tolerant(self, monkeypatch, tmp_path):
        conn = _manual_conn(monkeypatch, tmp_path)
        conn.execute(
            "INSERT OR REPLACE INTO ov_roots "
            "(root_uri, filters, enabled, origin, added_at) "
            "VALUES (?,?,1,'manual',?)",
            ("viking://resources/projects/bad", "{not-json", 100.0))
        conn.commit()
        rows = list_ov_roots()
        bad = next(r for r in rows
                   if r["root_uri"] == "viking://resources/projects/bad")
        assert bad["filters"] == {}  # 容错降级
        assert any(r["root_uri"] == CA_ROOT for r in rows)  # 不丢根
