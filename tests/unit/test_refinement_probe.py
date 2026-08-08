"""refinement._probe_ov_roots — OV 根探测 + 逐步启用（决策 44 前置，2026-08-08）。

覆盖：
  - 新根自动入表 enabled=0（origin='refine_probe'）
  - 语义命中评估：命中≥1 且 top-1 ≥ CA_OV_REFERENCE_THRESHOLD → 启用
  - 每轮最多启用 1 个（多候选分轮启用）
  - 幂等：已存在根只更新 last_seen，重复调用不重复启用/不重复插入
  - 阈值未配置（env 缺失）→ 只登记不启用（保守）
"""

import json
import urllib.parse
import urllib.request

import ca.fact_linking as fl

from ca.refinement import IdleRefinementDaemon
from ca.store import _get_topic_conn


def _projects_payload():
    """projects 根级目录：3 个种子根 + 3 个待探测候选根。"""
    return {"status": "ok", "result": {"entries": [
        {"uri": "viking://resources/projects/context-assembler",
         "rel_path": "context-assembler", "isDir": True},
        {"uri": "viking://resources/projects/windows",
         "rel_path": "windows", "isDir": True},
        {"uri": "viking://resources/projects/irobot",
         "rel_path": "irobot", "isDir": True},
        {"uri": "viking://resources/projects/osaka",
         "rel_path": "osaka", "isDir": True},
        {"uri": "viking://resources/projects/tokyo",
         "rel_path": "tokyo", "isDir": True},
        {"uri": "viking://resources/projects/kyoto",
         "rel_path": "kyoto", "isDir": True},
    ]}}


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _FakeProjectsUrlopen:
    """仅处理 projects 根级 fs/tree；其它请求（webdav/search）拒绝。"""

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/v1/fs/tree" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if qs.get("uri", [""])[0] == "viking://resources/projects":
                return _FakeResp(json.dumps(_projects_payload()).encode())
        raise ConnectionRefusedError("unexpected request")


def _mk_probe_db(tmp_path):
    """建 tmp DB：ov_roots 种子 + 3 个 reality（各命中一个候选根，kyoto 无命中）。"""
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    for rid, name in ((1, "大阪工作线"), (2, "东京工作线"), (3, "京都工作线")):
        conn.execute(
            "INSERT INTO realities (reality_id, name, hdl, current_status,"
            " profile, created_at, updated_at) VALUES (?,?,?,'{}','tester',0,0)",
            (rid, name, ""))
    conn.commit()
    return conn


def _search_hits(query):
    """reality 查询 → 命中候选根文档；kyoto 相关查询无命中。"""
    table = [
        ("大阪工作线", "viking://resources/projects/osaka/decisions/01.md"),
        ("东京工作线", "viking://resources/projects/tokyo/decisions/02.md"),
    ]
    for name, uri in table:
        if name in query:
            return [{"uri": uri, "score": 0.7}]
    return []


class TestProbeOvRoots:
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CA_OV_REFERENCE_THRESHOLD", "0.55")
        monkeypatch.setattr(urllib.request, "urlopen",
                            _FakeProjectsUrlopen())
        monkeypatch.setattr(fl, "_ov_search_find", _search_hits)
        return _mk_probe_db(tmp_path)

    def test_probe_ov_roots_incremental_enable(self, monkeypatch, tmp_path):
        """每轮仅启用 1 个、enabled 持久化、last_seen 更新、幂等。"""
        conn = self._setup(monkeypatch, tmp_path)
        daemon = IdleRefinementDaemon()

        # Cycle 1：登记 3 个新根 + 启用第 1 个（osaka）
        assert daemon._probe_ov_roots(conn) == 1
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT root_uri, enabled, origin, last_seen FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"][0] == 1
        assert rows["viking://resources/projects/osaka"][1] == "refine_probe"
        assert rows["viking://resources/projects/tokyo"][0] == 0
        assert rows["viking://resources/projects/kyoto"][0] == 0
        # 种子根 enabled 不受影响
        assert rows["viking://resources/projects/context-assembler"][0] == 1
        assert rows["viking://resources/projects/windows"][0] == 1
        assert rows["viking://resources/projects/irobot"][0] == 0
        # last_seen 全部更新
        assert all(r[2] is not None for r in rows.values())
        assert len(rows) == 6

        # Cycle 2：启用第 2 个（tokyo）；osaka 不被重复启用；无重复行
        assert daemon._probe_ov_roots(conn) == 1
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT root_uri, enabled FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"] == 1
        assert rows["viking://resources/projects/tokyo"] == 1
        assert rows["viking://resources/projects/kyoto"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 6

        # Cycle 3：无新候选可启用（kyoto 无命中）
        assert daemon._probe_ov_roots(conn) == 0
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT root_uri, enabled FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/kyoto"] == 0

    def test_no_threshold_registers_but_not_enables(self, monkeypatch,
                                                    tmp_path):
        """env 未配置 CA_OV_REFERENCE_THRESHOLD → 只登记候选根，不启用。"""
        monkeypatch.delenv("CA_OV_REFERENCE_THRESHOLD", raising=False)
        monkeypatch.setattr(urllib.request, "urlopen",
                            _FakeProjectsUrlopen())
        monkeypatch.setattr(fl, "_ov_search_find", _search_hits)
        conn = _mk_probe_db(tmp_path)

        assert IdleRefinementDaemon()._probe_ov_roots(conn) == 0
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT root_uri, enabled, origin FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"] == (0, "refine_probe")
        assert rows["viking://resources/projects/tokyo"] == (0, "refine_probe")
        assert rows["viking://resources/projects/kyoto"] == (0, "refine_probe")
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 6

    def test_ov_down_registers_nothing(self, monkeypatch, tmp_path):
        """OV fs/tree 失败 → 探测跳过，不写表。"""
        monkeypatch.setenv("CA_OV_REFERENCE_THRESHOLD", "0.55")
        def boom(req, timeout=None):
            raise ConnectionRefusedError("ov down")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        conn = _mk_probe_db(tmp_path)
        assert IdleRefinementDaemon()._probe_ov_roots(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 3
