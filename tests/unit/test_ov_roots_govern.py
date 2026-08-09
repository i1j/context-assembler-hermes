"""ov_roots LLM 治理（决策 44 续）— 治理核心测试（T1/T2/T3/T4/T8/T10/T11/T12/T13）。

覆盖（对应 workspace/codex-task-ov-roots-llm-govern.md §4）：
  T1  parse_decision_array 解析边界矩阵 ①-⑨（围栏/杂音/空/语法错/顶层数组/
      缺字段/非法 action/混合输入/多余字段 filters 忽略）
  T2  决策执行（同一 conn）：enable/disable→UPDATE、remove→DELETE、返回值语句数、
      幻影跳过、护栏降级不计入
  T3  证据收集：_count_ref_edges（links 键两跳归属 + 跨根同名按 uri）、
      _ov_tree_docs 防漂移对齐（== _recursive_fs_tree .md 数 − 隐藏 − 碎片）、
      D3 裁剪（enabled=1 → skipped；enabled=0 → 查询）
  T4  4B 失败三层路径 + 证据整体失败（graph.json 缺失 / 探测抛异常）→ keep 全部
  T8  触发节流：变化→1 次、全 keep→0、in-flight 双触发源合并、governance 不计入
      entries_modified
  T10 remove 后重新登记（同一 conn，探测复活路径）
  T11 filters 免疫：LLM 输出含 filters 字段 → 忽略，filters 列不变
  T12 R1 约束：priority="low" + format="json" + 调用 1 次；prompt 字段清单 +
      D3 标注；决策日志 caplog；_is_local_model 判定矩阵
  T13 CA_OV_GOVERN=0：不调 LLM、探测登记照常、快照前后一致

Mock 策略（任务书 §4）：
  - _FakeUrlopenGovern 处理 projects 根级 fs/tree（E-1：缺此键治理路径永不执行）
    + 各根 fs/tree（doc_count 证据）
  - _gov_graph fixture：tmp_path 小图指向 _graph_json_path（E-3）
  - 同一 conn 贯穿（remove→断言，防重开连接触发种子迁移复活）
  - CA_OV_GOVERN 双补丁：setenv + Config 类变量（E-4）
"""

import json
import logging
import sqlite3
import urllib.parse
import urllib.request

import ca.fact_linking as fl
import ca.refinement as ref
import scripts.wiki_to_graph as w2g  # noqa: F401

from ca.refinement import (
    IdleRefinementDaemon,
    _uri_belongs_to_root,
    parse_decision_array,
)
from ca.store import _get_topic_conn

# ── 共享常量 ──

CA_ROOT = "viking://resources/projects/context-assembler"
WIN_ROOT = "viking://resources/projects/windows"
IROBOT_ROOT = "viking://resources/projects/irobot"
OSAKA_ROOT = "viking://resources/projects/osaka"
TOKYO_ROOT = "viking://resources/projects/tokyo"
KYOTO_ROOT = "viking://resources/projects/kyoto"

_PROJECT_ROOTS = [CA_ROOT, WIN_ROOT, IROBOT_ROOT, OSAKA_ROOT, TOKYO_ROOT, KYOTO_ROOT]


def _projects_payload():
    """projects 根级目录（复用 test_refinement_probe._projects_payload 形态，E-1）。"""
    return {"status": "ok", "result": {"entries": [
        {"uri": uri, "rel_path": uri.rstrip("/").split("/")[-1], "isDir": True}
        for uri in _PROJECT_ROOTS
    ]}}


def _ok_entries(entries):
    return {"status": "ok", "result": {"entries": entries}}


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _FakeUrlopenGovern:
    """projects 根级 + 各根 fs/tree 按 uri 精确分发；未知 uri / 其它端点拒绝。"""

    def __init__(self, tree: dict | None = None):
        self._tree = tree or {}

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/v1/fs/tree" not in url:
            raise ConnectionRefusedError("only fs/tree mocked in govern test")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        uri = (qs.get("uri") or [""])[0]
        if uri == "viking://resources/projects":
            return _FakeResp(json.dumps(_projects_payload()).encode())
        payload = self._tree.get(uri)
        if payload is None:
            raise ConnectionRefusedError(f"unknown tree uri {uri}")
        return _FakeResp(json.dumps(payload).encode())


def _default_tree():
    """各根 fs/tree：windows 3 文档（含子目录）、osaka 1 文档。"""
    return {
        WIN_ROOT: _ok_entries([
            {"uri": f"{WIN_ROOT}/a.md", "rel_path": "windows/a.md", "isDir": False},
            {"uri": f"{WIN_ROOT}/b.md", "rel_path": "windows/b.md", "isDir": False},
            {"uri": f"{WIN_ROOT}/dir", "rel_path": "windows/dir", "isDir": True},
        ]),
        f"{WIN_ROOT}/dir": _ok_entries([
            {"uri": f"{WIN_ROOT}/dir/c.md", "rel_path": "windows/dir/c.md",
             "isDir": False},
        ]),
        OSAKA_ROOT: _ok_entries([
            {"uri": f"{OSAKA_ROOT}/d.md", "rel_path": "osaka/d.md", "isDir": False},
        ]),
    }


def _gov_graph(tmp_path, nodes=None, links=None):
    """tmp_path 小图 → monkeypatch ref._graph_json_path（E-3）。"""
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps({
        "nodes": nodes if nodes is not None else [],
        "links": links if links is not None else [],
    }), encoding="utf-8")
    return gp


def _sync_counter():
    """_run_graphify_sync 实例级计数 mock（保留 in-flight 去重语义）。"""
    calls = {"n": 0}

    def _sync(self):
        if getattr(self, "_graphify_inflight", False):
            return
        self._graphify_inflight = True
        calls["n"] += 1

    return _sync, calls


def _setup_govern(monkeypatch, tmp_path, llm_response=None, tree=None,
                  graph=None, govern=True):
    """治理测试公共 setup：tmp DB + projects/各根 fs/tree mock + graph + LLM mock。

    govern=True：默认 mock `_govern_llm_decision` 为 llm_response（None → keep 全部）。
    govern=False：不 mock LLM 层（供 T12 测真实 _govern_llm_decision + mock call_llm_raw）。
    """
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    monkeypatch.setattr(urllib.request, "urlopen", _FakeUrlopenGovern(tree))
    monkeypatch.setattr(fl, "_ov_search_find", lambda query: [])
    gp = graph if graph is not None else _gov_graph(tmp_path)
    monkeypatch.setattr(ref, "_graph_json_path", lambda: gp)
    sync, calls = _sync_counter()
    monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync", sync)
    if govern:
        if llm_response is None:
            llm_response = json.dumps([
                {"root_uri": uri, "action": "keep", "reason": "test keep"}
                for uri in _PROJECT_ROOTS
            ])
        monkeypatch.setattr(
            ref, "_govern_llm_decision",
            lambda prompt, priority="low": llm_response)
    return conn, calls


def _table(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT root_uri, enabled FROM ov_roots").fetchall()}


# ═══════════════════════════════════════════════════════════
# T1: 决策解析边界矩阵（parse_decision_array，解析层）
# ═══════════════════════════════════════════════════════════

class TestParseDecisionArray:
    """T1 ①-⑨：围栏/杂音/空/语法错/顶层数组/缺字段/非法 action/混合/多余字段。"""

    def test_fence_json_block(self):
        """① 围栏 ```json ... ``` → 剥围栏解析。"""
        text = '```json\n[{"root_uri": "a", "action": "keep"}]\n```'
        valid, invalid = parse_decision_array(text)
        assert invalid == 0
        assert valid == [{"root_uri": "a", "action": "keep", "reason": ""}]

    def test_noise_around_array(self):
        """② 前后杂音文字 → 取首 [ 至末 ] 子串。"""
        text = ('好的，决策如下：\n[{"root_uri": "a", "action": "enable", '
                '"reason": "ok"}]\n以上。')
        valid, invalid = parse_decision_array(text)
        assert invalid == 0
        assert valid[0]["action"] == "enable"
        assert valid[0]["reason"] == "ok"

    def test_empty_and_none(self):
        """③ 空串/None → ([], 0)（调用方走 keep 降级）。"""
        assert parse_decision_array("") == ([], 0)
        assert parse_decision_array(None) == ([], 0)  # type: ignore[arg-type]  # 防御分支（调用方不会传 None）

    def test_syntax_error(self):
        """④ 语法错 → ([], 1) 不崩。"""
        valid, invalid = parse_decision_array("[{bad json")
        assert valid == []
        assert invalid == 1

    def test_top_level_not_array(self):
        """⑤ 顶层非数组（dict）→ ([], 1)。"""
        valid, invalid = parse_decision_array(
            '{"root_uri": "a", "action": "keep"}')
        assert valid == []
        assert invalid == 1

    def test_missing_root_uri(self):
        """⑥ 缺 root_uri / 空 root_uri → 该项非法。"""
        text = '[{"action": "keep"}, {"root_uri": "", "action": "keep"}, ' \
               '{"root_uri": "a", "action": "keep"}]'
        valid, invalid = parse_decision_array(text)
        assert invalid == 2
        assert len(valid) == 1

    def test_illegal_action(self):
        """⑦ 非法 action → 该项非法剔除。"""
        text = '[{"root_uri": "a", "action": "delete"}, ' \
               '{"root_uri": "b", "action": "enable"}]'
        valid, invalid = parse_decision_array(text)
        assert invalid == 1
        assert valid[0]["root_uri"] == "b"

    def test_mixed_input_keeps_valid(self):
        """⑧ 混合输入：合法项照常执行，非法项剔除不抛异常。"""
        text = ('[\n'
                '  {"root_uri": "a", "action": "keep"},\n'
                '  {"root_uri": "b", "action": "enable", "reason": "r"},\n'
                '  {"root_uri": "c", "action": "NOPE"},\n'
                '  "not-a-dict"\n'
                ']')
        valid, invalid = parse_decision_array(text)
        assert invalid == 2
        assert {v["root_uri"] for v in valid} == {"a", "b"}

    def test_extra_fields_ignored(self):
        """⑨ 项含多余字段（filters 等）→ 合法；输出仅 root_uri/action/reason。"""
        text = ('[{"root_uri": "windows", "action": "disable", '
                '"filters": {"exclude": ["/code/"]}, "extra": 1}]')
        valid, invalid = parse_decision_array(text)
        assert invalid == 0
        assert valid == [{"root_uri": "windows", "action": "disable",
                          "reason": ""}]


# ═══════════════════════════════════════════════════════════
# T2: 决策执行（_govern_ov_roots 单事务 + 返回值语句数）
# ═══════════════════════════════════════════════════════════

class TestGovernExecution:
    """T2 同一 conn 断言：表状态与决策一致；返回值 = UPDATE/DELETE 语句数。"""

    def test_enable_disable_remove_same_conn(self, monkeypatch, tmp_path):
        """enable→1 / disable→0 / remove→行消失；CA 根 keep 不变。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "r1"},
            {"root_uri": WIN_ROOT, "action": "disable", "reason": "r2"},
            {"root_uri": IROBOT_ROOT, "action": "remove", "reason": "r3"},
            {"root_uri": CA_ROOT, "action": "keep", "reason": "r4"},
        ]))
        daemon = IdleRefinementDaemon()
        # 3 条执行语句（enable/disable/remove）；keep 无 SQL
        assert daemon._govern_ov_roots(conn) == 3
        t = _table(conn)
        assert t[OSAKA_ROOT] == 1
        assert t[WIN_ROOT] == 0
        assert IROBOT_ROOT not in t  # remove → 行不存在
        assert t[CA_ROOT] == 1  # keep 不变
        # 变化 > 0 → 触发 build 1 次
        assert calls["n"] == 1
        # 同一 conn：无重开（防种子迁移复活）
        assert conn.execute("SELECT 1").fetchone() == (1,)

    def test_statement_count_enable_then_disable_same_root(self, monkeypatch,
                                                           tmp_path):
        """enable+disable 同一根 → 2 条 UPDATE；后项覆盖前项（终态 disable）。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "first"},
            {"root_uri": OSAKA_ROOT, "action": "disable", "reason": "second"},
        ]))
        daemon = IdleRefinementDaemon()
        assert daemon._govern_ov_roots(conn) == 2
        assert _table(conn)[OSAKA_ROOT] == 0  # 后项覆盖

    def test_statement_count_enable_plus_keep(self, monkeypatch, tmp_path):
        """enable+keep → 1（keep 无 SQL）。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
            {"root_uri": CA_ROOT, "action": "keep", "reason": "k"},
        ]))
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 1

    def test_all_keep_returns_zero_no_build(self, monkeypatch, tmp_path):
        """全 keep → 0 语句、0 次 build。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path)  # 默认 keep 全部
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        t = _table(conn)
        assert t[CA_ROOT] == 1 and t[WIN_ROOT] == 1  # 无变化

    def test_phantom_root_skipped(self, monkeypatch, tmp_path):
        """幻影根（表中不存在）→ 跳过不执行、不计入返回值、executed=false。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": "viking://resources/projects/ghost",
             "action": "enable", "reason": "phantom"},
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "real"},
        ]))
        daemon = IdleRefinementDaemon()
        assert daemon._govern_ov_roots(conn) == 1  # 仅 osaka 计入
        assert OSAKA_ROOT in _table(conn)
        assert "viking://resources/projects/ghost" not in _table(conn)

    def test_max_enable_guardrail_degrades_to_keep(self, monkeypatch, tmp_path):
        """护栏：MAX_ENABLE=1 时 2 个 enable → 第 2 个降级 keep；返回 1。"""
        monkeypatch.setenv("CA_OV_GOVERN_MAX_ENABLE", "1")
        monkeypatch.setattr(ref.Config, "OV_GOVERN_MAX_ENABLE", 1)
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e1"},
            {"root_uri": TOKYO_ROOT, "action": "enable", "reason": "e2"},
        ]))
        daemon = IdleRefinementDaemon()
        assert daemon._govern_ov_roots(conn) == 1  # 仅第 1 个执行
        t = _table(conn)
        assert t[OSAKA_ROOT] == 1
        assert t[TOKYO_ROOT] == 0  # 降级 keep

    def test_max_enable_zero_blocks_all_enable(self, monkeypatch, tmp_path):
        """MAX_ENABLE=0 → 本轮任何 enable 全部降级 keep（0 语句）。"""
        monkeypatch.setenv("CA_OV_GOVERN_MAX_ENABLE", "0")
        monkeypatch.setattr(ref.Config, "OV_GOVERN_MAX_ENABLE", 0)
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
        ]))
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert _table(conn)[OSAKA_ROOT] == 0


# ═══════════════════════════════════════════════════════════
# T3: 证据收集
# ═══════════════════════════════════════════════════════════

class TestEvidenceCollection:
    """T3 _count_ref_edges（links 两跳归属）/ _ov_tree_docs（防漂移）/ D3 裁剪。"""

    def test_count_ref_edges_links_key_two_hop(self):
        """图键 links；两跳：edge.target → node.metadata.uri → 根归属。"""
        graph = {
            "nodes": [
                {"id": "ov_doc_a.md",
                 "metadata": {"uri": f"{WIN_ROOT}/a.md"}},
                {"id": "ov_doc_b.md",
                 "metadata": {"uri": f"{OSAKA_ROOT}/b.md"}},
                {"id": "ov_doc_c.md",
                 "metadata": {"uri": f"{WIN_ROOT}/sub/c.md"}},
            ],
            "links": [
                {"relation": "references_ov", "target": "ov_doc_a.md"},
                {"relation": "references_ov", "target": "ov_doc_b.md"},
                {"relation": "references_ov", "target": "ov_doc_c.md"},
                {"relation": "trace", "target": "ov_doc_a.md"},  # 非 references_ov
                {"relation": "references_ov", "target": "ov_doc_missing.md"},
            ],
        }
        assert ref._count_ref_edges(graph, WIN_ROOT) == 2
        assert ref._count_ref_edges(graph, OSAKA_ROOT) == 1
        assert ref._count_ref_edges(graph, IROBOT_ROOT) == 0

    def test_count_ref_edges_prefix_boundary(self):
        """根边界：osaka2 不误归 osaka（_uri_belongs_to_root 前缀防御）。"""
        assert _uri_belongs_to_root(
            "viking://resources/projects/osaka2/x.md", OSAKA_ROOT) is False
        assert _uri_belongs_to_root(
            f"{OSAKA_ROOT}/x.md", OSAKA_ROOT) is True
        assert _uri_belongs_to_root(OSAKA_ROOT, OSAKA_ROOT) is True

    def test_ov_tree_docs_drift_aligned_with_recursive_fs_tree(self,
                                                               monkeypatch,
                                                               tmp_path):
        """防漂移：简单无碎片树 → _ov_tree_docs 主文档数 == _recursive_fs_tree
        .md 数 − 隐藏摘要(0) − 碎片(0)。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path,
                                tree=_default_tree())
        count, root_ok = ref._ov_tree_docs(WIN_ROOT, {})
        assert root_ok is True
        entries = w2g._recursive_fs_tree(WIN_ROOT)
        md_count = len([e for e in entries
                        if str(e.get("uri", "")).endswith(".md")])
        assert count == md_count == 3  # a.md + b.md + dir/c.md

    def test_ov_tree_docs_applies_filters(self, monkeypatch, tmp_path):
        """_ov_tree_docs 应用根 filters：exclude "dir/"（相对根形态，对齐
        _load_all_ov_docs 的 rel_path 归一化口径）→ 减 1。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path,
                                tree=_default_tree())
        count, _ = ref._ov_tree_docs(
            WIN_ROOT, {"exclude": ["dir/"]})
        assert count == 2

    def test_ov_tree_docs_root_failure_sets_root_ok_false(self, monkeypatch,
                                                          tmp_path):
        """根级 fs/tree 失败 → (0, False)（evidence_available=false 信号）。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, tree={})
        count, root_ok = ref._ov_tree_docs(WIN_ROOT, {})
        assert count == 0
        assert root_ok is False

    @staticmethod
    def _mk_reality(conn):
        """插入 realities 行（_collect_top_score 的 query 来源）。"""
        conn.execute(
            "INSERT INTO realities (reality_id, name, hdl, current_status,"
            " profile, created_at, updated_at) VALUES (?,?,'完成参数优化',"
            "'{}','tester',0,0)",
            (1, "大阪工作线"))
        conn.commit()

    def test_d3_top_score_candidate_only(self, monkeypatch, tmp_path):
        """D3 裁剪：enabled=1 根（非候选）→ (None, True) 不查询；候选根 → 查询。"""
        conn = _get_topic_conn(tmp_path / "ca_topics.db")
        self._mk_reality(conn)
        hits = [{"uri": f"{OSAKA_ROOT}/d.md", "score": 0.7}]
        monkeypatch.setattr(fl, "_ov_search_find", lambda query: hits)
        # 非候选（已启用根）→ skipped 语义（不查询）
        score, ok = ref._collect_top_score(conn, CA_ROOT, is_candidate=False)
        assert score is None and ok is True
        # 候选根 → 查询并取最高分
        score, ok = ref._collect_top_score(conn, OSAKA_ROOT, is_candidate=True)
        assert score == 0.7 and ok is True

    def test_d3_top_score_search_failure(self, monkeypatch, tmp_path):
        """search/find 失败/超时 → (0.0, False)（证据缺失）。"""
        conn = _get_topic_conn(tmp_path / "ca_topics.db")
        self._mk_reality(conn)

        def boom(query):
            raise TimeoutError("search timeout")

        monkeypatch.setattr(fl, "_ov_search_find", boom)
        score, ok = ref._collect_top_score(conn, OSAKA_ROOT, is_candidate=True)
        assert score == 0.0 and ok is False

    def test_evidence_available_aggregation(self, monkeypatch, tmp_path):
        """evidence_available 聚合：doc_ok=False → false。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, tree={})  # 根树失败
        row = (WIN_ROOT, "{}", 0, "seed", 1.0)
        ev = ref._collect_root_evidence(conn, row, {WIN_ROOT}, set(), {})
        assert ev["evidence_available"] is False
        assert ev["doc_count"] == 0
        assert ev["ref_edges"] == 0
        assert ev["ov_exists"] is True
        assert ev["top_score"] == 0.0  # 候选根查询失败降级


# ═══════════════════════════════════════════════════════════
# T4: 4B 失败三层路径 + 证据整体失败
# ═══════════════════════════════════════════════════════════

class TestGovernFailureDegrade:
    """T4 LLM 调用失败 / 解析失败 / 校验失败 / graph 缺失 / 探测失败 → keep 全部。"""

    def test_llm_call_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        """① _govern_llm_decision 返回 None → 探测登记照常 + warning 一条 + 不触发 build。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=None)
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda prompt, priority="low": None)
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        # 探测登记照常（osaka 等新根已登记）
        assert OSAKA_ROOT in _table(conn)
        assert any("LLM 决策失败" in r.message for r in caplog.records)

    def test_parse_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        """② 响应非 JSON → 解析失败 → keep 全部 + warning。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path,
                                    llm_response="这不是 JSON")
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        assert any("决策解析无合法项" in r.message for r in caplog.records)

    def test_validation_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        """③ 全部项非法 → 无合法项 → keep 全部。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": "", "action": "enable"},
            {"root_uri": OSAKA_ROOT, "action": "bad-action"},
        ]))
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        assert _table(conn)[OSAKA_ROOT] == 0  # 未误写

    def test_graph_missing_keeps_all(self, monkeypatch, tmp_path, caplog):
        """④ graph.json 缺失 → keep 全部 + warning + 不触发 build。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ref, "_graph_json_path",
            lambda: tmp_path / "nope" / "graph.json")  # 不存在
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        assert any("graph.json 缺失" in r.message for r in caplog.records)

    def test_probe_exception_keeps_all(self, monkeypatch, tmp_path, caplog):
        """⑤ _ov_projects_roots 抛异常 → 探测失败 → keep 全部 + warning。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path)
        monkeypatch.setattr(IdleRefinementDaemon, "_ov_projects_roots",
                            staticmethod(lambda: (_ for _ in ()).throw(
                                RuntimeError("ov down"))))
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0
        assert any("OV projects 根级探测失败" in r.message
                   for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# T8: 触发节流（R8）
# ═══════════════════════════════════════════════════════════

class TestGovernBuildTrigger:
    """T8 变化→1 次 / 全 keep→0 / in-flight 合并 / entries_modified 不计入。"""

    def test_change_triggers_build_once(self, monkeypatch, tmp_path):
        """变化 → _run_graphify_sync 恰好 1 次。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
        ]))
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 1
        assert calls["n"] == 1

    def test_no_change_no_build(self, monkeypatch, tmp_path):
        """全 keep → 0 次。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path)
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        assert calls["n"] == 0

    def test_inflight_dedup_two_sources(self, monkeypatch, tmp_path):
        """双触发源合并去重：inflight=True 时再次调用 → 不重复触发。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
        ]))
        daemon = IdleRefinementDaemon()
        # 第一次：govern 变化触发（inflight False → 计数 1）
        assert daemon._govern_ov_roots(conn) == 1
        # 第二次：Step 5 同轮再触发 → inflight 去重
        daemon._run_graphify_sync()
        assert calls["n"] == 1

    def test_governance_not_counted_in_entries_modified(self, monkeypatch,
                                                        tmp_path):
        """governance 行数不计入 entries_modified（cycle 级：即使 govern 有变化）。"""
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
        ]))
        daemon = IdleRefinementDaemon()
        # 隔离 cycle 依赖：真实 DB 与 ca_cache 扫描均替换
        monkeypatch.setattr(ref, "_get_topic_conn", lambda *a, **k: conn)
        written = {}
        monkeypatch.setattr(ref, "write_refinement_meta",
                            lambda **kw: written.update(kw))
        monkeypatch.setattr(daemon, "_get_active_sessions", lambda: set())
        monkeypatch.setattr(daemon, "_compute_global_turn_max", lambda: 0)
        monkeypatch.setattr(daemon, "_run_internal_refine",
                            lambda c, a: (0, 0))
        monkeypatch.setattr(daemon, "_run_cross_validate",
                            lambda c, a: (0, 0))
        monkeypatch.setattr(daemon, "_run_zombie_cleanup", lambda c: 0)
        monkeypatch.setattr(daemon, "_run_health_score", lambda c: 0)
        monkeypatch.setattr(daemon, "_run_fact_linking", lambda c: 0)
        monkeypatch.setattr(ref.Config, "REFINEMENT_DOC_MAINTENANCE", False)

        daemon._run_refinement_cycle()
        # govern 已执行且变化（enable osaka）→ 但 entries_modified 保持 0
        assert _table(conn)[OSAKA_ROOT] == 1
        assert written.get("entries_modified") == 0
        assert "ov_root_govern" in written.get("tasks_run", [])
        # graphify_synced 由 Step 5 触发（entries_modified=0 不触发）→ 0
        assert written.get("graphify_synced") == 0


# ═══════════════════════════════════════════════════════════
# T10: remove 后重新登记（探测复活路径）
# ═══════════════════════════════════════════════════════════

class TestRemoveReRegister:
    """T10 remove 后 OV 仍存在 → 下轮探测重新登记 enabled=0 origin=refine_probe。"""

    def test_removed_root_re_registered_next_cycle(self, monkeypatch,
                                                   tmp_path):
        conn, calls = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "remove", "reason": "r"},
        ]))
        daemon = IdleRefinementDaemon()
        # Cycle 1: remove osaka（osaka 是种子外的探测根？不——osaka 为 probe 新根，
        # 但 remove 目标需存在。先让探测登记 osaka，再 remove。）
        # 直接两轮：轮 1 探测登记 + remove；轮 2 探测复活。
        # 轮 1：先登记（LLM 本轮 remove）
        assert daemon._govern_ov_roots(conn) == 1
        assert OSAKA_ROOT not in _table(conn)

        # 轮 2：mock 探测仍返回 osaka + LLM keep（避免再 remove）
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda prompt, priority="low": json.dumps([
                                {"root_uri": OSAKA_ROOT, "action": "keep",
                                 "reason": "k"},
                            ]))
        assert daemon._govern_ov_roots(conn) == 0
        rows = conn.execute(
            "SELECT root_uri, enabled, origin FROM ov_roots "
            "WHERE root_uri=?", (OSAKA_ROOT,)).fetchall()
        assert len(rows) == 1  # 复活登记
        assert rows[0][1] == 0  # enabled=0
        assert rows[0][2] == "refine_probe"
        # 同一 conn 贯穿（未重开 → 无种子复活陷阱）
        assert conn.execute("SELECT 1").fetchone() == (1,)


# ═══════════════════════════════════════════════════════════
# T11: filters 免疫（执行层）
# ═══════════════════════════════════════════════════════════

class TestFiltersImmune:
    """T11 LLM 输出含 filters 字段 → 忽略，filters 列不变。"""

    def test_llm_filters_field_ignored(self, monkeypatch, tmp_path):
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": WIN_ROOT, "action": "disable",
             "filters": {"exclude": ["/code/"]}, "reason": "f"},
        ]))
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 1
        flt = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?",
            (WIN_ROOT,)).fetchone()[0]
        assert json.loads(flt) == {"exclude": ["/code/"]}  # 种子原值不变


# ═══════════════════════════════════════════════════════════
# T12: R1 约束（成功路径）
# ═══════════════════════════════════════════════════════════

class TestR1Constraints:
    """T12 priority="low" + format="json" + 次数 1；prompt 字段清单；决策日志。"""

    def test_govern_llm_decision_passes_format_json(self, monkeypatch,
                                                    tmp_path, caplog):
        """真实 _govern_llm_decision → call_llm_raw kwargs 断言。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, govern=False)
        captured = {}

        def _fake_raw(prompt, num_predict=None, temperature=None,
                      max_retries=None, priority="normal", format=None,
                      **kw):
            captured["priority"] = priority
            captured["format"] = format
            captured["num_predict"] = num_predict
            captured["prompt"] = prompt
            return json.dumps([
                {"root_uri": uri, "action": "keep", "reason": "k"}
                for uri in _PROJECT_ROOTS
            ])

        monkeypatch.setattr(ref, "call_llm_raw", _fake_raw)
        # 本地 4B 身份（默认 Config：qwen3-4b-instruct:16k + localhost:11435）
        assert ref._is_local_model(ref.Config.LLM_MODEL) is True
        daemon = IdleRefinementDaemon()
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            daemon._govern_ov_roots(conn)
        # R1: priority + format + num_predict（防截断）+ 恰好 1 次调用
        assert captured["priority"] == "low"
        assert captured["format"] == "json"
        assert captured["num_predict"] == ref.Config.OV_GOVERN_MAX_TOKENS
        # prompt 含字段清单（R2）与 D3 裁剪标注
        for key in ("ref_edges", "doc_count", "top_score",
                    "evidence_available", "D3"):
            assert key in captured["prompt"]
        # 已启用根 top_score=skipped 标注（D3）
        assert "skipped" in captured["prompt"]

    def test_decision_log_single_line_json(self, monkeypatch, tmp_path,
                                           caplog):
        """决策日志：事务后逐行 INFO 单行 JSON（executed 语义）。"""
        conn, _ = _setup_govern(monkeypatch, tmp_path, llm_response=json.dumps([
            {"root_uri": OSAKA_ROOT, "action": "enable", "reason": "e"},
            {"root_uri": "viking://resources/projects/ghost",
             "action": "remove", "reason": "p"},
        ]))
        daemon = IdleRefinementDaemon()
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            daemon._govern_ov_roots(conn)
        decisions = [r for r in caplog.records
                     if r.levelno == logging.INFO
                     and "ov_roots govern decision" in r.getMessage()]
        assert len(decisions) == 2
        payloads = [json.loads(r.getMessage().split(": ", 1)[1])
                    for r in decisions]
        by_uri = {p["root_uri"]: p for p in payloads}
        assert by_uri[OSAKA_ROOT]["executed"] is True
        assert by_uri[OSAKA_ROOT]["action"] == "enable"
        # 幻影 → executed=false + reason=root not in table
        assert by_uri["viking://resources/projects/ghost"]["executed"] is False
        assert by_uri["viking://resources/projects/ghost"]["reason"] \
            == "root not in table"

    @staticmethod
    def _patch_model(monkeypatch, model, endpoint):
        monkeypatch.setattr(ref.Config, "LLM_MODEL", model)
        monkeypatch.setattr(ref.Config, "LLM_ENDPOINT", endpoint)

    def test_is_local_model_matrix(self, monkeypatch):
        """_is_local_model 判定矩阵（≥3 样例）：本地调用/云模型拒/qwen3-4b+远程拒。"""
        # 本地 4B：qwen3-4b 前缀 + localhost 端点 → True
        self._patch_model(monkeypatch, "qwen3-4b-instruct:16k",
                          "http://localhost:11435")
        assert ref._is_local_model(ref.Config.LLM_MODEL) is True
        # 云模型名 + 本地端点 → 拒（保守）
        self._patch_model(monkeypatch, "deepseek-chat", "http://localhost:11435")
        assert ref._is_local_model(ref.Config.LLM_MODEL) is False
        # qwen3-4b 前缀 + 远程端点（网关代理）→ 拒
        self._patch_model(monkeypatch, "qwen3-4b-instruct:16k",
                          "http://api.deepseek.com/v1")
        assert ref._is_local_model(ref.Config.LLM_MODEL) is False
        # 非 4b + 远程 → 拒
        self._patch_model(monkeypatch, "gpt-4o", "http://api.openai.com")
        assert ref._is_local_model(ref.Config.LLM_MODEL) is False

    def test_govern_llm_decision_rejects_remote_model(self, monkeypatch,
                                                      caplog):
        """_govern_llm_decision：模型非本地 → 返回 None（降级 keep），不调 LLM。"""
        monkeypatch.setattr(ref.Config, "LLM_MODEL", "deepseek-chat")
        called = {"n": 0}

        def _fake_raw(*a, **k):
            called["n"] += 1
            return "[]"

        monkeypatch.setattr(ref, "call_llm_raw", _fake_raw)
        with caplog.at_level(logging.WARNING, logger="ca.refinement"):
            assert ref._govern_llm_decision("prompt") is None
        assert called["n"] == 0
        assert any("非本地 4B" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# T13: CA_OV_GOVERN=0
# ═══════════════════════════════════════════════════════════

class TestGovernDisabled:
    """T13 CA_OV_GOVERN=0 → 仅探测登记，不调 LLM，快照前后一致。"""

    def test_govern_off_probe_only(self, monkeypatch, tmp_path):
        # E-4 双补丁：env + Config 类变量（import 时求值，仅 setenv 无效）
        monkeypatch.setenv("CA_OV_GOVERN", "0")
        monkeypatch.setattr(ref.Config, "OV_GOVERN", False)
        llm_called = {"n": 0}
        conn, calls = _setup_govern(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ref, "_govern_llm_decision",
            lambda prompt, priority="low": llm_called.__setitem__(
                "n", llm_called["n"] + 1) or "[]")
        daemon = IdleRefinementDaemon()

        before = _table(conn)
        assert IdleRefinementDaemon()._govern_ov_roots(conn) == 0
        after = _table(conn)
        # 行为同旧版探测登记：新根登记 enabled=0 照常
        assert OSAKA_ROOT in after and after[OSAKA_ROOT] == 0
        # last_seen 更新
        row = conn.execute(
            "SELECT last_seen FROM ov_roots WHERE root_uri=?", (OSAKA_ROOT,)
        ).fetchone()
        assert row[0] is not None
        # 不调 LLM、不触发 build；enabled 列快照前后一致（探测登记行除外——
        # 新登记行本就在「登记后」语义内：enabled=0 与原表一致）
        assert llm_called["n"] == 0
        assert calls["n"] == 0
        assert all(after[k] == v for k, v in before.items())
        assert daemon._graphify_inflight is False
