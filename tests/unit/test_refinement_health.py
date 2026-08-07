"""refinement.py 健康评分回归测试 — 决策 34 v2.7（BUG 修复：2026-08-07）。

背景（BUG-1）：_run_health_score 日志统计块用 row[8] 访问 SELECT 的 8 列结果
（索引 0-7）→ tuple index out of range → 精炼轮每次 0.1s aborted
（refinement_meta 历史 3 条 aborted 同源）。修复：主循环累计 flagged_count，
统计日志复用计数，不再二次调用 _compute_health_score。

背景（BUG-2）：_clean_dead_sources 等 3 处 fallback 用 Path.home()/".hermes"/"ca_cache"
（忽略 HERMES_HOME env）→ 独立脚本/测试环境（无 hermes_constants 可导入）下
误清全部 source_strands（2026-08-07 精炼轮误清 133/133 事故，已从 s2r 重建恢复）。
修复：fallback 优先用 HERMES_HOME env，与 store._get_topic_store_path 一致。

本文件覆盖：
  H-1 健康评分正常完成（无 IndexError），全部 reality 写入 health_score
  H-2 单 strand / 多 source reality 评分差异（过碎信号降分）
  H-3 flagged_for_review 标记正确（score < 0.3）
  Z-1 独立脚本环境（无 hermes_constants）下 _clean_dead_sources 用 HERMES_HOME
      正确解析 ca_cache，不误清有效 source
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

from ca.refinement import IdleRefinementDaemon  # noqa: E402


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """构造最小 realities + strand_to_reality 表（对齐 _run_health_score 查询）。"""
    conn = sqlite3.connect(str(tmp_path / "test_topics.db"))
    conn.execute(
        "CREATE TABLE realities ("
        " reality_id INTEGER PRIMARY KEY, name TEXT, hdl TEXT, current_status TEXT,"
        " timeline TEXT, source_strands TEXT, profile TEXT, centroid_json TEXT,"
        " query_centroid_json TEXT, query_count INTEGER, health_score REAL,"
        " flagged_for_review INTEGER, topic_count INTEGER,"
        " reviewed_at REAL, last_reviewed_turn INTEGER, updated_at REAL, created_at REAL)"
    )
    conn.execute(
        "CREATE TABLE strand_to_reality (strand_id INTEGER, reality_id INTEGER)"
    )
    # R1: 多 source（2 session）+ 有 facts → 高分
    conn.execute(
        "INSERT INTO realities (reality_id, name, hdl, current_status, timeline,"
        " source_strands, centroid_json, updated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            1, "R1 工作线", "H1",
            json.dumps({"current_state": ["正常"], "key_facts": ["事实A", "事实B"],
                        "goals": ["目标"], "context": []}),
            json.dumps([{"hdl": "H1", "ts": 100.0}]),
            json.dumps({"sess-A": [1, 2], "sess-B": [3]}),
            json.dumps([0.1, 0.2]),
            100.0, 50.0,
        ),
    )
    # R2: 单 strand + 无 facts + 空 centroid → 低分（flagged）
    conn.execute(
        "INSERT INTO realities (reality_id, name, hdl, current_status, timeline,"
        " source_strands, centroid_json, updated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            2, "R2 碎片", "H2",
            json.dumps({"current_state": [], "key_facts": [], "goals": [], "context": []}),
            json.dumps([]),
            json.dumps({"sess-C": [7]}),
            "null",
            100.0, 50.0,
        ),
    )
    conn.commit()
    return conn


class TestHealthScore:
    def test_h1_completes_without_index_error(self, tmp_path):
        """H-1 回归：统计块不再越界，全部 reality 获得 health_score。"""
        conn = _make_db(tmp_path)
        daemon = IdleRefinementDaemon()
        scored = daemon._run_health_score(conn)
        assert scored == 2  # 两个 reality 都评分
        rows = conn.execute(
            "SELECT reality_id, health_score, flagged_for_review FROM realities"
        ).fetchall()
        scores = {rid: (score, flagged) for rid, score, flagged in rows}
        assert len(scores) == 2
        # 两个都有分（非 NULL）
        assert all(score is not None for score, _ in scores.values())
        conn.close()

    def test_h2_fragmented_reality_scores_lower(self, tmp_path):
        """H-2 单 strand 碎片 reality 分低于多 source reality（过碎信号）。"""
        conn = _make_db(tmp_path)
        daemon = IdleRefinementDaemon()
        daemon._run_health_score(conn)
        r1 = conn.execute(
            "SELECT health_score FROM realities WHERE reality_id=1").fetchone()[0]
        r2 = conn.execute(
            "SELECT health_score FROM realities WHERE reality_id=2").fetchone()[0]
        assert r1 > r2  # 多 source + facts 高；单 strand + 空 facts 低
        conn.close()

    def test_h3_low_score_flagged(self, tmp_path):
        """H-3 score < 0.3 → flagged_for_review=1（R2 单 strand 空内容应被标记）。"""
        conn = _make_db(tmp_path)
        daemon = IdleRefinementDaemon()
        daemon._run_health_score(conn)
        r2 = conn.execute(
            "SELECT health_score, flagged_for_review FROM realities WHERE reality_id=2"
        ).fetchone()
        assert r2[0] < 0.3
        assert r2[1] == 1
        conn.close()


class TestZombieCleanupPath:
    def test_z1_clean_dead_sources_uses_hermes_home(self, tmp_path, monkeypatch):
        """Z-1 独立脚本环境（hermes_constants 不可导入）下，
        _clean_dead_sources 必须用 HERMES_HOME 定位 ca_cache，
        不能回退到 ~/.hermes/ca_cache 误清有效 source（BUG-2 回归）。"""
        # 模拟独立脚本环境：hermes_constants 不可导入
        monkeypatch.setitem(sys.modules, "hermes_constants", None)

        # 构造 profile 目录：<home>/ca_cache/sess-A.db 存在
        home = tmp_path / "profile"
        cache = home / "ca_cache"
        cache.mkdir(parents=True)
        (cache / "sess-A.db").write_text("")  # 有效 session DB

        monkeypatch.setenv("HERMES_HOME", str(home))
        # 防真实 ~/.hermes 干扰：确保 home 下无默认 ca_cache 干扰
        assert (cache / "sess-A.db").exists()

        daemon = IdleRefinementDaemon()
        cleaned = daemon._clean_dead_sources(
            {"sess-A": [1, 4], "dead-sess": [99]})
        # 有效 session 保留，死 session 清掉
        assert cleaned == {"sess-A": [1, 4]}

    def test_z2_fallback_without_hermes_home_env(self, tmp_path, monkeypatch):
        """Z-2 无 HERMES_HOME env 时 fallback 到 ~/.hermes/ca_cache（旧行为兜底）。"""
        monkeypatch.setitem(sys.modules, "hermes_constants", None)
        monkeypatch.delenv("HERMES_HOME", raising=False)

        daemon = IdleRefinementDaemon()
        # 不崩溃即可（路径可能不存在 → 全清是正确行为，因为确实找不到）
        cleaned = daemon._clean_dead_sources({"sess-A": [1]})
        assert isinstance(cleaned, dict)
