"""第三轮 T3：Step 8.5 事实关联建边（信号 B L2 + 信号 C L1）。

覆盖：
  - 信号 B：承接动词候选 → mock 4B → depends_on/continues 边落图（_origin=fact_linking）
  - 信号 B：4B 判定失败 → 跳过该候选不崩（降级）
  - 信号 C：mock OV 检索命中（配置阈值后）→ references_ov 边落图（ov_doc_* 命名）
  - 信号 C：阈值未验证（默认探测模式）→ 不落图
  - 信号 C：OV API 失败 → 降级跳过不崩
  - 无候选 / 无 graph.json → 返回 0 不崩
"""

import json
from pathlib import Path

import pytest

import ca.fact_linking as fl


def _mk_reality(rid, name, hdl="", cs=None):
    return {"reality_id": rid, "name": name, "hdl": hdl,
            "current_status": cs or {"current_state": [], "key_facts": [],
                                     "goals": [], "context": []}}


def _graph(gp, with_ov_docs=True):
    """mock graph.json；默认含 ov_doc 节点（模拟 wiki 子图已建，references_ov 可落图）。"""
    nodes = []
    if with_ov_docs:
        nodes = [
            {"id": "ov_doc_连接池配置优化方案", "label": "[知识] 连接池配置优化方案"},
            {"id": "ov_doc_conn-pool.md", "label": "[知识] conn-pool.md"},
            {"id": "ov_doc_34-idle-refinement.md", "label": "[知识] 34-idle-refinement.md"},
            {"id": "ov_doc_37-reality-restructure.md", "label": "[知识] 37-reality-restructure.md"},
            {"id": "ov_doc_38-reality-graph-inject-merge.md", "label": "[知识] 38-reality-graph-inject-merge.md"},
        ]
    gp.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")


class TestSignalB:
    def test_depends_on_edge_landed(self, tmp_path, monkeypatch):
        """承接动词候选 → 4B 判定 depends_on → 边落图。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "_judge_link_relation", lambda s, t: "depends_on")
        # 信号 C 默认探测模式：mock OV 失败降级，避免真实网络调用
        monkeypatch.setattr(fl, "_ov_search_find", lambda q: None)

        realities = [
            _mk_reality(1, "缓存方案重构", "基于连接池方案的缓存改造"),
            _mk_reality(2, "连接池方案", "连接池上限调优"),
            _mk_reality(3, "压测报告", "输出压测报告"),
        ]
        added = fl.run_fact_linking(None, realities)
        g = json.loads(gp.read_text(encoding="utf-8"))
        rels = {(l["source"], l["target"], l["relation"]) for l in g["links"]}
        assert added >= 1
        # 边方向：含动词方为 source，文本关联方为 target
        assert ("reality_1", "reality_2", "depends_on") in rels
        e = next(l for l in g["links"]
                 if l["relation"] == "depends_on")
        assert e["_origin"] == "fact_linking"
        assert e["confidence_score"] == 0.7

    def test_continues_edge_landed(self, tmp_path, monkeypatch):
        """4B 判定 continues → continues 边落图。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "_judge_link_relation", lambda s, t: "continues")
        monkeypatch.setattr(fl, "_ov_search_find", lambda q: None)

        realities = [
            _mk_reality(1, "第二阶段优化", "延续第一阶段的工作"),
            _mk_reality(2, "第一阶段优化", "完成第一阶段"),
        ]
        added = fl.run_fact_linking(None, realities)
        g = json.loads(gp.read_text(encoding="utf-8"))
        rels = {(l["source"], l["target"], l["relation"]) for l in g["links"]}
        assert ("reality_1", "reality_2", "continues") in rels
        assert added >= 1

    def test_judge_failure_skips_candidate(self, tmp_path, monkeypatch):
        """4B 判定失败（None）→ 跳过候选，不崩。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "_judge_link_relation", lambda s, t: None)
        # 同时把信号 C 关掉（探测不落图）
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", None)
        monkeypatch.setattr(fl, "_ov_search_find", lambda q: None)

        realities = [
            _mk_reality(1, "缓存方案重构", "基于连接池方案的缓存改造"),
            _mk_reality(2, "连接池方案", "连接池上限调优"),
        ]
        added = fl.run_fact_linking(None, realities)
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert added == 0
        assert g["links"] == []

    def test_parse_relation(self):
        """宽松解析：JSON / 围栏 / 裸词。"""
        assert fl._parse_relation('{"relation": "depends_on"}') == "depends_on"
        assert fl._parse_relation('```json\n{"relation": "continues"}\n```') == "continues"
        assert fl._parse_relation("none") == "none"
        assert fl._parse_relation("random text") is None
        assert fl._parse_relation(None) is None

    def test_no_candidates_no_crash(self, tmp_path, monkeypatch):
        """无承接动词候选 → 返回 0 不崩。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", None)
        monkeypatch.setattr(fl, "_ov_search_find", lambda q: None)

        realities = [_mk_reality(1, "独立任务", "无关联内容")]
        assert fl.run_fact_linking(None, realities) == 0


class TestSignalC:
    def test_references_ov_edge_landed(self, tmp_path, monkeypatch):
        """配置阈值 + mock OV 命中 → references_ov 边落图（ov_doc_* 命名）。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.6)
        hit = {"title": "连接池配置优化方案", "score": 0.9, "uri": "/design/conn-pool.md"}
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [hit])

        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        added = fl.run_fact_linking(None, realities)
        g = json.loads(gp.read_text(encoding="utf-8"))
        refs = [l for l in g["links"] if l["relation"] == "references_ov"]
        assert added == 1
        assert len(refs) == 1
        assert refs[0]["source"] == "reality_1"
        assert refs[0]["target"] == "ov_doc_连接池配置优化方案"
        assert refs[0]["_origin"] == "fact_linking"

    def test_below_threshold_no_edge(self, tmp_path, monkeypatch):
        """命中相似度低于阈值 → 不落图。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.8)
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"title": "无关文档", "score": 0.3}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0

    def test_probe_mode_no_edge(self, tmp_path, monkeypatch):
        """阈值未验证（默认探测模式）→ 命中也不落图。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", None)
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"title": "连接池配置优化方案", "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_ov_api_failure_degrades(self, tmp_path, monkeypatch):
        """OV API 失败（None）→ 降级跳过不崩。"""
        gp = tmp_path / "graph.json"
        _graph(gp)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.6)
        monkeypatch.setattr(fl, "_ov_search_find", lambda q: None)
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_no_graph_file_skips(self, tmp_path, monkeypatch):
        """graph.json 不存在 → 跳过不崩。"""
        gp = tmp_path / "missing.json"
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.6)
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"title": "文档", "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0

    def test_extract_ov_hits_result_nested(self):
        """OV search/find 真实响应（result.memories/resources 嵌套）→ 解析出 resources。"""
        data = {"status": "ok", "result": {
            "memories": [{"uri": "viking://user/tester/memories/x.md", "score": 0.9}],
            "resources": [{"uri": "viking://resources/projects/context-assembler/decisions/34-idle-refinement/34-idle-refinement.md/背景.md", "score": 0.7}],
            "skills": [],
            "total": 2,
        }}
        hits = fl._extract_ov_hits(data)
        assert len(hits) == 1
        assert hits[0]["uri"].startswith("viking://resources/")

    def test_extract_ov_hits_top_level_fallback(self):
        """非 result 嵌套的旧形态响应 → 顶层 list 键兜底。"""
        assert fl._extract_ov_hits({"hits": [{"uri": "x", "score": 0.5}]})[0]["uri"] == "x"
        assert fl._extract_ov_hits([]) == []
        assert fl._extract_ov_hits(None) is None

    def test_ov_doc_nid_extracts_main_doc(self):
        """uri 指向文档碎片/隐藏摘要 → 提取第一个非隐藏 .md 主文档段。"""
        # 碎片：34 决策目录下背景.md → 主文档 34-idle-refinement.md
        assert fl._ov_doc_nid({"uri": "viking://resources/projects/context-assembler/decisions/34-idle-refinement/34-idle-refinement.md/背景.md"}) == "ov_doc_34-idle-refinement.md"
        # 碎片：37 决策嵌套 → 主文档 37-reality-restructure.md
        assert fl._ov_doc_nid({"uri": "viking://resources/projects/context-assembler/decisions/37-reality-restructure.md/决策_37CA_Reali"}) == "ov_doc_37-reality-restructure.md"
        # title 形态直接给 → 原逻辑
        assert fl._ov_doc_nid({"title": "连接池配置优化方案", "score": 0.9}) == "ov_doc_连接池配置优化方案"

    def test_ov_doc_nid_hidden_abstract_returns_none(self):
        """uri 无主文档段（仅隐藏摘要）→ None（防悬挂）。"""
        assert fl._ov_doc_nid({"uri": "viking://resources/projects/context-assembler/decisions/34-idle-refinement/.overview.md"}) is None
        assert fl._ov_doc_nid({"uri": "viking://user/x/.abstract.md"}) is None

    def test_hit_not_in_graph_skipped(self, tmp_path, monkeypatch):
        """命中文档未入图（known_ov 无此节点）→ 不建悬挂边。"""
        gp = tmp_path / "graph.json"
        _graph(gp, with_ov_docs=True)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.6)
        # 命中未入图文档（ov_doc_未入图文档 不在 known_ov）
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"uri": "viking://resources/xxx/未入图文档.md", "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_no_ov_docs_in_graph_skips_all(self, tmp_path, monkeypatch):
        """图内无 ov_doc 节点（wiki 子图未建）→ references_ov 全部跳过不落图。"""
        gp = tmp_path / "graph.json"
        _graph(gp, with_ov_docs=False)
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.6)
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"uri": "viking://resources/xxx/doc.md", "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []


class TestProfileFilter:
    """其它 profile 资源过滤（2026-08-07 用户约束：允许非 CA 项目，禁止其它 profile）。"""

    def test_foreign_profiles_removed(self, monkeypatch):
        """user/topics/resources 下的其它 profile 命中 → 剔除。"""
        monkeypatch.setattr(fl, "_PROFILES_CACHE", frozenset({"tester", "winker", "sysadmin", "pmgr"}))
        hits = [
            {"uri": "viking://user/winker/memories/x.md", "score": 0.9},
            {"uri": "viking://topics/sysadmin/t1/.abstract.md", "score": 0.8},
            {"uri": "viking://resources/winker/ca_topics/w/mrx/.overview.md", "score": 0.7},
        ]
        assert fl.filter_foreign_profiles(hits) == []

    def test_own_profile_and_shared_kept(self, monkeypatch):
        """本 profile + 无 profile 段（共享项目/非 CA 项目）→ 保留。"""
        monkeypatch.setattr(fl, "_PROFILES_CACHE", frozenset({"tester", "winker", "sysadmin", "pmgr"}))
        monkeypatch.setattr(fl, "current_profile", lambda: "tester")
        hits = [
            {"uri": "viking://user/tester/memories/x.md", "score": 0.9},
            {"uri": "viking://resources/projects/context-assembler/decisions/34-idle-refinement/34-idle-refinement.md/背景.md", "score": 0.8},
            {"uri": "viking://resources/projects/windows/comfyui/docs/wiki/README.md", "score": 0.7},
            {"uri": "viking://resources/TP-001/TP-001.md", "score": 0.6},
        ]
        out = fl.filter_foreign_profiles(hits)
        assert len(out) == 4

    def test_known_profiles_skips_non_profile_dirs(self, tmp_path, monkeypatch):
        """~/.hermes/profiles/ 下非 profile 目录（.git/ca_cache/scripts）不混入名单。"""
        fake_home = tmp_path
        fake_root = fake_home / ".hermes" / "profiles"
        for name in ("tester", "winker", ".git", "ca_cache", "scripts"):
            d = fake_root / name
            d.mkdir(parents=True)
            (d / "auth.json").write_text("{}")
        # 非 profile 目录删掉 auth.json（模拟真实结构：.git/ca_cache 无 auth.json）
        for name in (".git", "ca_cache", "scripts"):
            (fake_root / name / "auth.json").unlink()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        assert fl._known_profiles() == {"tester", "winker"}

    def test_ov_search_find_filters(self, monkeypatch):
        """_ov_search_find 结果经 profile 过滤（其它 profile 命中不返回）。"""
        monkeypatch.setattr(fl, "_PROFILES_CACHE", frozenset({"tester", "winker", "sysadmin", "pmgr"}))
        monkeypatch.setattr(fl, "current_profile", lambda: "tester")
        # mock 网络层：返回含其它 profile 命中的响应
        import types
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({"status": "ok", "result": {
                    "resources": [
                        {"uri": "viking://user/winker/memories/x.md", "score": 0.9},
                        {"uri": "viking://resources/projects/context-assembler/decisions/34-idle-refinement/34-idle-refinement.md/背景.md", "score": 0.8},
                    ], "memories": [], "skills": []}}).encode()
        fake_urlopen = lambda req, timeout=None: FakeResp()
        # _ov_search_find 内部 import urllib.request → 需 patch 模块
        import urllib.request as _ur
        monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
        monkeypatch.setattr(fl, "OV_API", "http://127.0.0.1:1")
        monkeypatch.setattr(fl, "OV_SEARCH_TIMEOUT", 1)
        monkeypatch.setattr(fl, "OV_SEARCH_PATH", "/x")
        hits = fl._ov_search_find("test")
        assert len(hits) == 1
        assert hits[0]["uri"].startswith("viking://resources/projects/")


class TestSignalCLayered:
    """信号 C 分层（2026-08-08）：P0 导航排除 + P1 先代码后 4B + P2 top-K。"""

    @staticmethod
    def _graph_with(gp, ov_ids):
        nodes = [{"id": nid, "label": f"[知识] {nid}"} for nid in ov_ids]
        gp.write_text(json.dumps({"nodes": nodes, "links": []}),
                      encoding="utf-8")

    def test_top1_auto_layer_no_jaccard_no_4b(self, tmp_path, monkeypatch):
        """top-1 score ≥ 0.60 → 自动落图，零 token（不调 jaccard/4B）。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_a.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/a.md",
                        "score": 0.9}])
        calls = []
        monkeypatch.setattr(
            fl, "_doc_title_jaccard",
            lambda rt, hit: calls.append("jaccard") or 0.0)
        monkeypatch.setattr(
            fl, "_judge_ov_reference_4b",
            lambda r, hit: calls.append("4b") or "none")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1
        assert calls == []

    def test_top1_boundary_jaccard_above_min_lands(self, tmp_path, monkeypatch):
        """0.55 ≤ score < 0.60 且 jaccard ≥ 0.05 → 词面放行落图（不上 4B）。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_b.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/b.md",
                        "score": 0.58}])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.3)
        calls = []
        monkeypatch.setattr(
            fl, "_judge_ov_reference_4b",
            lambda r, hit: calls.append("4b") or "none")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1
        assert calls == []

    def test_top1_boundary_jaccard_low_4b_references_lands(
            self, tmp_path, monkeypatch):
        """0.55-0.60 且 jaccard < 0.05 → 4B 判 references → 落图。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_b.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/b.md",
                        "score": 0.58}])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.0)
        monkeypatch.setattr(fl, "_judge_ov_reference_4b",
                            lambda r, hit: "references")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1
        g = json.loads(gp.read_text(encoding="utf-8"))
        refs = [l for l in g["links"] if l["relation"] == "references_ov"]
        assert len(refs) == 1
        assert refs[0]["target"] == "ov_doc_b.md"

    def test_top1_boundary_jaccard_low_4b_none_skips(
            self, tmp_path, monkeypatch):
        """0.55-0.60 且 jaccard < 0.05 → 4B 判 none → 跳过不落图。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_b.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/b.md",
                        "score": 0.58}])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.0)
        monkeypatch.setattr(fl, "_judge_ov_reference_4b",
                            lambda r, hit: "none")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_top1_boundary_4b_failure_skips(self, tmp_path, monkeypatch):
        """边界区 4B 解析失败（None）→ 跳过候选不崩。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_b.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/b.md",
                        "score": 0.58}])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.0)
        monkeypatch.setattr(fl, "_judge_ov_reference_4b",
                            lambda r, hit: None)
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_below_floor_no_edge_no_4b(self, tmp_path, monkeypatch):
        """score < 0.55 → 不落图，且不调 jaccard/4B。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_a.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/x/a.md",
                        "score": 0.50}])
        calls = []
        monkeypatch.setattr(
            fl, "_doc_title_jaccard",
            lambda rt, hit: calls.append("jaccard") or 0.0)
        monkeypatch.setattr(
            fl, "_judge_ov_reference_4b",
            lambda r, hit: calls.append("4b") or "none")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        assert calls == []

    def test_topk_multiple_hits_multiple_edges(self, tmp_path, monkeypatch):
        """同 reality 多命中（score 全 ≥ 0.60）→ 每个不同 target 建边。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_a.md", "ov_doc_b.md", "ov_doc_c.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_TOP_K_MIN_SCORE", 0.60)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [
                {"uri": "viking://resources/projects/x/a.md", "score": 0.9},
                {"uri": "viking://resources/projects/x/b.md", "score": 0.85},
                {"uri": "viking://resources/projects/x/c.md", "score": 0.8},
            ])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 3
        g = json.loads(gp.read_text(encoding="utf-8"))
        targets = {l["target"] for l in g["links"]
                   if l["relation"] == "references_ov"}
        assert targets == {"ov_doc_a.md", "ov_doc_b.md", "ov_doc_c.md"}

    def test_topk_same_nid_dedup(self, tmp_path, monkeypatch):
        """碎片与主文档同 nid → 只建 1 条边。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_34-idle-refinement.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [
                {"uri": "viking://resources/projects/context-assembler/decisions/"
                        "34-idle-refinement/34-idle-refinement.md/背景.md",
                 "score": 0.9},
                {"uri": "viking://resources/projects/context-assembler/decisions/"
                        "34-idle-refinement/34-idle-refinement.md/.overview.md",
                 "score": 0.88},
            ])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1
        g = json.loads(gp.read_text(encoding="utf-8"))
        refs = [l for l in g["links"] if l["relation"] == "references_ov"]
        assert len(refs) == 1
        assert refs[0]["target"] == "ov_doc_34-idle-refinement.md"

    def test_topk_low_rank2_3_skip_4b_count_limited(self, tmp_path, monkeypatch):
        """top-2/3 score < 0.60 → 跳过不上 4B；4B 仅 top-1 边界区调用 1 次。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_a.md", "ov_doc_b.md", "ov_doc_c.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "OV_JACCARD_MIN", 0.05)
        monkeypatch.setattr(fl, "OV_TOP_K_MIN_SCORE", 0.60)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [
                {"uri": "viking://resources/projects/x/a.md", "score": 0.58},
                {"uri": "viking://resources/projects/x/b.md", "score": 0.57},
                {"uri": "viking://resources/projects/x/c.md", "score": 0.56},
            ])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.0)
        calls = []

        def fake_4b(reality, hit):
            calls.append(hit["uri"])
            return "references"

        monkeypatch.setattr(fl, "_judge_ov_reference_4b", fake_4b)
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1
        assert len(calls) == 1
        assert calls == ["viking://resources/projects/x/a.md"]
        g = json.loads(gp.read_text(encoding="utf-8"))
        refs = [l for l in g["links"] if l["relation"] == "references_ov"]
        assert [l["target"] for l in refs] == ["ov_doc_a.md"]

    def test_topk_constant_limits_hits(self, tmp_path, monkeypatch):
        """OV_TOP_K=2 → 只遍历前 2 个命中（第 3 个不处理）。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_a.md", "ov_doc_b.md", "ov_doc_c.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_TOP_K", 2)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [
                {"uri": "viking://resources/projects/x/a.md", "score": 0.9},
                {"uri": "viking://resources/projects/x/b.md", "score": 0.85},
                {"uri": "viking://resources/projects/x/c.md", "score": 0.8},
            ])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 2
        g = json.loads(gp.read_text(encoding="utf-8"))
        targets = {l["target"] for l in g["links"]
                   if l["relation"] == "references_ov"}
        assert targets == {"ov_doc_a.md", "ov_doc_b.md"}

    def test_navigation_doc_excluded_even_high_score(self, tmp_path,
                                                     monkeypatch):
        """INDEX.md 命中 score 0.9（图内已有节点）→ 仍排除（P0 泛文档）。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_index.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/"
                               "context-assembler/INDEX.md",
                        "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_tail_not_exact_kept(self, tmp_path, monkeypatch):
        """01-overview.md（尾段 01-overview）→ 精确匹配不误伤，正常落图。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_01-overview.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/"
                               "context-assembler/architecture/01-overview.md",
                        "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1

    def test_hidden_overview_fragment_excluded_by_nid(self, tmp_path,
                                                      monkeypatch):
        """缺陷场景：uri 尾段 .overview.md（nid 解析为 ov_doc_overview.md）
        → 导航排除用 tgt 命中，不落图（2026-08-08 修复）。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_overview.md"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(
            fl, "_ov_search_find",
            lambda q: [{"uri": "viking://resources/projects/context-assembler/"
                               "overview.md/.overview.md",
                        "score": 0.9}])
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 0
        g = json.loads(gp.read_text(encoding="utf-8"))
        assert g["links"] == []

    def test_title_form_hit_boundary_uses_title(self, tmp_path, monkeypatch):
        """title 形态命中（无 uri）→ 导航判定用 title；边界区走 4B。"""
        gp = tmp_path / "graph.json"
        self._graph_with(gp, ["ov_doc_连接池配置优化方案"])
        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "OV_AUTO_THRESHOLD", 0.60)
        monkeypatch.setattr(fl, "_ov_search_find",
                            lambda q: [{"title": "连接池配置优化方案",
                                        "score": 0.58}])
        monkeypatch.setattr(fl, "_doc_title_jaccard", lambda rt, hit: 0.0)
        monkeypatch.setattr(fl, "_judge_ov_reference_4b",
                            lambda r, hit: "references")
        realities = [_mk_reality(1, "连接池优化", "连接池上限调优")]
        assert fl.run_fact_linking(None, realities) == 1


class TestIsNavigationDoc:
    """P0 导航排除精确性：尾段集合精确匹配（8 组 + title 形态）。"""

    def test_navigation_uris_excluded(self):
        nav = [
            "viking://resources/projects/x/INDEX.md",
            "viking://resources/projects/x/docs/README.md",
            "viking://resources/projects/x/changelog.md",
            "viking://resources/projects/x/overview.md",
        ]
        for uri in nav:
            assert fl._is_navigation_doc({"uri": uri}), uri

    def test_non_navigation_uris_kept(self):
        kept = [
            "viking://resources/projects/x/01-overview.md",
            "viking://resources/projects/x/C-INDEX.md",
            "viking://resources/projects/x/H-INDEX.md",
            "viking://resources/projects/x/AGENTS.md",
        ]
        for uri in kept:
            assert not fl._is_navigation_doc({"uri": uri}), uri

    def test_title_form_fallback(self):
        assert fl._is_navigation_doc({"title": "INDEX.md"})
        assert fl._is_navigation_doc({"title": "readme"})
        assert not fl._is_navigation_doc({"title": "连接池配置优化方案"})
        assert not fl._is_navigation_doc({"title": "01-overview.md"})
        assert not fl._is_navigation_doc({})
        assert not fl._is_navigation_doc({"uri": ""})


class TestDocTitleJaccard:
    """词面 Jaccard：frontmatter → H1 → 文件名 fallback + 缓存 + 退化。"""

    def test_frontmatter_title(self, monkeypatch):
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE", {})
        monkeypatch.setattr(
            fl, "_fetch_ov_raw",
            lambda uri: "---\ntitle: \"连接池配置优化方案\"\n---\n# 正文\n")
        hit = {"uri": "viking://resources/projects/x/conn-pool.md",
               "score": 0.58}
        # reality "连接池优化" ∩ title "连接池配置优化方案" = 3 / 并集 9
        assert fl._doc_title_jaccard("连接池优化", hit) == pytest.approx(3 / 9)

    def test_h1_fallback(self, monkeypatch):
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE", {})
        monkeypatch.setattr(fl, "_fetch_ov_raw",
                            lambda uri: "# 工具摘要引擎设计\n\n正文")
        hit = {"uri": "viking://resources/projects/x/t.md", "score": 0.58}
        assert fl._doc_title_jaccard("工具摘要引擎", hit) > 0

    def test_filename_fallback(self, monkeypatch):
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE", {})
        monkeypatch.setattr(fl, "_fetch_ov_raw",
                            lambda uri: "无 frontmatter 无 H1")
        hit = {"uri": "viking://resources/projects/x/conn-pool.md",
               "score": 0.58}
        # title = "conn-pool.md" → 英文 token conn/pool/md
        assert fl._doc_title_jaccard("conn pool config", hit) > 0

    def test_cache_prevents_refetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE", {})
        monkeypatch.setattr(
            fl, "_fetch_ov_raw",
            lambda uri: calls.append(uri) or "---\ntitle: 连接池配置优化方案\n---\n")
        hit = {"uri": "viking://resources/projects/x/a.md"}
        fl._doc_title_jaccard("连接池优化", hit)
        fl._doc_title_jaccard("连接池优化", hit)
        assert len(calls) == 1

    def test_fetch_failure_returns_zero(self, monkeypatch):
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE", {})
        monkeypatch.setattr(fl, "_fetch_ov_raw", lambda uri: "")
        hit = {"uri": "viking://resources/projects/x/a.md"}
        assert fl._doc_title_jaccard("连接池优化", hit) == 0.0

    def test_no_uri_returns_zero(self):
        hit = {"title": "连接池配置优化方案"}
        assert fl._doc_title_jaccard("连接池优化", hit) == 0.0


class TestOverseerJudge4B:
    """边界区 4B 复核：verdict 解析 / 重试一次 / 失败跳过 / 调用参数。"""

    @staticmethod
    def _reality():
        return _mk_reality(1, "连接池优化", "连接池上限调优")

    @staticmethod
    def _hit():
        return {"uri": "viking://resources/projects/x/a.md",
                "title": "连接池配置优化方案", "score": 0.58}

    def test_verdict_references(self, monkeypatch):
        import ca.topic_summary as ts
        monkeypatch.setattr(ts, "call_llm_raw",
                            lambda prompt, **kw: '{"verdict": "references"}')
        assert fl._judge_ov_reference_4b(self._reality(), self._hit()) \
            == "references"

    def test_parse_fail_retry_once_then_verdict(self, monkeypatch):
        import ca.topic_summary as ts
        responses = iter(["not json", '{"verdict": "none"}'])
        monkeypatch.setattr(ts, "call_llm_raw",
                            lambda prompt, **kw: next(responses))
        assert fl._judge_ov_reference_4b(self._reality(), self._hit()) == "none"

    def test_parse_fail_twice_returns_none(self, monkeypatch):
        import ca.topic_summary as ts
        monkeypatch.setattr(ts, "call_llm_raw",
                            lambda prompt, **kw: "garbage")
        assert fl._judge_ov_reference_4b(self._reality(), self._hit()) is None

    def test_call_exception_returns_none(self, monkeypatch):
        import ca.topic_summary as ts

        def boom(prompt, **kw):
            raise RuntimeError("llm down")

        monkeypatch.setattr(ts, "call_llm_raw", boom)
        assert fl._judge_ov_reference_4b(self._reality(), self._hit()) is None

    def test_call_params(self, monkeypatch):
        import ca.topic_summary as ts
        seen = {}

        def fake_llm(prompt, **kw):
            seen.update(kw)
            return '{"verdict": "references"}'

        monkeypatch.setattr(ts, "call_llm_raw", fake_llm)
        fl._judge_ov_reference_4b(self._reality(), self._hit())
        assert seen["priority"] == "low"
        assert seen["temperature"] == 0.2
        assert seen["max_retries"] == 1

    def test_prompt_contains_reality_and_doc(self, monkeypatch):
        monkeypatch.setattr(fl, "_OV_TITLE_CACHE",
                            {"viking://resources/projects/x/a.md": "连接池配置优化方案"})
        prompt = fl.build_ov_reference_judge_prompt(self._reality(),
                                                    self._hit())
        assert "连接池优化" in prompt
        assert "连接池上限调优" in prompt
        assert "连接池配置优化方案" in prompt
        assert "viking://resources/projects/x/a.md" in prompt

    def test_parse_ov_verdict(self):
        assert fl._parse_ov_verdict('{"verdict": "references"}') == "references"
        assert fl._parse_ov_verdict('```json\n{"verdict": "none"}\n```') == "none"
        assert fl._parse_ov_verdict("none") == "none"
        assert fl._parse_ov_verdict("references") == "references"
        assert fl._parse_ov_verdict("random") is None
        assert fl._parse_ov_verdict('{"relation": "none"}') is None
        assert fl._parse_ov_verdict(None) is None
        assert fl._parse_ov_verdict("") is None
