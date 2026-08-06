"""Store 层 theme schema — themes / theme_strand_map（v6.5 重构版）。

设计决策对照（wiki theme 重构方案 v4/v5）:
  → M8 彻底重构 schema：topic_wiki/wiki_strand_map 废弃，themes/theme_strand_map 新建
  → 一级信息字段：title / overview / ooda_json（当前详细状态）/ key_facts / open_items
  → 二级信息：timeline_json（演变序列，仅 overview 条目，按主题块追加，不设上限）
  → 归并决策：theme_strand_map.method 一行记录（vector | llm | fallback）

覆盖:
  - schema: themes / theme_strand_map 存在；topic_wiki / wiki_strand_map 不再创建
  - themes 列: ooda_json / timeline_json 等
  - create_theme: 基本写入 + 字段可读
  - update_theme: timeline 追加（seq 递增）/ overview 更新 / source_strands 保留
  - insert_theme_strand_map: 映射写入
  - load_all_themes: 多 theme 读取
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def theme_db(tmp_path):
    """临时 topic DB，自动创建 v6.5 schema。"""
    from ca.store import _get_topic_conn

    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


def _timeline_entry(seq: int, topic_id: int, turns: list, overview: str) -> dict:
    return {"seq": seq, "topic_id": topic_id, "turns": turns,
            "session_id": "sess1", "overview": overview}


class TestThemeSchema:
    """v6.5 schema 存在性 + 旧表废弃。"""

    def test_theme_tables_exist(self, theme_db):
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(theme_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "themes" in tables
        assert "theme_strand_map" in tables

    def test_old_wiki_tables_not_created(self, theme_db):
        """不向前兼容：topic_wiki / wiki_strand_map 不再创建。"""
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(theme_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "topic_wiki" not in tables
        assert "wiki_strand_map" not in tables

    def test_themes_has_v65_columns(self, theme_db):
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(theme_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(themes)")}
        for col in ["theme_id", "title", "overview", "ooda_json",
                    "key_facts_json", "open_items_json",
                    "timeline_json", "centroid_json",
                    "source_strands", "profile", "created_at", "updated_at"]:
            assert col in cols, f"themes 缺列: {col}"

    def test_theme_strand_map_columns(self, theme_db):
        from ca.store import _get_topic_conn

        conn = _get_topic_conn(theme_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(theme_strand_map)")}
        for col in ["session_id", "strand_id", "theme_id", "method"]:
            assert col in cols


class TestThemeCRUD:
    """create / load / update / map 读写。"""

    def test_create_theme(self, theme_db):
        from ca.store import create_theme

        tid = create_theme(
            profile="tester",
            title="连接池与超时配置优化",
            overview="完成了参数优化并验证效果",
            ooda={"决策与方案": ["连接池上限调至 200"]},
            key_facts=["连接池耗尽导致超时"],
            open_items=["压测报告待输出"],
            timeline_entry=_timeline_entry(1, 3, [7, 8, 9], "完成参数优化"),
            source_strand={"session_id": "sess1", "strand_id": 5},
            centroid_json='[0.1, 0.2]',
            db_path=theme_db,
        )
        assert tid > 0

    def test_create_theme_with_changes(self, theme_db):
        """create_theme 支持初始 changes（全量去重 changes 初始态）。"""
        from ca.store import create_theme, load_all_themes

        tid = create_theme(
            profile="tester", title="主题丙", overview="ov",
            ooda={"决策与方案": ["决策A"]}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], "ov"),
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None,
            changes=["变更1", "变更2"],
            db_path=theme_db,
        )
        assert tid > 0
        t = load_all_themes(db_path=theme_db)[0]
        assert t["changes"] == ["变更1", "变更2"]

    def test_load_all_themes(self, theme_db):
        from ca.store import create_theme, load_all_themes

        create_theme(profile="tester", title="主题甲", overview="ov甲",
                     ooda={}, key_facts=[], open_items=[],
                     timeline_entry=_timeline_entry(1, 1, [1], "甲"),
                     source_strand={"session_id": "s1", "strand_id": 1},
                     centroid_json=None, db_path=theme_db)
        create_theme(profile="tester", title="主题乙", overview="ov乙",
                     ooda={}, key_facts=[], open_items=[],
                     timeline_entry=_timeline_entry(1, 2, [2], "乙"),
                     source_strand={"session_id": "s1", "strand_id": 2},
                     centroid_json=None, db_path=theme_db)

        themes = load_all_themes(db_path=theme_db)
        assert len(themes) == 2
        titles = {t["title"] for t in themes}
        assert titles == {"主题甲", "主题乙"}
        t = next(t for t in themes if t["title"] == "主题甲")
        assert t["overview"] == "ov甲"
        assert t["ooda"] == {}

    def test_update_theme_appends_timeline(self, theme_db):
        from ca.store import create_theme, load_all_themes, update_theme

        tid = create_theme(
            profile="tester", title="主题甲", overview="初始状态",
            ooda={"决策与方案": ["方案A"]}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], "初始状态"),
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None, db_path=theme_db,
        )
        # 第二次归并：timeline 追加 + overview 更新
        ok = update_theme(
            theme_id=tid,
            overview="演进后状态",
            ooda={"决策与方案": ["方案A", "方案B"]},
            key_facts=["新结论"],
            open_items=[],
            timeline_entry=_timeline_entry(2, 2, [5, 6], "演进后状态"),
            changes=["方案B"],
            db_path=theme_db,
        )
        assert ok

        themes = load_all_themes(db_path=theme_db)
        t = themes[0]
        assert t["overview"] == "演进后状态"
        assert t["ooda"]["决策与方案"] == ["方案A", "方案B"]
        assert t["key_facts"] == ["新结论"]
        # timeline: seq 递增追加
        assert [e["seq"] for e in t["timeline"]] == [1, 2]
        assert t["timeline"][-1]["overview"] == "演进后状态"

    def test_update_theme_keeps_source_strands(self, theme_db):
        from ca.store import create_theme, load_all_themes, update_theme

        tid = create_theme(
            profile="tester", title="主题甲", overview="初始",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], "初始"),
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None, db_path=theme_db,
        )
        update_theme(theme_id=tid, overview="更新", db_path=theme_db)

        themes = load_all_themes(db_path=theme_db)
        assert themes[0]["source_strands"] == {"s1": [1]}

    def test_insert_theme_strand_map(self, theme_db):
        from ca.store import create_theme, insert_theme_strand_map

        tid = create_theme(
            profile="tester", title="主题甲", overview="",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], ""),
            source_strand=None, centroid_json=None, db_path=theme_db,
        )
        ok = insert_theme_strand_map(
            "sess1", 42, tid, method="llm", db_path=theme_db)
        assert ok

        from ca.store import _get_topic_conn
        conn = _get_topic_conn(theme_db)
        row = conn.execute(
            "SELECT theme_id, method FROM theme_strand_map "
            "WHERE session_id=? AND strand_id=?",
            ("sess1", 42),
        ).fetchone()
        assert row == (tid, "llm")

    def test_create_theme_sets_timeline_seq(self, theme_db):
        """create 首条 timeline 自动补 seq=1（与 update_theme 追加衔接）。"""
        from ca.store import create_theme, load_all_themes

        create_theme(
            profile="tester", title="主题甲", overview="ov",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry={"topic_id": 1, "turns": [1],
                            "session_id": "s1", "overview": "首块"},
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None, db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)
        assert themes[0]["timeline"][0]["seq"] == 1

    def test_update_theme_missing_returns_false(self, theme_db):
        from ca.store import update_theme

        assert update_theme(theme_id=9999, overview="x", db_path=theme_db) is False

    def test_query_themes_by_semantics_filters_profile(self, theme_db):
        """v6.5.2: 语义检索必须按 profile 过滤（修复跨 profile 混入注入）。"""
        import json as _json
        from ca.store import create_theme, query_themes_by_semantics

        vec = _json.dumps([0.1] * 8)
        create_theme(
            profile="tester", title="连接池优化", overview="ov",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], "ov"),
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=vec, db_path=theme_db,
        )
        create_theme(
            profile="default", title="其他会话主题", overview="ov2",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [2], "ov2"),
            source_strand={"session_id": "s2", "strand_id": 2},
            centroid_json=vec, db_path=theme_db,
        )
        res = query_themes_by_semantics([0.1] * 8, profile="tester", limit=5, db_path=theme_db)
        titles = [t["title"] for t in res]
        assert "连接池优化" in titles
        assert "其他会话主题" not in titles

    def test_query_themes_by_semantics_excludes_session(self, theme_db):
        """v6.5.2: exclude_session_id 排除当前 session 的 theme（防 FAR 切换自注入）。"""
        import json as _json
        from ca.store import create_theme, query_themes_by_semantics

        vec = _json.dumps([0.2] * 8)
        create_theme(
            profile="tester", title="当前会话主题", overview="ov",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [1], "ov"),
            source_strand={"session_id": "cur_sess", "strand_id": 1},
            centroid_json=vec, db_path=theme_db,
        )
        create_theme(
            profile="tester", title="历史会话主题", overview="ov2",
            ooda={}, key_facts=[], open_items=[],
            timeline_entry=_timeline_entry(1, 1, [2], "ov2"),
            source_strand={"session_id": "old_sess", "strand_id": 2},
            centroid_json=vec, db_path=theme_db,
        )
        res = query_themes_by_semantics(
            [0.2] * 8, profile="tester", limit=5,
            exclude_session_id="cur_sess", db_path=theme_db)
        titles = [t["title"] for t in res]
        assert "历史会话主题" in titles
        assert "当前会话主题" not in titles
