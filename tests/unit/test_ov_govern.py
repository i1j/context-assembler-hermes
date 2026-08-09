"""决策 44 续：ov_roots LLM 治理（T1-T5 / T8 / T10-T13）。

覆盖：
  T1   parse_decision_array 边界矩阵（围栏/杂音/空/语法错/顶层/缺字段/非法 action/混合/多余字段）
  T2   决策执行（同一 conn）：enable/disable→UPDATE；remove→DELETE；返回值计数
  T3   证据收集：ref_edges（links 键 + 跨根同名按 uri 归属）、doc_count（filters）、
       top_score（D3 裁剪）、防漂移对齐 _recursive_fs_tree
  T4   4B 失败三层路径 + 证据整体失败（graph.json 缺失 / 探测抛异常）
  T5   手动接口：set_ov_root_filters / list_ov_roots
  T8   触发节流：变化→1 次、全 keep→0、双触发源合并去重、governance 不计入 entries_modified
  T10  remove 后 OV 仍存在 → 下轮探测重新登记 enabled=0（同一 conn）
  T11  filters 免疫：LLM 输出含 filters 字段 → 忽略，filters 列不变
  T12  R1 约束：call_llm_raw kwargs（priority=low + format=json + 1 次）、
       prompt 字段清单 + D3 裁剪标注、决策日志逐行 JSON、模型身份矩阵
  T13  CA_OV_GOVERN=0 → 不调 LLM、仅探测登记
"""

import json
import logging
import sqlite3
import subprocess
import urllib.parse
import urllib.request

import pytest

import ca.fact_linking as fl
import ca.refinement as ref
import ca.store as store_mod
import scripts.wiki_to_graph as w2g

from ca.refinement import (
    IdleRefinementDaemon,
    _build_govern_prompt,
    _collect_root_evidence,
    _count_ref_edges,
    _graph_json_path,
    _is_local_model,
    _ov_tree_docs,
    _uri_belongs_to_root,
    parse_decision_array,
)
from ca.store import _get_topic_conn, get_last_refinement_meta, list_ov_roots, set_ov_root_filters

CA = "viking://resources/projects/context-assembler"
WIN = "viking://resources/projects/windows"
ROBOT = "viking://resources/projects/irobot"
OSAKA = "viking://resources/projects/osaka"
TOKYO = "viking://resources/projects/tokyo"
KYOTO = "viking://resources/projects/kyoto"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """测试隔离：store 默认路径指向 tmp DB；OV_GOVERN 固定开启（T13 另行覆盖）。"""
    monkeypatch.setattr(store_mod, "_get_topic_store_path",
                        lambda: tmp_path / "ca_topics.db")
    monkeypatch.setenv("CA_OV_GOVERN", "1")
    monkeypatch.setattr(ref.Config, "OV_GOVERN", True)
    monkeypatch.setattr(ref.Config, "OV_GOVERN_MAX_ENABLE", 1)


# ── mock 基建 ──

def _projects_payload():
    return {"status": "ok", "result": {"entries": [
        {"uri": CA, "rel_path": "context-assembler", "isDir": True},
        {"uri": WIN, "rel_path": "windows", "isDir": True},
        {"uri": ROBOT, "rel_path": "irobot", "isDir": True},
        {"uri": OSAKA, "rel_path": "osaka", "isDir": True},
        {"uri": TOKYO, "rel_path": "tokyo", "isDir": True},
        {"uri": KYOTO, "rel_path": "kyoto", "isDir": True},
    ]}}


def _ok_entries(entries):
    return {"status": "ok", "result": {"entries": entries}}


def _root_trees():
    """各根 fs/tree（证据 doc_count）：windows 含 code/（种子 filters 排除）。"""
    return {
        WIN: _ok_entries([
            {"uri": f"{WIN}/show-me-the-story/decisions/01-windows-ollama-backend.md",
             "rel_path": "windows/show-me-the-story/decisions/01-windows-ollama-backend.md",
             "isDir": False},
            {"uri": f"{WIN}/comfyui-model-setup/decisions/03-electron-crash-browser.md",
             "rel_path": "windows/comfyui-model-setup/decisions/03-electron-crash-browser.md",
             "isDir": False},
            {"uri": f"{WIN}/comfyui-model-setup/code/notes.md",
             "rel_path": "windows/comfyui-model-setup/code/notes.md",
             "isDir": False},
            {"uri": f"{WIN}/INDEX.md", "rel_path": "windows/INDEX.md",
             "isDir": False},
        ]),
        OSAKA: _ok_entries([
            {"uri": f"{OSAKA}/decisions/01.md",
             "rel_path": "osaka/decisions/01.md", "isDir": False},
            {"uri": f"{OSAKA}/INDEX.md", "rel_path": "osaka/INDEX.md",
             "isDir": False},
        ]),
        TOKYO: _ok_entries([
            {"uri": f"{TOKYO}/decisions/02.md",
             "rel_path": "tokyo/decisions/02.md", "isDir": False},
        ]),
        KYOTO: _ok_entries([
            {"uri": f"{KYOTO}/decisions/03.md",
             "rel_path": "kyoto/decisions/03.md", "isDir": False},
        ]),
    }


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _GovUrlopen:
    """projects 根级 + 各根 fs/tree；其它请求（webdav/search）拒绝。"""

    def __init__(self, trees=None, projects=None):
        self._trees = trees if trees is not None else _root_trees()
        self._projects = projects if projects is not None else _projects_payload()

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/v1/fs/tree" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            uri = (qs.get("uri") or [""])[0]
            if uri == "viking://resources/projects":
                return _FakeResp(json.dumps(self._projects).encode())
            payload = self._trees.get(uri)
            if payload is not None:
                return _FakeResp(json.dumps(payload).encode())
        raise ConnectionRefusedError(f"unexpected request: {url}")


def _gov_graph(tmp_path, monkeypatch):
    """小图（E-3）：links 键 + references_ov 边 + 跨根同名 nid 按 uri 归属。"""
    gp = tmp_path / "graph.json"
    graph = {
        "nodes": [
            {"id": "reality_1", "file_type": "knowledge"},
            {"id": "ov_doc_01-windows-ollama-backend.md",
             "metadata": {"uri": f"{WIN}/show-me-the-story/decisions/01-windows-ollama-backend.md"}},
            {"id": "ov_doc_03-electron-crash-browser.md",
             "metadata": {"uri": f"{WIN}/comfyui-model-setup/decisions/03-electron-crash-browser.md"}},
            {"id": "ov_doc_index.md",
             "metadata": {"uri": f"{CA}/INDEX.md"}},
            {"id": "ov_doc_01.md",
             "metadata": {"uri": f"{OSAKA}/decisions/01.md"}},
        ],
        "links": [
            {"source": "reality_1", "target": "ov_doc_01-windows-ollama-backend.md",
             "relation": "references_ov"},
            {"source": "reality_1", "target": "ov_doc_03-electron-crash-browser.md",
             "relation": "references_ov"},
            {"source": "reality_2", "target": "ov_doc_index.md",
             "relation": "references_ov"},
            {"source": "reality_3", "target": "ov_doc_01.md",
             "relation": "references_ov"},
            {"source": "reality_1", "target": "code_node_x", "relation": "calls"},
        ],
    }
    gp.write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setattr(ref, "_graph_json_path", lambda: gp)
    return graph


def _mk_gov_db(tmp_path):
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)  # 种子 3 行 + user_version=1
    return db, conn


def _setup_gov(monkeypatch, tmp_path, decision=None):
    """标准治理环境：projects/根树 mock + graph.json + LLM 决策 mock（None=调用失败）。"""
    db, conn = _mk_gov_db(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen())
    _gov_graph(tmp_path, monkeypatch)
    monkeypatch.setattr(ref, "_govern_llm_decision",
                        lambda prompt, priority="low": decision)
    daemon = IdleRefinementDaemon()
    return db, conn, daemon


def _rows(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT root_uri, enabled FROM ov_roots").fetchall()}


# ── T1: parse_decision_array 边界矩阵 ──

class TestParseDecisionArray:
    @pytest.mark.parametrize("text,valid_n,invalid_n", [
        # ① 围栏
        ('```json\n[{"root_uri": "a", "action": "keep"}]\n```', 1, 0),
        # ② 杂音
        ('好的，决策如下: [{"root_uri": "a", "action": "enable", "reason": "x"}] 完', 1, 0),
        # ③ 空
        ("", 0, 0),
        # ④ 语法错
        ("{not json", 0, 1),
        # ⑤ 顶层非数组
        ('{"root_uri": "a", "action": "keep"}', 0, 1),
        # ⑥ 缺字段
        ('[{"root_uri": "a"}]', 0, 1),
        # ⑦ 非法 action
        ('[{"root_uri": "a", "action": "explode"}]', 0, 1),
    ])
    def test_matrix(self, text, valid_n, invalid_n):
        valid, n = parse_decision_array(text)
        assert len(valid) == valid_n
        assert n == invalid_n

    def test_mixed_input_valid_executed(self):
        """⑧ 混合输入：合法项保留，非法项剔除，不抛异常。"""
        text = json.dumps([
            {"root_uri": "a", "action": "enable", "reason": "ok"},
            {"root_uri": "b", "action": "bad"},
            "not a dict",
            {"root_uri": "", "action": "keep"},
        ])
        valid, n = parse_decision_array(text)
        assert len(valid) == 1
        assert valid[0] == {"root_uri": "a", "action": "enable", "reason": "ok"}
        assert n == 3

    def test_extra_fields_ignored(self):
        """⑨ 多余字段（filters）不构成非法，该根执行。"""
        text = json.dumps([
            {"root_uri": "a", "action": "keep", "reason": "ok",
             "filters": {"include": ["x"]}},
        ])
        valid, n = parse_decision_array(text)
        assert len(valid) == 1
        assert n == 0
        assert "filters" not in valid[0]


# ── T2: 决策执行（同一 conn）──

class TestGovernDecisionExecution:
    def test_enable_disable_remove_count(self, monkeypatch, tmp_path):
        """enable/disable→UPDATE；remove→DELETE；返回值 = 执行语句数。"""
        decision = json.dumps([
            {"root_uri": OSAKA, "action": "enable", "reason": "r1"},
            {"root_uri": TOKYO, "action": "disable", "reason": "r2"},
            {"root_uri": KYOTO, "action": "remove", "reason": "r3"},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        n = daemon._govern_ov_roots(conn)
        assert n == 3
        rows = _rows(conn)
        assert rows[OSAKA] == 1
        assert rows[TOKYO] == 0
        assert KYOTO not in rows
        assert rows[WIN] == 1  # 未涉及其它根

    def test_enable_then_keep_counts_one(self, monkeypatch, tmp_path):
        decision = json.dumps([
            {"root_uri": OSAKA, "action": "enable", "reason": "r"},
            {"root_uri": TOKYO, "action": "keep", "reason": "r"},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        assert daemon._govern_ov_roots(conn) == 1
        assert _rows(conn)[OSAKA] == 1
        assert _rows(conn)[TOKYO] == 0

    def test_keep_all_returns_zero(self, monkeypatch, tmp_path):
        decision = json.dumps([
            {"root_uri": OSAKA, "action": "keep", "reason": "r"},
            {"root_uri": TOKYO, "action": "keep", "reason": "r"},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        assert daemon._govern_ov_roots(conn) == 0
        rows = _rows(conn)
        assert rows[OSAKA] == 0  # 探测登记照常
        assert rows[TOKYO] == 0

    def test_duplicate_root_later_wins(self, monkeypatch, tmp_path):
        """同一根 enable+disable → 2 语句；最终状态 disable。"""
        decision = json.dumps([
            {"root_uri": OSAKA, "action": "enable", "reason": "r1"},
            {"root_uri": OSAKA, "action": "disable", "reason": "r2"},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        assert daemon._govern_ov_roots(conn) == 2
        assert _rows(conn)[OSAKA] == 0

    def test_phantom_and_guardrail_not_counted(self, monkeypatch, tmp_path):
        """幻影根（不在表）+ 护栏降级（enable 超上限）不计入返回值。"""
        decision = json.dumps([
            {"root_uri": "viking://resources/projects/ghost",
             "action": "enable", "reason": "r"},
            {"root_uri": OSAKA, "action": "enable", "reason": "r1"},
            {"root_uri": TOKYO, "action": "enable", "reason": "r2"},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        n = daemon._govern_ov_roots(conn)
        assert n == 1  # ghost 幻影跳过；osaka 第 1 个 enable 执行；tokyo 降级 keep
        rows = _rows(conn)
        assert rows[OSAKA] == 1
        assert rows[TOKYO] == 0


# ── T3: 证据收集 ──

class TestEvidenceCollection:
    def _mk_evidence_db(self, tmp_path, monkeypatch):
        _, conn = _mk_gov_db(tmp_path)
        monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen())
        graph = _gov_graph(tmp_path, monkeypatch)
        # 候选根：osaka（enabled=0）——先登记再置 0
        conn.execute(
            "INSERT OR IGNORE INTO ov_roots "
            "(root_uri, filters, enabled, origin, added_at, last_seen) "
            "VALUES (?, '{}', 0, 'refine_probe', 0, 0)", (OSAKA,))
        conn.commit()
        for rid, name in ((1, "大阪工作线"), (2, "Windows Ollama 后端")):
            conn.execute(
                "INSERT INTO realities (reality_id, name, hdl, current_status,"
                " profile, created_at, updated_at) "
                "VALUES (?,?,?,'{}','tester',0,0)", (rid, name, ""))
        conn.commit()

        def _hits(query):
            if "大阪工作线" in query:
                return [{"uri": f"{OSAKA}/decisions/01.md", "score": 0.66}]
            if "ollama" in query.lower():
                return [{"uri": f"{WIN}/show-me-the-story/decisions/01-windows-ollama-backend.md",
                         "score": 0.9}]
            return []
        monkeypatch.setattr(fl, "_ov_search_find", _hits)
        return conn, graph

    def test_ref_edges_links_key_and_uri_attribution(self, tmp_path,
                                                     monkeypatch):
        """图键 links；references_ov 两跳归属；跨根同名 nid 按 metadata.uri。"""
        conn, graph = self._mk_evidence_db(tmp_path, monkeypatch)
        root_set = {CA, WIN, ROBOT, OSAKA, TOKYO, KYOTO}
        ev_win = _collect_root_evidence(
            conn, (WIN, '{"exclude":["/code/"]}', 1, "seed", 1.0),
            root_set, set(), graph)
        ev_ca = _collect_root_evidence(
            conn, (CA, "{}", 1, "seed", 1.0),
            root_set, set(), graph)
        ev_osaka = _collect_root_evidence(
            conn, (OSAKA, "{}", 0, "refine_probe", 1.0),
            root_set, {OSAKA}, graph)
        # windows: 2 条 references_ov 边（code_node 的 calls 边不计）
        assert ev_win["ref_edges"] == 2
        # CA: INDEX.md 边归属 CA（即使与 windows 的 INDEX.md 同 nid 概念）
        assert ev_ca["ref_edges"] == 1
        assert ev_osaka["ref_edges"] == 1
        # 图键确实是 links（edges 键不存在）
        assert "links" in graph and "edges" not in graph

    def test_doc_count_filters_and_top_score(self, tmp_path, monkeypatch):
        """doc_count 应用 filters（windows exclude /code/）；top_score 仅候选根。"""
        conn, _ = self._mk_evidence_db(tmp_path, monkeypatch)
        root_set = {CA, WIN, ROBOT, OSAKA, TOKYO, KYOTO}
        ev_win = _collect_root_evidence(
            conn, (WIN, '{"exclude":["/code/"]}', 1, "seed", 1.0),
            root_set, set(), {})
        # windows 树 4 文件 − code/notes.md（exclude /code/ 生效）→ 3 文档
        assert ev_win["doc_count"] == 3
        # 已启用根：top_score skipped（D3 裁剪标注），不查询
        assert ev_win["top_score"] == "skipped"
        assert ev_win["evidence_available"] is True
        ev_osaka = _collect_root_evidence(
            conn, (OSAKA, "{}", 0, "refine_probe", 1.0),
            root_set, {OSAKA}, {})
        # osaka 树 2 文件 → 2 文档
        assert ev_osaka["doc_count"] == 2
        # 候选根：search/find 命中 osaka 文档 → top_score=0.66
        assert ev_osaka["top_score"] == pytest.approx(0.66)
        assert ev_osaka["is_new"] is True
        assert ev_osaka["ov_exists"] is True
        assert ev_osaka["evidence_available"] is True

    def test_ov_tree_docs_aligned_with_recursive_tree(self, tmp_path,
                                                      monkeypatch):
        """防漂移：_ov_tree_docs 主文档数 == _recursive_fs_tree .md 数 − 隐藏摘要 − 碎片。"""
        root = OSAKA
        tree = {
            root: _ok_entries([
                {"uri": f"{root}/a.md", "rel_path": "a.md", "isDir": False},
                {"uri": f"{root}/b.md", "rel_path": "b.md", "isDir": False},
                {"uri": f"{root}/a.md/背景.md", "rel_path": "a.md/背景.md",
                 "isDir": False},
                {"uri": f"{root}/.overview.md", "rel_path": ".overview.md",
                 "isDir": False},
                {"uri": f"{root}/sub", "rel_path": "sub", "isDir": True},
            ]),
            f"{root}/sub": _ok_entries([
                {"uri": f"{root}/sub/c.md", "rel_path": "sub/c.md",
                 "isDir": False},
            ]),
        }
        monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen(tree))
        count, ok = _ov_tree_docs(root, {})
        assert ok is True
        entries = w2g._recursive_fs_tree(root)
        md = [e for e in entries
              if str(e.get("uri", "")).endswith(".md")]
        hidden_frags = [e for e in md
                        if ".overview" in e["uri"] or "背景.md" in e["uri"]]
        assert count == len(md) - len(hidden_frags)


# ── T4: 失败路径 ──

class TestGovernFailures:
    def test_llm_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        """4B 调用失败（None）→ 不写决策、探测登记照常、仅 warning 一条。"""
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=None)
            n = daemon._govern_ov_roots(conn)
        assert n == 0
        rows = _rows(conn)
        assert rows[OSAKA] == 0  # 探测登记照常（enabled=0）
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "LLM 决策失败" in warnings[0].getMessage()
        infos = [r for r in caplog.records
                 if r.levelno == logging.INFO and "govern decision" in r.getMessage()]
        assert infos == []  # 无每根决策日志

    def test_parse_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            _, conn, daemon = _setup_gov(monkeypatch, tmp_path,
                                         decision="{garbage not json")
            n = daemon._govern_ov_roots(conn)
        assert n == 0
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "决策解析无合法项" in warnings[0].getMessage()

    def test_validation_failure_keeps_all(self, monkeypatch, tmp_path, caplog):
        """校验失败（全部非法项）→ 不写表 + 仅 warning 一条。"""
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=json.dumps([
                {"root_uri": OSAKA, "action": "explode"}]))
            n = daemon._govern_ov_roots(conn)
        assert n == 0
        assert _rows(conn)[OSAKA] == 0
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "决策解析无合法项" in warnings[0].getMessage()

    def test_graph_missing_keeps_all_no_build(self, monkeypatch, tmp_path,
                                              caplog):
        """graph.json 缺失 → keep 全部 + warning + 不触发 build + 不调 LLM。"""
        db, conn = _mk_gov_db(tmp_path)
        monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen())
        gp = tmp_path / "graph.json"  # 不存在
        monkeypatch.setattr(ref, "_graph_json_path", lambda: gp)
        calls = []
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda *a, **k: calls.append(1) or "[]")
        daemon = IdleRefinementDaemon()
        sync_calls = []
        monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync",
                            lambda self: sync_calls.append(1))
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            n = daemon._govern_ov_roots(conn)
        assert n == 0
        assert calls == []  # 不构建 prompt / 不调 LLM
        assert sync_calls == []
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("keep 全部" in r.getMessage() for r in warnings)

    def test_probe_exception_keeps_all_no_build(self, monkeypatch, tmp_path,
                                                caplog):
        """_ov_projects_roots 抛异常 → 同前（keep 全部 + warning + 不触发 build）。"""
        _, conn = _mk_gov_db(tmp_path)
        def _boom():
            raise RuntimeError("ov down")
        monkeypatch.setattr(IdleRefinementDaemon, "_ov_projects_roots",
                            staticmethod(_boom))
        calls = []
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda *a, **k: calls.append(1) or "[]")
        daemon = IdleRefinementDaemon()
        sync_calls = []
        monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync",
                            lambda self: sync_calls.append(1))
        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            n = daemon._govern_ov_roots(conn)
        assert n == 0
        assert calls == []
        assert sync_calls == []
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("keep 全部" in r.getMessage() for r in warnings)


# ── T5: 手动接口 ──

class TestManualFilters:
    def test_set_filters_valid(self, tmp_path):
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        assert set_ov_root_filters(
            WIN, {"include": ["comfyui-model-setup"], "exclude": ["/code/"]}) is True
        row = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN,)).fetchone()
        assert json.loads(row[0]) == {"include": ["comfyui-model-setup"],
                                      "exclude": ["/code/"]}

    def test_set_filters_invalid_rejected(self, tmp_path):
        db = tmp_path / "ca_topics.db"
        conn = _get_topic_conn(db)
        assert set_ov_root_filters(WIN, ["a"]) is False          # 非 dict
        assert set_ov_root_filters(WIN, {"include": "a"}) is False   # 非 list
        assert set_ov_root_filters(WIN, {"include": [1, 2]}) is False  # 非 str
        assert set_ov_root_filters(WIN, {"exclude": [None]}) is False
        # 空数组合法
        assert set_ov_root_filters(WIN, {"include": [], "exclude": []}) is True
        # root_uri 不在表
        assert set_ov_root_filters("viking://resources/projects/nope",
                                   {}) is False
        # 非法写入被拒绝（最后一次合法写入 {"include": [], "exclude": []} 未被非法项覆盖）
        row = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (WIN,)).fetchone()
        assert json.loads(row[0]) == {"include": [], "exclude": []}

    def test_list_ov_roots_shape(self, tmp_path):
        db = tmp_path / "ca_topics.db"
        _get_topic_conn(db)
        rows = list_ov_roots()
        assert len(rows) == 3
        first = rows[0]
        assert set(first) == {"root_uri", "filters", "enabled", "origin",
                              "added_at", "last_seen"}
        assert isinstance(first["filters"], dict)
        win = next(r for r in rows if r["root_uri"] == WIN)
        assert win["filters"] == {"exclude": ["/code/"]}
        assert win["enabled"] == 1
        assert win["origin"] == "seed"


# ── T8: 触发节流 ──

class TestGovernTrigger:
    def test_change_triggers_sync_once(self, monkeypatch, tmp_path):
        """变化 → 触发 _run_graphify_sync 1 次（实例级 mock 计数）。"""
        decision = json.dumps([{"root_uri": OSAKA, "action": "enable",
                                "reason": "r"}])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        calls = []
        monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync",
                            lambda self: calls.append(1))
        daemon._govern_ov_roots(conn)
        assert len(calls) == 1

    def test_keep_all_no_trigger(self, monkeypatch, tmp_path):
        decision = json.dumps([{"root_uri": OSAKA, "action": "keep",
                                "reason": "r"}])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        calls = []
        monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync",
                            lambda self: calls.append(1))
        daemon._govern_ov_roots(conn)
        assert calls == []

    def test_inflight_dedupe(self, monkeypatch, tmp_path):
        """双触发源合并去重：同轮二次调用不重复 spawn；复位后可再触发。"""
        daemon = IdleRefinementDaemon()
        calls = []
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: calls.append(1))
        daemon._run_graphify_sync()
        daemon._run_graphify_sync()
        assert len(calls) == 1
        daemon._graphify_inflight = False  # cycle 开始复位
        daemon._run_graphify_sync()
        assert len(calls) == 2

    def test_govern_changes_not_counted_in_entries_modified(self, monkeypatch,
                                                            tmp_path):
        """governance 行数不计入 entries_modified（独立断言）。"""
        decision = json.dumps([{"root_uri": OSAKA, "action": "enable",
                                "reason": "r"}])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        monkeypatch.setattr(daemon, "_compute_global_turn_max", lambda: 0)
        monkeypatch.setattr(daemon, "_get_active_sessions", lambda: set())
        monkeypatch.setattr(daemon, "_run_internal_refine",
                            lambda c, s: (0, 0))
        monkeypatch.setattr(daemon, "_run_cross_validate",
                            lambda c, s: (0, 0))
        monkeypatch.setattr(daemon, "_run_zombie_cleanup", lambda c: 0)
        monkeypatch.setattr(daemon, "_run_health_score", lambda c: 0)
        monkeypatch.setattr(daemon, "_run_fact_linking", lambda c: 0)
        sync_calls = []
        monkeypatch.setattr(IdleRefinementDaemon, "_run_graphify_sync",
                            lambda self: sync_calls.append(1))

        daemon._run_refinement_cycle()

        # 治理变更已生效（osaka enabled=1）
        assert _rows(conn)[OSAKA] == 1
        # 但 entries_modified 记录为 0（governance 不计入）
        meta = get_last_refinement_meta()
        assert meta is not None
        assert meta["entries_modified"] == 0
        # 触发去重后 sync 只被治理触发 1 次（Step 5 无 entries_modified 不触发）
        assert len(sync_calls) == 1


# ── T10: remove 后重新登记 ──

class TestRemoveReregister:
    def test_removed_root_reregistered_enabled0(self, monkeypatch, tmp_path):
        """remove 后 OV 仍存在 → 下轮探测重新登记 enabled=0（同一 conn）。"""
        decision = json.dumps([{"root_uri": OSAKA, "action": "remove",
                                "reason": "gone"}])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        assert daemon._govern_ov_roots(conn) == 1
        assert conn.execute(
            "SELECT 1 FROM ov_roots WHERE root_uri=?", (OSAKA,)
        ).fetchone() is None

        # 第二轮：同一 conn，fs/tree 仍返回该根 → 重新登记 enabled=0
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda prompt, priority="low": json.dumps([
                                {"root_uri": OSAKA, "action": "keep",
                                 "reason": "r"}]))
        n2 = daemon._govern_ov_roots(conn)
        assert n2 == 0
        row = conn.execute(
            "SELECT enabled, origin FROM ov_roots WHERE root_uri=?",
            (OSAKA,)).fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "refine_probe"


# ── T11: filters 免疫 ──

class TestFiltersImmunity:
    def test_llm_filters_field_ignored(self, monkeypatch, tmp_path):
        """LLM 输出含 filters 字段 → 忽略，filters 列不变。"""
        decision = json.dumps([
            {"root_uri": OSAKA, "action": "enable", "reason": "r",
             "filters": {"include": ["hack"]}},
        ])
        _, conn, daemon = _setup_gov(monkeypatch, tmp_path, decision=decision)
        assert daemon._govern_ov_roots(conn) == 1
        row = conn.execute(
            "SELECT filters FROM ov_roots WHERE root_uri=?", (OSAKA,)).fetchone()
        assert json.loads(row[0]) == {}  # 未被写入


# ── T12: R1 约束（成功路径）──

class TestGovernLLMContract:
    def test_call_kwargs_prompt_and_logs(self, monkeypatch, tmp_path, caplog):
        """call_llm_raw kwargs（priority=low + format=json + 1 次）；prompt 含
        字段清单 + D3 裁剪标注；决策日志逐行 JSON（INFO）。"""
        captured = {}
        calls = []

        def _fake_raw(prompt, priority="normal", **kw):
            calls.append(1)
            captured["priority"] = priority
            captured["format"] = kw.get("format")
            captured["num_predict"] = kw.get("num_predict")
            captured["prompt"] = prompt
            return json.dumps([{"root_uri": OSAKA, "action": "enable",
                                "reason": "r"}])

        monkeypatch.setattr(ref, "call_llm_raw", _fake_raw)
        monkeypatch.setattr(ref.Config, "LLM_MODEL", "qwen3-4b-instruct:16k")
        monkeypatch.setattr(ref.Config, "LLM_ENDPOINT", "http://localhost:11435")
        db, conn = _mk_gov_db(tmp_path)
        monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen())
        _gov_graph(tmp_path, monkeypatch)
        daemon = IdleRefinementDaemon()

        with caplog.at_level(logging.INFO, logger="ca.refinement"):
            n = daemon._govern_ov_roots(conn)

        assert n == 1
        assert len(calls) == 1
        assert captured["priority"] == "low"
        assert captured["format"] == "json"
        assert captured["num_predict"] == ref.Config.OV_GOVERN_MAX_TOKENS
        assert "字段清单" in captured["prompt"]
        assert "D3 裁剪" in captured["prompt"]
        assert OSAKA in captured["prompt"]
        # 决策日志：逐行单行 JSON（executed=true）
        logs = [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO
                and "govern decision" in r.getMessage()]
        assert len(logs) == 1
        payload = json.loads(logs[0].split(": ", 1)[1])
        assert payload == {"root_uri": OSAKA, "action": "enable",
                           "reason": "r", "executed": True}

    @pytest.mark.parametrize("model,endpoint,expect_call", [
        ("qwen3-4b-instruct:16k", "http://localhost:11435", True),     # 本地调用
        ("qwen3-4b-instruct:16k", "http://127.0.0.1:11435", True),     # 本地调用
        ("deepseek-v4-flash", "http://localhost:11435", False),        # 云模型 + 本地端点 → 拒
        ("qwen3-4b-instruct:16k", "http://llm.gateway.example:8000", False),  # qwen + 远程端点 → 拒
        ("deepseek-v4-pro", "http://llm.gateway.example:8000", False),  # 云 + 远程 → 拒
    ])
    def test_model_identity_matrix(self, model, endpoint, expect_call,
                                   monkeypatch):
        monkeypatch.setattr(ref.Config, "LLM_MODEL", model)
        monkeypatch.setattr(ref.Config, "LLM_ENDPOINT", endpoint)
        calls = []
        monkeypatch.setattr(ref, "call_llm_raw",
                            lambda prompt, **kw: calls.append(1) or "[]")
        result = ref._govern_llm_decision("test prompt", priority="low")
        if expect_call:
            assert result is not None
            assert len(calls) == 1
        else:
            assert result is None
            assert calls == []


# ── T13: CA_OV_GOVERN=0 ──

class TestGovernDisabled:
    def test_govern_off_registers_only(self, monkeypatch, tmp_path):
        """CA_OV_GOVERN=0 → 不调 LLM、仅探测登记；enabled 快照一致。"""
        monkeypatch.setenv("CA_OV_GOVERN", "0")
        monkeypatch.setattr(ref.Config, "OV_GOVERN", False)
        db, conn = _mk_gov_db(tmp_path)
        monkeypatch.setattr(urllib.request, "urlopen", _GovUrlopen())
        _gov_graph(tmp_path, monkeypatch)
        called = []
        monkeypatch.setattr(ref, "_govern_llm_decision",
                            lambda *a, **k: called.append(1) or "[]")
        daemon = IdleRefinementDaemon()

        before = _rows(conn)
        assert daemon._govern_ov_roots(conn) == 0
        assert called == []  # 不调 LLM

        rows = _rows(conn)
        assert rows[OSAKA] == 0  # 新根登记 enabled=0 照常
        assert rows[TOKYO] == 0
        assert rows[CA] == 1
        # last_seen 更新
        ls = conn.execute(
            "SELECT last_seen FROM ov_roots").fetchall()
        assert all(r[0] is not None for r in ls)
        # enabled 列快照前后一致（新登记行 enabled=0 不影响已有值）
        for uri, enabled in before.items():
            assert rows[uri] == enabled
