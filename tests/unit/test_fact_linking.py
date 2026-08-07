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
