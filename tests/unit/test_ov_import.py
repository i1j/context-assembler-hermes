"""2026-08-08：OV 资源全量入图 + 命名统一（references_ov 边打通）。

覆盖（任务书 docs/fix-task-20260808-ov-link.md §3/§4）：
  - _load_all_ov_docs：fs/tree 响应解析（过滤目录/非 md、碎片/隐藏摘要去重、
    主文档 uri 保留、网络失败 → 空列表不抛异常）
  - build_wiki_subgraph：无条件建全量 ov_doc 节点（URI 文件段规则命名，
    file_type=knowledge、_origin=ov_import、community=0、metadata.uri 完整）
  - 命名统一：入图 nid 与 ca/fact_linking._ov_doc_nid 逐字节一致；同一文档
    title 命名旧节点不再生成，trace 边只指向 URI 命名节点
  - _clean_knowledge_domain：旧 title 命名 ov_doc 节点清理（含连带边）；
    fs/tree 不可用 → 保守跳过
  - 幂等：重复 build/merge 不产生重复节点，无悬挂边
"""

import json
import urllib.request

import ca.fact_linking as fl
import scripts.wiki_to_graph as w2g

from ca.store import _get_topic_conn, create_reality  # noqa: E402


def _tree_payload():
    """mock fs/tree 响应：290 条目/231 md 的真实形态（含碎片/隐藏摘要/目录/非 md）。

    文档数 = 7：INDEX.md / 01-overview.md / 15-fingerprint-dedup.md /
    37-reality-restructure.md / 38-reality-graph-inject-merge.md /
    topic-summarization-decision.md / ca-ov-topic-submit.md
    """
    return {"status": "ok", "result": {"entries": [
        {"uri": "viking://resources/projects/context-assembler/decisions",
         "rel_path": "decisions", "isDir": True},
        {"uri": "viking://resources/projects/context-assembler/README.txt",
         "rel_path": "README.txt", "isDir": False},
        # 碎片先于主文档出现 → 仍归并到主文档（最短 rel_path 保留）
        {"uri": "viking://resources/projects/context-assembler/decisions/15-fingerprint-dedup/15-fingerprint-dedup.md/背景.md",
         "rel_path": "decisions/15-fingerprint-dedup/15-fingerprint-dedup.md/背景.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/15-fingerprint-dedup/15-fingerprint-dedup.md/.overview.md",
         "rel_path": "decisions/15-fingerprint-dedup/15-fingerprint-dedup.md/.overview.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/15-fingerprint-dedup/15-fingerprint-dedup.md",
         "rel_path": "decisions/15-fingerprint-dedup/15-fingerprint-dedup.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/INDEX.md",
         "rel_path": "INDEX.md", "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/architecture/01-overview/01-overview.md",
         "rel_path": "architecture/01-overview/01-overview.md", "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/architecture/ca-ov-topic-submit.md",
         "rel_path": "architecture/ca-ov-topic-submit.md", "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/37-reality-restructure.md/37-reality-restructure.md",
         "rel_path": "decisions/37-reality-restructure.md/37-reality-restructure.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/37-reality-restructure.md/决策_37CA_Reali.md",
         "rel_path": "decisions/37-reality-restructure.md/决策_37CA_Reali.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/38-reality-graph-inject-merge.md/38-reality-graph-inject-merge.md",
         "rel_path": "decisions/38-reality-graph-inject-merge.md/38-reality-graph-inject-merge.md",
         "isDir": False},
        {"uri": "viking://resources/projects/context-assembler/decisions/topic-summarization-decision.md/话题摘要化设计_v3_取代_OV_VLM_摘要.md",
         "rel_path": "decisions/topic-summarization-decision.md/话题摘要化设计_v3_取代_OV_VLM_摘要.md",
         "isDir": False},
    ]}}


def _fixture_docs():
    """_load_all_ov_docs 的解析结果（供 build/cleanup 直接 mock）。"""
    return [
        {"uri": "viking://resources/projects/context-assembler/INDEX.md",
         "rel_path": "INDEX.md", "nid": "ov_doc_index.md"},
        {"uri": "viking://resources/projects/context-assembler/architecture/01-overview/01-overview.md",
         "rel_path": "architecture/01-overview/01-overview.md",
         "nid": "ov_doc_01-overview.md"},
        {"uri": "viking://resources/projects/context-assembler/architecture/ca-ov-topic-submit.md",
         "rel_path": "architecture/ca-ov-topic-submit.md",
         "nid": "ov_doc_ca-ov-topic-submit.md"},
        {"uri": "viking://resources/projects/context-assembler/decisions/15-fingerprint-dedup/15-fingerprint-dedup.md",
         "rel_path": "decisions/15-fingerprint-dedup/15-fingerprint-dedup.md",
         "nid": "ov_doc_15-fingerprint-dedup.md"},
        {"uri": "viking://resources/projects/context-assembler/decisions/37-reality-restructure.md/37-reality-restructure.md",
         "rel_path": "decisions/37-reality-restructure.md/37-reality-restructure.md",
         "nid": "ov_doc_37-reality-restructure.md"},
        {"uri": "viking://resources/projects/context-assembler/decisions/38-reality-graph-inject-merge.md/38-reality-graph-inject-merge.md",
         "rel_path": "decisions/38-reality-graph-inject-merge.md/38-reality-graph-inject-merge.md",
         "nid": "ov_doc_38-reality-graph-inject-merge.md"},
        {"uri": "viking://resources/projects/context-assembler/decisions/topic-summarization-decision.md/话题摘要化设计_v3_取代_OV_VLM_摘要.md",
         "rel_path": "decisions/topic-summarization-decision.md/话题摘要化设计_v3_取代_OV_VLM_摘要.md",
         "nid": "ov_doc_topic-summarization-decision.md"},
    ]


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _FakeUrlopen:
    """fs/tree 返回 mock 载荷；webdav（TRACE_SOURCES 抓取）拒绝 → 降级。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/v1/fs/tree" in url:
            return _FakeResp(json.dumps(self._payload).encode())
        raise ConnectionRefusedError("webdav not mocked in test")


def _ok_entries(entries):
    return {"status": "ok", "result": {"entries": entries}}


def _multi_tree():
    """多根 fs/tree 载荷（决策 44）：按 uri 精确分发。

    windows 根含 8 个 references_ov 目标文档（含 code/ 目录与文档碎片，
    rel_path 带根段 —— 实测 OV 形态）；irobot 根含与 CA 冲突的 INDEX.md。
    """
    ca = "viking://resources/projects/context-assembler"
    win = "viking://resources/projects/windows"
    robot = "viking://resources/projects/irobot"
    return {
        # E-1（决策 44 续）：projects 根级载荷键 —— _ov_projects_roots() 依赖；
        # 缺此键治理决策路径永不执行（T2/T8/T10/T12 必失败）。
        "viking://resources/projects": _ok_entries([
            {"uri": ca, "rel_path": "context-assembler", "isDir": True},
            {"uri": win, "rel_path": "windows", "isDir": True},
            {"uri": robot, "rel_path": "irobot", "isDir": True},
            {"uri": "viking://resources/projects/osaka",
             "rel_path": "osaka", "isDir": True},
            {"uri": "viking://resources/projects/tokyo",
             "rel_path": "tokyo", "isDir": True},
            {"uri": "viking://resources/projects/kyoto",
             "rel_path": "kyoto", "isDir": True},
        ]),
        ca: _tree_payload(),
        win: _ok_entries([
            {"uri": f"{win}/comfyui-model-setup",
             "rel_path": "windows/comfyui-model-setup", "isDir": True},
            {"uri": f"{win}/show-me-the-story",
             "rel_path": "windows/show-me-the-story", "isDir": True},
            {"uri": f"{win}/comfyui-vram-free",
             "rel_path": "windows/comfyui-vram-free", "isDir": True},
            {"uri": f"{win}/ops", "rel_path": "windows/ops", "isDir": True},
        ]),
        f"{win}/comfyui-model-setup": _ok_entries([
            {"uri": f"{win}/comfyui-model-setup/decisions",
             "rel_path": "windows/comfyui-model-setup/decisions",
             "isDir": True},
            {"uri": f"{win}/comfyui-model-setup/code",
             "rel_path": "windows/comfyui-model-setup/code", "isDir": True},
        ]),
        f"{win}/comfyui-model-setup/decisions": _ok_entries([
            {"uri": f"{win}/comfyui-model-setup/decisions/03-electron-crash-browser.md",
             "rel_path": "windows/comfyui-model-setup/decisions/03-electron-crash-browser.md",
             "isDir": False},
            # 碎片 → 与主文档同 nid，去重保留主文档 uri
            {"uri": f"{win}/comfyui-model-setup/decisions/03-electron-crash-browser.md/背景.md",
             "rel_path": "windows/comfyui-model-setup/decisions/03-electron-crash-browser.md/背景.md",
             "isDir": False},
        ]),
        f"{win}/comfyui-model-setup/code": _ok_entries([
            {"uri": f"{win}/comfyui-model-setup/code/notes.md",
             "rel_path": "windows/comfyui-model-setup/code/notes.md",
             "isDir": False},
            # B1/B2 回归（2026-08-09）：实测深层 fs/tree 返回 rel_path 相对
            # 被查询 uri（丢全部前缀）→ 必须由完整 uri 补全，exclude /code/ 仍命中
            {"uri": f"{win}/comfyui-model-setup/code/commands/commands.md",
             "rel_path": "commands/commands.md", "isDir": False},
        ]),
        f"{win}/show-me-the-story": _ok_entries([
            {"uri": f"{win}/show-me-the-story/decisions",
             "rel_path": "windows/show-me-the-story/decisions",
             "isDir": True},
        ]),
        f"{win}/show-me-the-story/decisions": _ok_entries([
            {"uri": f"{win}/show-me-the-story/decisions/01-windows-ollama-backend.md",
             "rel_path": "windows/show-me-the-story/decisions/01-windows-ollama-backend.md",
             "isDir": False},
            {"uri": f"{win}/show-me-the-story/decisions/02-timeout-context.md",
             "rel_path": "windows/show-me-the-story/decisions/02-timeout-context.md",
             "isDir": False},
        ]),
        f"{win}/comfyui-vram-free": _ok_entries([
            {"uri": f"{win}/comfyui-vram-free/decisions",
             "rel_path": "windows/comfyui-vram-free/decisions",
             "isDir": True},
        ]),
        f"{win}/comfyui-vram-free/decisions": _ok_entries([
            {"uri": f"{win}/comfyui-vram-free/decisions/01-vram-usage.md",
             "rel_path": "windows/comfyui-vram-free/decisions/01-vram-usage.md",
             "isDir": False},
        ]),
        f"{win}/ops": _ok_entries([
            {"uri": f"{win}/ops/openviking",
             "rel_path": "windows/ops/openviking", "isDir": True},
            {"uri": f"{win}/ops/graphify",
             "rel_path": "windows/ops/graphify", "isDir": True},
        ]),
        f"{win}/ops/openviking": _ok_entries([
            {"uri": f"{win}/ops/openviking/reindex-embedding-rebuild.md",
             "rel_path": "windows/ops/openviking/reindex-embedding-rebuild.md",
             "isDir": False},
            {"uri": f"{win}/ops/openviking/components",
             "rel_path": "windows/ops/openviking/components", "isDir": True},
        ]),
        f"{win}/ops/openviking/components": _ok_entries([
            {"uri": f"{win}/ops/openviking/components/mcp-tools.md",
             "rel_path": "windows/ops/openviking/components/mcp-tools.md",
             "isDir": False},
        ]),
        f"{win}/ops/graphify": _ok_entries([
            {"uri": f"{win}/ops/graphify/cluster-migration.md",
             "rel_path": "windows/ops/graphify/cluster-migration.md",
             "isDir": False},
        ]),
        robot: _ok_entries([
            # 与 CA INDEX.md 同 nid → 跨根同名保留 context-assembler 优先
            {"uri": f"{robot}/INDEX.md", "rel_path": "irobot/INDEX.md",
             "isDir": False},
            {"uri": f"{robot}/decisions/shared.md",
             "rel_path": "irobot/decisions/shared.md", "isDir": False},
            {"uri": f"{robot}/slam-nav.md", "rel_path": "irobot/slam-nav.md",
             "isDir": False},
        ]),
    }


class _FakeUrlopenMulti:
    """多根 fs/tree mock：按 uri 精确分发；未知 uri / webdav → 拒绝（降级跳过）。"""

    def __init__(self, tree: dict):
        self._tree = tree

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/v1/fs/tree" in url:
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            uri = (qs.get("uri") or [""])[0]
            payload = self._tree.get(uri)
            if payload is None:
                raise ConnectionRefusedError(f"unknown tree uri {uri}")
            return _FakeResp(json.dumps(payload).encode())
        raise ConnectionRefusedError("webdav not mocked in test")


def _patch_multi_tree(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _FakeUrlopenMulti(_multi_tree()))


def _seed_ov_roots_db(tmp_path, monkeypatch):
    """建 tmp DB（store schema + ov_roots 种子）+ 挂多根 fs/tree mock。"""
    db = tmp_path / "ca_topics.db"
    _get_topic_conn(db)
    monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
    _patch_multi_tree(monkeypatch)
    return db


def _patch_tree(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _FakeUrlopen(_tree_payload()))


def _mk_reality(db, name="连接池优化"):
    return create_reality(
        profile="tester", name=name, hdl="完成参数优化并验证",
        current_status={"current_state": ["连接池上限调至 200"],
                        "key_facts": ["连接池耗尽导致超时"],
                        "goals": ["压测报告待输出"], "context": []},
        timeline_entry={"seq": 1, "topic_id": 1, "turns": [7, 8],
                        "session_id": "s1", "overview": "完成参数优化"},
        source_strand={"session_id": "s1", "strand_id": 10},
        centroid_json="[0.1, 0.2]",
        db_path=db,
    )


class TestLoadAllOvDocs:
    """_load_all_ov_docs：fs/tree 全量清单解析。"""

    def test_parses_tree_and_dedupes_fragments(self, tmp_path, monkeypatch):
        db = tmp_path / "ca_topics.db"
        _get_topic_conn(db)  # ov_roots 表 + 种子
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        _patch_tree(monkeypatch)
        docs = w2g._load_all_ov_docs()
        # 12 条目 → 过滤目录/非 md → 碎片去重 → 7 文档
        assert len(docs) == 7
        nids = {d["nid"] for d in docs}
        assert "ov_doc_15-fingerprint-dedup.md" in nids
        assert "ov_doc_index.md" in nids
        assert "ov_doc_37-reality-restructure.md" in nids
        assert "ov_doc_topic-summarization-decision.md" in nids
        # 碎片/隐藏摘要不产生独立节点
        assert "ov_doc_背景.md" not in nids
        assert "ov_doc_.overview.md" not in nids
        assert "ov_doc_决策_37CA_Reali.md" not in nids
        # 全部为 .md 文件条目
        assert all(d["rel_path"].endswith(".md") for d in docs)
        # 碎片先出现也保留主文档 uri（最短 rel_path）
        main15 = next(d for d in docs
                      if d["nid"] == "ov_doc_15-fingerprint-dedup.md")
        assert main15["uri"].endswith(
            "decisions/15-fingerprint-dedup/15-fingerprint-dedup.md")
        # 输出稳定有序
        assert [d["rel_path"] for d in docs] == sorted(
            d["rel_path"] for d in docs)

    def test_failure_returns_empty_no_raise(self, tmp_path, monkeypatch):
        def boom(req, timeout=None):
            raise ConnectionRefusedError("ov down")
        db = tmp_path / "ca_topics.db"
        _get_topic_conn(db)  # ov_roots 表 + 种子
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert w2g._load_all_ov_docs() == []

    def test_extract_tree_entries_shapes(self):
        e1 = {"uri": "a.md", "rel_path": "a.md", "isDir": False}
        assert w2g._extract_tree_entries([e1]) == [e1]
        assert w2g._extract_tree_entries({"entries": [e1]}) == [e1]
        assert w2g._extract_tree_entries({"result": {"items": [e1]}}) == [e1]
        assert w2g._extract_tree_entries({"data": [e1]}) == [e1]
        assert w2g._extract_tree_entries({"nope": 1}) == []


class TestBuildSubgraphOvImport:
    """build_wiki_subgraph：无条件建全量 ov_doc 节点。"""

    def test_builds_all_ov_nodes(self, tmp_path, monkeypatch):
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        _patch_tree(monkeypatch)
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))

        sub = w2g.build_wiki_subgraph()
        ov = [n for n in sub["nodes"]
              if n.get("id", "").startswith("ov_doc_")]
        # 全量入图：7 文档各一节点
        assert len(ov) == 7
        ids = [n["id"] for n in ov]
        assert len(ids) == len(set(ids))  # 无双节点
        for n in ov:
            assert n["file_type"] == "knowledge"
            assert n["_origin"] == "ov_import"
            assert n["community"] == 0
            assert n["metadata"]["uri"].startswith(
                "viking://resources/projects/context-assembler")
            assert n["source_file"] == n["metadata"]["uri"]

    def test_naming_identical_to_fact_linking(self, tmp_path, monkeypatch):
        """入图 nid 与 ca/fact_linking._ov_doc_nid 逐字节一致（references_ov 打通核心）。"""
        db = tmp_path / "ca_topics.db"
        _get_topic_conn(db)  # ov_roots 表 + 种子
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        _patch_tree(monkeypatch)
        docs = w2g._load_all_ov_docs()
        assert docs
        for d in docs:
            assert d["nid"] == fl._ov_doc_nid({"uri": d["uri"]})
        # 检索命中碎片 uri → 与入图节点同一 nid（信号 C target 可命中）
        frag = ("viking://resources/projects/context-assembler/decisions/"
                "37-reality-restructure.md/决策_37CA_Reali.md")
        assert w2g._uri_ov_doc_nid(frag) == fl._ov_doc_nid({"uri": frag}) \
            == "ov_doc_37-reality-restructure.md"
        # 仅隐藏摘要（无主文档段）→ 双方都返回 None（防悬挂）
        only_hidden = ("viking://resources/projects/context-assembler/"
                       "decisions/34-idle-refinement/.overview.md")
        assert w2g._uri_ov_doc_nid(only_hidden) is None
        assert fl._ov_doc_nid({"uri": only_hidden}) is None

    def test_bigram_trace_edge_targets_uri_nid(self, tmp_path, monkeypatch):
        """title 命名 vs URI 命名：同一文档只建 URI 命名节点，trace 边指向它。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db, name="Reality 重构")
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: _fixture_docs())
        monkeypatch.setattr(w2g, "_load_ov_doc_titles", lambda: [
            {"title": "决策 37：Reality 重构", "label": "决策 37：Reality 重构",
             "nid": "ov_doc_决策_37_reality_重构",  # 旧 title 命名（应被忽略）
             "uri": "viking://resources/projects/context-assembler/decisions/"
                    "37-reality-restructure.md/37-reality-restructure.md",
             "words": ["reality", "重构"]},
        ])
        monkeypatch.setattr(w2g, "_bigram_idf",
                            lambda titles: {"reality": 3.0, "重构": 3.0})

        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        assert "ov_doc_37-reality-restructure.md" in ids
        assert "ov_doc_决策_37_reality_重构" not in ids  # 无 title 命名旧节点
        # 节点引用已建的全量节点（未被 bigram 重建为 trace 节点）
        ov_node = next(n for n in sub["nodes"]
                       if n["id"] == "ov_doc_37-reality-restructure.md")
        assert ov_node["_origin"] == "ov_import"
        # trace 边只指向 URI 命名节点
        trace = [e for e in sub["edges"] if e.get("relation") == "trace"]
        assert trace
        assert all(e["target"] == "ov_doc_37-reality-restructure.md"
                   for e in trace)

    def test_degraded_path_keeps_trace_logic(self, tmp_path, monkeypatch):
        """fs/tree 不可用 → 降级：bigram 命中仍建节点（原 TRACE_SOURCES 逻辑）。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db, name="Reality 重构")
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: [])
        monkeypatch.setattr(w2g, "_load_ov_doc_titles", lambda: [
            {"title": "Reality 重构", "label": "Reality 重构",
             "nid": "ov_doc_37-reality-restructure.md",
             "uri": "viking://resources/projects/context-assembler/decisions/"
                    "37-reality-restructure.md/37-reality-restructure.md",
             "words": ["reality", "重构"]},
        ])
        monkeypatch.setattr(w2g, "_bigram_idf",
                            lambda titles: {"reality": 3.0, "重构": 3.0})

        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        assert "ov_doc_37-reality-restructure.md" in ids
        ov_node = next(n for n in sub["nodes"]
                       if n["id"] == "ov_doc_37-reality-restructure.md")
        assert ov_node["_origin"] == "trace"  # 降级路径保留原 trace 建节点
        assert any(e.get("relation") == "trace" for e in sub["edges"])

    def test_build_degrades_without_ov(self, tmp_path, monkeypatch):
        """OV 不可用 → 不抛异常，reality 节点照常，无 ov_doc 节点。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)

        def boom(req, timeout=None):
            raise ConnectionRefusedError("ov down")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))

        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        assert any(i.startswith("reality_") for i in ids)
        assert not any(i.startswith("ov_doc_") for i in ids)

    def test_merge_idempotent(self, tmp_path, monkeypatch):
        """重复 merge 不产生重复节点；无悬挂边。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        monkeypatch.setattr(w2g, "CA_TOPICS_DB", str(db))
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: _fixture_docs())
        monkeypatch.setattr(w2g, "_load_ov_doc_titles", lambda: [])

        sub = w2g.build_wiki_subgraph()
        assert len([n for n in sub["nodes"]
                    if n["id"].startswith("ov_doc_")]) == 7
        assert w2g.merge_into_main_graph(sub, str(gp), str(db))
        g1 = json.loads(gp.read_text(encoding="utf-8"))
        assert len([n for n in g1["nodes"]
                    if n["id"].startswith("ov_doc_")]) == 7
        assert w2g.merge_into_main_graph(sub, str(gp), str(db))
        g2 = json.loads(gp.read_text(encoding="utf-8"))
        ov2 = [n for n in g2["nodes"] if n["id"].startswith("ov_doc_")]
        assert len(ov2) == 7
        assert len({n["id"] for n in ov2}) == 7  # 幂等：无重复节点
        ids = {n["id"] for n in g2["nodes"]}
        assert all(l["source"] in ids and l["target"] in ids
                   for l in g2["links"])  # 无悬挂边


class TestCleanupOvDocs:
    """_clean_knowledge_domain：ov_doc_ 前缀节点以 fs/tree 清单为权威。"""

    def test_removes_title_named_ov_docs(self, tmp_path, monkeypatch):
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: _fixture_docs())
        main = {
            "nodes": [
                {"id": "reality_1", "file_type": "knowledge"},
                {"id": "ov_doc_15-fingerprint-dedup.md", "file_type": "knowledge"},
                {"id": "ov_doc_37-reality-restructure.md", "file_type": "knowledge"},
                {"id": "ov_doc_决策_38：reality_图模型", "file_type": "knowledge"},
                {"id": "ov_doc_ca_↔_openviking_话题自动提交_—_技术方案",
                 "file_type": "knowledge"},
                {"id": "ov_doc_旧文档.md", "file_type": "knowledge"},
                {"id": "topic_manager", "file_type": "code"},
            ],
            "links": [
                {"source": "reality_1", "target": "ov_doc_决策_38：reality_图模型",
                 "relation": "trace"},
                {"source": "reality_1", "target": "ov_doc_15-fingerprint-dedup.md",
                 "relation": "trace"},
                {"source": "reality_1", "target": "ov_doc_旧文档.md",
                 "relation": "trace"},
                {"source": "topic_manager", "target": "ov_doc_15-fingerprint-dedup.md",
                 "relation": "calls"},
            ],
        }
        removed_nodes, removed_edges = w2g._clean_knowledge_domain(main, str(db))
        ids = {n["id"] for n in main["nodes"]}
        # 旧 title 命名节点 + 不在清单节点 → 删
        assert "ov_doc_决策_38：reality_图模型" not in ids
        assert "ov_doc_ca_↔_openviking_话题自动提交_—_技术方案" not in ids
        assert "ov_doc_旧文档.md" not in ids
        # 清单内 URI 命名节点 → 保留；代码节点不动
        assert "ov_doc_15-fingerprint-dedup.md" in ids
        assert "ov_doc_37-reality-restructure.md" in ids
        assert "topic_manager" in ids
        # 连带边删除
        rels = {(l["source"], l["target"]) for l in main["links"]}
        assert ("reality_1", "ov_doc_决策_38：reality_图模型") not in rels
        assert ("reality_1", "ov_doc_旧文档.md") not in rels
        assert ("reality_1", "ov_doc_15-fingerprint-dedup.md") in rels
        assert ("topic_manager", "ov_doc_15-fingerprint-dedup.md") in rels
        assert removed_nodes == 3
        assert removed_edges == 2

    def test_skips_rule_when_tree_unavailable(self, tmp_path, monkeypatch):
        """fs/tree 不可用（空清单）→ 保守跳过 ov_doc 清理（保留 TRACE_SOURCES 节点）。"""
        db = tmp_path / "ca_topics.db"
        _mk_reality(db)
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: [])
        main = {
            "nodes": [
                {"id": "reality_1", "file_type": "knowledge"},
                {"id": "ov_doc_决策_38：reality_图模型", "file_type": "knowledge"},
                {"id": "ov_doc_34-idle-refinement.md", "file_type": "knowledge"},
            ],
            "links": [],
        }
        removed_nodes, removed_edges = w2g._clean_knowledge_domain(main, str(db))
        ids = {n["id"] for n in main["nodes"]}
        assert "ov_doc_决策_38：reality_图模型" in ids
        assert "ov_doc_34-idle-refinement.md" in ids
        assert removed_nodes == 0
        assert removed_edges == 0


class TestMultiRootOvImport:
    """决策 44：多根递归拉取 + filters + 跨根 nid 优先级。"""

    def test_build_multi_root_merge(self, tmp_path, monkeypatch):
        """mock 3 根 enabled=1、重叠文档、CA 优先 nid、windows 文档入图。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        _get_topic_conn(db).execute(
            "UPDATE ov_roots SET enabled=1 WHERE root_uri=?",
            ("viking://resources/projects/irobot",))
        _get_topic_conn(db).commit()
        _mk_reality(db, name="连接池优化")

        docs = w2g._load_all_ov_docs()
        nids = {d["nid"] for d in docs}
        # CA 文档仍在
        assert "ov_doc_index.md" in nids
        assert "ov_doc_15-fingerprint-dedup.md" in nids
        # windows 根 8 个 references_ov 目标文档入图（碎片去重后主文档）
        assert "ov_doc_03-electron-crash-browser.md" in nids
        assert "ov_doc_01-windows-ollama-backend.md" in nids
        assert "ov_doc_02-timeout-context.md" in nids
        assert "ov_doc_01-vram-usage.md" in nids
        assert "ov_doc_reindex-embedding-rebuild.md" in nids
        assert "ov_doc_mcp-tools.md" in nids
        assert "ov_doc_cluster-migration.md" in nids
        # irobot 根（本轮启用）唯一文档入图；与 CA 冲突的 INDEX.md 保留 CA
        assert "ov_doc_slam-nav.md" in nids
        assert "ov_doc_shared.md" in nids
        # 碎片不产生独立节点
        assert "ov_doc_背景.md" not in nids
        # 跨根同名 INDEX.md → context-assembler 优先（uri 指向 CA 根）
        ca_index = next(d for d in docs if d["nid"] == "ov_doc_index.md")
        assert ca_index["uri"].startswith(
            "viking://resources/projects/context-assembler")
        # build 子图含 windows 文档
        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        assert "ov_doc_03-electron-crash-browser.md" in ids
        assert "ov_doc_cluster-migration.md" in ids
        assert "ov_doc_index.md" in ids

    def test_filters_include_exclude_code(self, tmp_path, monkeypatch):
        """include+exclude 组合：code/ 排除、include 首段白名单、label 不被 override。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        _get_topic_conn(db).execute(
            "UPDATE ov_roots SET filters=? WHERE root_uri=?",
            (json.dumps({"include": ["comfyui-model-setup",
                                      "show-me-the-story"],
                         "exclude": ["/code/"]},
                        ensure_ascii=False),
             "viking://resources/projects/windows"))
        _get_topic_conn(db).commit()
        _mk_reality(db, name="连接池优化")

        docs = w2g._load_all_ov_docs()
        nids = {d["nid"] for d in docs}
        # include 白名单内 → 收录
        assert "ov_doc_03-electron-crash-browser.md" in nids
        assert "ov_doc_01-windows-ollama-backend.md" in nids
        # include 白名单外（comfyui-vram-free / ops）→ 不收录
        assert "ov_doc_01-vram-usage.md" not in nids
        assert "ov_doc_reindex-embedding-rebuild.md" not in nids
        assert "ov_doc_cluster-migration.md" not in nids
        # exclude /code/ → code 目录文档不收录
        assert "ov_doc_notes.md" not in nids
        # B2 回归：深层 code/ 条目（mock rel_path 相对被查询 uri、丢前缀）
        # 补全后含 /code/ 子串 → 同样被排除
        assert "ov_doc_commands.md" not in nids

        # build 层：code/ 文档无节点、无 code label override
        sub = w2g.build_wiki_subgraph()
        nodes = sub["nodes"]
        ids = {n["id"] for n in nodes}
        assert "ov_doc_notes.md" not in ids
        assert "ov_doc_commands.md" not in ids
        assert not any(str(n.get("source_location", "")).startswith(
            "windows/comfyui-model-setup/code/") for n in nodes)
        # 收录文档保持 knowledge 语义（file_type 不被 override 为 code）
        target = next(n for n in nodes
                      if n["id"] == "ov_doc_03-electron-crash-browser.md")
        assert target["file_type"] == "knowledge"
        assert target["_origin"] == "ov_import"

    def test_cross_root_nid_collision_priority(self, tmp_path, monkeypatch):
        """跨根同名文档保留 context-assembler 优先（同 nid 后到者丢弃）。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        _get_topic_conn(db).execute(
            "UPDATE ov_roots SET enabled=1 WHERE root_uri=?",
            ("viking://resources/projects/irobot",))
        _get_topic_conn(db).commit()

        docs = w2g._load_all_ov_docs()
        by_nid = {d["nid"]: d for d in docs}
        # INDEX.md 跨根同名 → 保留 CA 根 uri
        assert by_nid["ov_doc_index.md"]["uri"].startswith(
            "viking://resources/projects/context-assembler")
        # irobot 唯一文档不受影响
        assert by_nid["ov_doc_slam-nav.md"]["uri"].startswith(
            "viking://resources/projects/irobot")

    def test_recursive_tree_seen_dedup(self, monkeypatch):
        """递归 seen 去环：A→B→A 不再展开，嵌套文件收集。"""
        a = "viking://resources/projects/a"
        b = "viking://resources/projects/a/b"
        monkeypatch.setattr(urllib.request, "urlopen", _FakeUrlopenMulti({
            a: _ok_entries([
                {"uri": b, "rel_path": "b", "isDir": True},
                {"uri": f"{a}/root.md", "rel_path": "root.md", "isDir": False},
            ]),
            b: _ok_entries([
                {"uri": a, "rel_path": "..", "isDir": True},  # 环
                {"uri": f"{b}/deep.md", "rel_path": "b/deep.md",
                 "isDir": False},
            ]),
        }))
        entries = w2g._recursive_fs_tree(a)
        uris = {e["uri"] for e in entries}
        assert f"{a}/root.md" in uris
        assert f"{b}/deep.md" in uris

    def test_recursive_tree_deep_rel_path_complete(self, monkeypatch):
        """B1 回归：深层 fs/tree rel_path 相对被查询 uri（丢全部前缀）→
        _recursive_fs_tree 返回的 rel_path 必须由完整 uri 补全（含全部前缀段）。"""
        root = "viking://resources/projects/windows"
        sub = f"{root}/comfyui-vram-free"
        code = f"{sub}/code"
        deep = f"{code}/commands"
        # 每层 mock rel_path 均相对被查询 uri（实测 OV 形态），递归必须补前缀
        monkeypatch.setattr(urllib.request, "urlopen", _FakeUrlopenMulti({
            root: _ok_entries([
                {"uri": sub, "rel_path": "comfyui-vram-free", "isDir": True},
            ]),
            sub: _ok_entries([
                {"uri": code, "rel_path": "code", "isDir": True},
            ]),
            code: _ok_entries([
                {"uri": deep, "rel_path": "commands", "isDir": True},
            ]),
            deep: _ok_entries([
                {"uri": f"{deep}/commands.md",
                 "rel_path": "commands.md", "isDir": False},
            ]),
        }))
        entries = w2g._recursive_fs_tree(root)
        by_uri = {e["uri"]: e for e in entries}
        assert by_uri[f"{deep}/commands.md"]["rel_path"] == \
            "comfyui-vram-free/code/commands/commands.md"

    def test_filters_empty_all_included(self, tmp_path, monkeypatch):
        """filters 边界：空 {} / 空 include / 空 exclude = 全收录。"""
        for filters in ("{}", '{"include": []}', '{"exclude": []}'):
            db = _seed_ov_roots_db(tmp_path, monkeypatch)
            _get_topic_conn(db).execute(
                "UPDATE ov_roots SET filters=? WHERE root_uri=?",
                (filters, "viking://resources/projects/windows"))
            _get_topic_conn(db).commit()
            nids = {d["nid"] for d in w2g._load_all_ov_docs()}
            # 默认 exclude 移除后，code/ 文档也全收录
            assert "ov_doc_notes.md" in nids
            assert "ov_doc_cluster-migration.md" in nids


class TestReferencesOvLanding:
    """决策 44：多根入图后 references_ov 边落图（8 候选、无悬挂）。"""

    def test_references_ov_landing_fact_linking(self, tmp_path, monkeypatch):
        """env 阈值 0.55 + mock OV 服务：8 个 references_ov 边落图且无悬挂。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        _mk_reality(db, name="连接池优化")
        # 8 个 references_ov 候选（R2/R29/R31/R32/R33/R36/R37/R38）
        targets = [
            (1, "Electron 崩溃排查",
             "viking://resources/projects/windows/comfyui-model-setup/decisions/03-electron-crash-browser.md"),
            (2, "Windows Ollama 后端",
             "viking://resources/projects/windows/show-me-the-story/decisions/01-windows-ollama-backend.md"),
            (3, "超时上下文",
             "viking://resources/projects/windows/show-me-the-story/decisions/02-timeout-context.md"),
            (4, "VRAM 占用",
             "viking://resources/projects/windows/comfyui-vram-free/decisions/01-vram-usage.md"),
            (5, "Embedding 重建",
             "viking://resources/projects/windows/ops/openviking/reindex-embedding-rebuild.md"),
            (6, "MCP 工具",
             "viking://resources/projects/windows/ops/openviking/components/mcp-tools.md"),
            (7, "Ollama 窗口后端",
             "viking://resources/projects/windows/show-me-the-story/decisions/01-windows-ollama-backend.md"),
            (8, "集群迁移",
             "viking://resources/projects/windows/ops/graphify/cluster-migration.md"),
        ]
        realities = [
            {"reality_id": rid, "name": name, "hdl": "",
             "current_status": {}}
            for rid, name, _ in targets
        ]

        def _hits(query):
            for _, name, uri in targets:
                if name in query:
                    return [{"uri": uri, "score": 0.65}]
            return []

        # 多根子图 → 合并到主图（windows 目标文档节点入图）
        sub = w2g.build_wiki_subgraph()
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps({"nodes": [], "links": []}),
                      encoding="utf-8")
        assert w2g.merge_into_main_graph(sub, str(gp), str(db))

        monkeypatch.setattr(fl, "_graph_path", lambda: gp)
        monkeypatch.setattr(fl, "OV_REFERENCE_THRESHOLD", 0.55)
        monkeypatch.setattr(fl, "_judge_link_relation", lambda s, t: None)
        monkeypatch.setattr(fl, "_ov_search_find", _hits)

        added = fl.run_fact_linking(None, realities)
        g = json.loads(gp.read_text(encoding="utf-8"))
        refs = [l for l in g["links"]
                if l.get("relation") == "references_ov"]
        assert added >= 8
        assert len(refs) >= 8
        # 无悬挂：全部 target 在图节点集内
        ids = {n["id"] for n in g["nodes"]}
        assert all(l["target"] in ids for l in refs)
        # 边关系完整
        assert all(l["_origin"] == "fact_linking" for l in refs)


class TestCaRootTraceGate:
    """R6（决策 44 续）：无根豁免——CA 根 disable 时跳过 trace 建边。"""

    def _disable_ca_root(self, db):
        conn = _get_topic_conn(db)
        conn.execute(
            "UPDATE ov_roots SET enabled=0 WHERE root_uri=?",
            ("viking://resources/projects/context-assembler",))
        conn.commit()

    def test_load_ov_roots_excludes_disabled_ca_root(self, tmp_path,
                                                     monkeypatch):
        """CA 根 disable → _load_ov_roots 不含 CA 根（无豁免）。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        self._disable_ca_root(db)
        roots = w2g._load_ov_roots()
        uris = {r["root_uri"] for r in roots}
        assert "viking://resources/projects/context-assembler" not in uris
        assert "viking://resources/projects/windows" in uris

    def test_add_trace_edges_gated_before_fetch(self, monkeypatch):
        """_add_trace_edges(ca_root_enabled=False) → return 在 fetch 前（不 fetch 不建边）。"""
        def boom(uri):
            raise AssertionError("ca_root_enabled=False 时不应 fetch OV")
        monkeypatch.setattr(w2g, "_fetch_ov_raw", boom)
        subgraph = {"nodes": [], "edges": []}
        w2g._add_trace_edges(subgraph, set(), ca_root_enabled=False)
        assert subgraph["nodes"] == []
        assert subgraph["edges"] == []

    def test_bigram_gate_fs_tree_available(self, tmp_path, monkeypatch):
        """fs/tree 可用 + nid 碰撞：CA 根 disabled → 节点存在但 trace 边不建。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        self._disable_ca_root(db)
        _mk_reality(db, name="Reality 重构")
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: _fixture_docs())
        monkeypatch.setattr(w2g, "_load_ov_doc_titles", lambda: [
            {"title": "决策 37：Reality 重构", "label": "决策 37：Reality 重构",
             "nid": "ov_doc_37-reality-restructure.md",
             "uri": "viking://resources/projects/context-assembler/decisions/"
                    "37-reality-restructure.md/37-reality-restructure.md",
             "words": ["reality", "重构"]},
        ])
        monkeypatch.setattr(w2g, "_bigram_idf",
                            lambda titles: {"reality": 3.0, "重构": 3.0})

        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        # ov_import 全量节点仍建（mock 清单）；但 CA 根 disabled → 无 trace 边
        assert "ov_doc_37-reality-restructure.md" in ids
        trace = [e for e in sub["edges"] if e.get("relation") == "trace"]
        assert all(e["target"] != "ov_doc_37-reality-restructure.md"
                   for e in trace)
        # 对照组：CA 根 enabled → trace 边存在
        _get_topic_conn(db).execute(
            "UPDATE ov_roots SET enabled=1 WHERE root_uri=?",
            ("viking://resources/projects/context-assembler",))
        _get_topic_conn(db).commit()
        sub2 = w2g.build_wiki_subgraph()
        trace2 = [e for e in sub2["edges"] if e.get("relation") == "trace"]
        assert any(e["target"] == "ov_doc_37-reality-restructure.md"
                   for e in trace2)

    def test_bigram_gate_degraded_no_crash(self, tmp_path, monkeypatch):
        """降级（fs/tree 不可用）：CA 根 disabled → 不建 trace 节点/边，不崩。"""
        db = _seed_ov_roots_db(tmp_path, monkeypatch)
        self._disable_ca_root(db)
        _mk_reality(db, name="Reality 重构")
        monkeypatch.setattr(w2g, "_load_all_ov_docs", lambda: [])
        monkeypatch.setattr(w2g, "_load_ov_doc_titles", lambda: [
            {"title": "Reality 重构", "label": "Reality 重构",
             "nid": "ov_doc_37-reality-restructure.md",
             "uri": "viking://resources/projects/context-assembler/decisions/"
                    "37-reality-restructure.md/37-reality-restructure.md",
             "words": ["reality", "重构"]},
        ])
        monkeypatch.setattr(w2g, "_bigram_idf",
                            lambda titles: {"reality": 3.0, "重构": 3.0})

        sub = w2g.build_wiki_subgraph()
        ids = {n["id"] for n in sub["nodes"]}
        assert "ov_doc_37-reality-restructure.md" not in ids
        assert not any(e.get("relation") == "trace" for e in sub["edges"])
        # _meta 出口存在（main 读取路径）
        assert sub.get("_meta", {}).get("ca_root_enabled") is False
