"""refinement._govern_ov_roots — OV 根探测登记 + LLM 治理决策（决策 44 续，2026-08-10）。

覆盖（决策 44 前置探测机制的回归 + 治理轮返回值语义）：
  - 新根自动入表 enabled=0（origin='refine_probe'）
  - LLM 决策 mock keep → 不启用任何根（启用权已移交 LLM，代码不再自动启用）
  - 返回值 = 本轮执行语句数（keep 决策 → 0；不再返回「启用根数」）
  - 幂等：已存在根只更新 last_seen，重复调用不重复插入
  - OV fs/tree 失败 → 无写入（整体失败 keep 全部）
"""

import json
import urllib.parse
import urllib.request

import ca.fact_linking as fl
import ca.refinement as ref

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
    """仅处理 projects 根级 fs/tree；其它请求（webdav/search）拒绝。

    各根级 fs/tree（证据 doc_count）未 mock → 连接拒绝 → 单项证据缺失
    （evidence_available=false），不影响探测登记与 keep 决策路径。
    """

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


_PROJECT_ROOTS = [
    "viking://resources/projects/context-assembler",
    "viking://resources/projects/windows",
    "viking://resources/projects/irobot",
    "viking://resources/projects/osaka",
    "viking://resources/projects/tokyo",
    "viking://resources/projects/kyoto",
]


def _keep_all_response(*args, **kwargs):
    """LLM 决策 mock：全部根 keep（返回 0 语句）。"""
    return json.dumps([
        {"root_uri": uri, "action": "keep", "reason": "test keep"}
        for uri in _PROJECT_ROOTS
    ])


class TestGovernOvRootsProbe:
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(urllib.request, "urlopen",
                            _FakeProjectsUrlopen())
        monkeypatch.setattr(fl, "_ov_search_find", _search_hits)
        monkeypatch.setattr(ref, "_govern_llm_decision", _keep_all_response)
        # graph.json 证据（空图即可：keep 决策不依赖证据值）
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        monkeypatch.setattr(ref, "_graph_json_path", lambda: gp)
        return _mk_probe_db(tmp_path)

    def test_probe_ov_roots_registers_and_keeps(self, monkeypatch, tmp_path):
        """探测登记 + LLM keep：3 个新根入表 enabled=0；返回值 = 0（无执行语句）。"""
        conn = self._setup(monkeypatch, tmp_path)
        daemon = IdleRefinementDaemon()

        # Cycle 1：登记 3 个新根；LLM 决策 keep → 不启用
        assert daemon._govern_ov_roots(conn) == 0
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT root_uri, enabled, origin, last_seen FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"][0] == 0
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

        # Cycle 2：幂等——无重复行、无新登记、仍 keep
        assert daemon._govern_ov_roots(conn) == 0
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT root_uri, enabled FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"] == 0
        assert rows["viking://resources/projects/tokyo"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 6

    def test_govern_keep_decision_no_auto_enable(self, monkeypatch, tmp_path):
        """启用权移交 LLM：keep 决策 → 无自动启用（阈值 env 不再决定启用）。"""
        monkeypatch.setenv("CA_OV_REFERENCE_THRESHOLD", "0.55")
        conn = self._setup(monkeypatch, tmp_path)

        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT root_uri, enabled, origin FROM ov_roots").fetchall()}
        assert rows["viking://resources/projects/osaka"] == (0, "refine_probe")
        assert rows["viking://resources/projects/tokyo"] == (0, "refine_probe")
        assert rows["viking://resources/projects/kyoto"] == (0, "refine_probe")
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 6

    def test_ov_down_registers_nothing(self, monkeypatch, tmp_path):
        """OV fs/tree 失败 → 探测跳过（整体失败 keep 全部），不写表。"""
        def boom(req, timeout=None):
            raise ConnectionRefusedError("ov down")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("LLM 不应被调用")))
        conn = _mk_probe_db(tmp_path)
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ov_roots").fetchone()[0] == 3
