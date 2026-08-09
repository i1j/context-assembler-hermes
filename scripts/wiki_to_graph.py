"""wiki_to_graph.py — 将 wiki 知识条目同步到 Graphify 图谱。

设计：
  1. 读取 themes 表，为每个 theme 生成一个知识节点
  2. 读取 theme_strand_map，在归并的 theme 之间生成边
  3. 读取 OV 设计文档的 frontmatter trace: → 生成 trace 边（决策↔代码追溯）
  4. 输出到 graphify-out/wiki_subgraph.json
  5. 合并到主 graph.json

触发：_run_wiki_merge 之后（daemon 子进程）
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

try:
    import urllib.request
    _HAS_URLLIB = True
except ImportError:
    _HAS_URLLIB = False

def _resolve_ca_cache_dir() -> str:
    """解析 CA cache 目录（2026-08-08 修复：不再硬编码 tester profile）。

    解析顺序：CA_CACHE_DIR env（显式覆盖）→ HERMES_HOME env（gateway/独立脚本
    注入）→ ~/.hermes/ca_cache fallback。与 store._get_topic_store_path 对齐，
    避免 winker/sysadmin profile 跑时误读 tester 库。
    """
    env_cache = os.getenv("CA_CACHE_DIR", "").strip()
    if env_cache:
        return env_cache
    env_home = os.getenv("HERMES_HOME", "").strip()
    if env_home:
        return str(Path(env_home) / "ca_cache")
    return str(Path.home() / ".hermes" / "ca_cache")


CA_CACHE_DIR = _resolve_ca_cache_dir()
GRAPHIFY_OUT = os.getenv(
    "GRAPHIFY_OUT",
    str(Path(__file__).resolve().parent.parent / "graphify-out"),
)
CA_TOPICS_DB = os.path.join(CA_CACHE_DIR, "ca_topics.db")
WIKI_GRAPH_FILE = os.path.join(GRAPHIFY_OUT, "wiki_subgraph.json")
GRAPH_JSON = os.path.join(GRAPHIFY_OUT, "graph.json")

# ── 节点 label 前缀，避免与 AST 节点冲突 ──
NODE_PREFIX = "[知识]"

# ── OV 设计文档追溯 ──
OV_API = os.getenv("OV_API", "http://127.0.0.1:1933")
TRACE_SOURCES = [
    "viking://resources/projects/context-assembler/decisions/topic-summarization-decision.md/话题摘要化设计_v3_取代_OV_VLM_摘要.md",
    "viking://resources/projects/context-assembler/architecture/ca-ov-topic-submit.md",
    # 决策 43 v4 目录重组后：三条核心决策线（34 精炼轮 / 37 reality 重构 / 38-reality 图模型）
    "viking://resources/projects/context-assembler/decisions/34-idle-refinement/34-idle-refinement.md",
    "viking://resources/projects/context-assembler/decisions/37-reality-restructure.md/37-reality-restructure.md",
    "viking://resources/projects/context-assembler/decisions/38-reality-graph-inject-merge.md/38-reality-graph-inject-merge.md",
]
CA_CODE_DIR = str(Path(__file__).resolve().parent.parent)

# ── ov_roots 多根配置（决策 44 前置，2026-08-08）──
# 仅建表兜底；种子权威在 ca/store.py _migrate_ov_roots_seed（user_version 门控）。
# 子进程绝不播种：否则 graphify 会把 LLM remove/disable 的根当场复活 enabled=1
# （决策 44 续 R6/R7）。
_OV_CA_ROOT = "viking://resources/projects/context-assembler"


def _uri_belongs_to_root(uri: str, root_uri: str) -> bool:
    """根边界归属：uri == 根 或 uri 在根 + "/" 前缀下（与 ca/refinement 同规则）。"""
    base = root_uri.rstrip("/")
    return uri == base or uri.startswith(base + "/")


_OV_ROOTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ov_roots (
    root_uri    TEXT PRIMARY KEY,
    filters     TEXT DEFAULT '{}',
    enabled     INTEGER DEFAULT 1,
    origin      TEXT DEFAULT 'manual',
    added_at    REAL,
    last_seen   REAL
);
"""


def _get_db_path() -> str:
    """ca_topics.db 路径解析（2026-08-08 修复：HERMES_HOME 优先，不再硬编码 tester）。

    解析顺序：模块级 CA_TOPICS_DB 显式覆盖（测试/独立脚本）→ CA_CACHE_DIR env
    （显式覆盖）→ HERMES_HOME env（gateway/子进程注入）→ CA_CACHE_DIR fallback。
    """
    env_cache = os.getenv("CA_CACHE_DIR", "").strip()
    env_home = os.getenv("HERMES_HOME", "").strip()
    computed = (
        os.path.join(env_cache, "ca_topics.db") if env_cache
        else str(Path(env_home) / "ca_cache" / "ca_topics.db") if env_home
        else os.path.join(CA_CACHE_DIR, "ca_topics.db")
    )
    if CA_TOPICS_DB != computed:
        return CA_TOPICS_DB  # 显式覆盖（测试 monkeypatch / 调用方赋值）
    return computed


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    _ensure_ov_roots(conn)
    return conn


def _ensure_ov_roots(conn: sqlite3.Connection) -> None:
    """仅建表兜底；种子权威在 ca/store.py `_migrate_ov_roots_seed`（user_version 门控）。

    独立脚本入口不播种：LLM remove/disable 的根不被子进程复活（R6/R7）。
    """
    try:
        conn.executescript(_OV_ROOTS_SCHEMA_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        print(f"  [WIKI_GRAPH] ov_roots ensure failed: {exc}")


def _extract_bigrams(text: str) -> list[str]:
    """中英混合 bigram 切分：中文连续 2 字 + 英文 token。

    v6.5.4: 原整串 `\w+` 匹配对中文无分词能力（'话题切换' 与 '话题摘要'
    零重叠），改为字符级 bigram，'摘要' 可跨标题命中。
    """
    out: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(chunk) - 1):
            out.append(chunk[i:i + 2])
    for m in re.findall(r"[A-Za-z]{2,}", text):
        out.append(m.lower())
    return out


def _bigram_idf(all_titles: list[str]) -> dict[str, float]:
    """log-IDF 权重：bigram 在全部 theme 标题中的稀有度。

    idf = ln(N/df)，df 是该 bigram 出现的 theme 数。
    高频泛词（优化/机制/话题）权重低，专有词（提交/摘要）权重高。
    """
    import math
    n = max(len(all_titles), 1)
    df: dict[str, int] = {}
    for t in all_titles:
        for bg in set(_extract_bigrams(t)):
            df[bg] = df.get(bg, 0) + 1
    return {bg: math.log(n / d) for bg, d in df.items()}


def _uri_ov_doc_nid(uri: str) -> str | None:
    """URI 文件段规则生成 ov_doc nid（与 ca/fact_linking._ov_doc_nid uri 分支逐字节一致）。

    取 uri 中第一个非隐藏 .md 段（跳过 .overview/.abstract 等隐藏摘要与碎片路径），
    保留 .md 后缀；无主文档段 → None。
    """
    parts = [p for p in uri.rstrip("/").split("/") if p]
    for p in parts:
        if p.endswith(".md") and not p.startswith("."):
            return f"ov_doc_{p.lower().replace(' ', '_')[:48]}"
    return None


def _extract_tree_entries(payload: Any) -> list[dict]:
    """fs/tree 响应形状兜底：顶层 list / entries / items / data / result 嵌套。"""
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("entries", "items", "nodes", "files", "data"):
        v = payload.get(key)
        if isinstance(v, list):
            return [e for e in v if isinstance(e, dict)]
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("entries", "items", "nodes", "files", "data"):
            v = result.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    elif isinstance(result, list):
        return [e for e in result if isinstance(e, dict)]
    return []


def _fetch_tree(uri: str) -> list[dict]:
    """GET /api/v1/fs/tree 单层条目列表；网络失败/超时 → [] + 警告（降级）。"""
    if not _HAS_URLLIB:
        return []
    import urllib.parse
    url = (f"{OV_API.rstrip('/')}/api/v1/fs/tree?"
           + urllib.parse.urlencode({"uri": uri}))
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  [WIKI_GRAPH] fs/tree fetch failed: {exc}"
              " → 降级（保留 TRACE_SOURCES 逻辑）")
        return []
    return _extract_tree_entries(payload)


def _recursive_fs_tree(uri: str, depth: int = 0, max_depth: int = 6,
                       seen: set | None = None,
                       root_uri: str | None = None) -> list[dict]:
    """递归 fs/tree 收集（决策 44：OV fs/tree 非递归，多根必须逐级展开）。

    isDir 条目（字符串 "True"/"1"）递归其 uri；文件条目直接收集。
    max_depth 防失控（实测 windows 最深 4 级，6 足够）；seen 去环（防御性）。
    单层失败 → 跳过该子目录（其余根不受影响）。
    rel_path 完整性（2026-08-09 修复 B1/B2）：fs/tree 返回的 rel_path 随查询
    层级变化（实测相对被查询 uri、顶层含/不含根段形态不一），递归收集必然丢
    前缀 → 条目 rel_path 一律由完整 uri 剥离 root_uri 前缀推导（_rel_from_uri），
    深层条目含全部前缀段，exclude "/code/" 等子串匹配不失效。root_uri 默认取
    顶层 uri。
    """
    if seen is None:
        seen = set()
    if root_uri is None:
        root_uri = uri
    if depth > max_depth or uri in seen:
        return []
    seen.add(uri)
    out: list[dict] = []
    for e in _fetch_tree(uri):
        entry = dict(e)
        entry_uri = str(entry.get("uri") or "").strip()
        rel = _rel_from_uri(entry_uri, root_uri) if entry_uri else None
        if rel:
            entry["rel_path"] = rel
        if str(entry.get("isDir", "false")).strip().lower() in ("true", "1"):
            if entry_uri:
                out.extend(_recursive_fs_tree(
                    entry_uri, depth + 1, max_depth, seen, root_uri))
        else:
            out.append(entry)
    return out


def _rel_from_uri(uri: str, root_uri: str) -> str | None:
    """从完整 uri 推导相对根 rel_path（修复 B1，2026-08-09）。

    OV fs/tree 返回的 rel_path 形态不一：实测相对被查询 uri（如查
    .../comfyui-vram-free/code 返回 commands/commands.md），顶层又可能含根段
    （windows/comfyui-model-setup/...）或仅含子项目前缀。唯一恒定可靠的是完整
    uri → 剥离 root_uri 前缀即完整相对根路径
    （comfyui-vram-free/code/commands/commands.md），exclude 子串匹配
    （如 /code/）不再漏网。uri 不在 root_uri 下（防御）→ None，调用方保留
    fs/tree 原 rel_path。
    """
    base = root_uri.rstrip("/")
    u = uri.rstrip("/")
    if u.startswith(base + "/"):
        return u[len(base) + 1:]
    return None


def _rel_to_root(rel_path: str, root_uri: str) -> str:
    """rel_path 相对根路径：若首段是根名则剥离（OV fs/tree 两种形态兼容）。

    fs/tree 实测 rel_path 含根段（如 windows/comfyui-model-setup/...）；现有
    mock/部分服务返回不含根段（decisions/...）。include 过滤按相对根匹配。
    """
    base = root_uri.rstrip("/").split("/")[-1]
    parts = rel_path.split("/")
    if parts and parts[0] == base:
        return "/".join(parts[1:])
    return rel_path


def _apply_filters(entries: list[dict], filters: dict,
                   root_uri: str) -> list[dict]:
    """filters 收集层过滤（评审 T2：职责分离，_clean_knowledge_domain 不重复过滤）。

    - include: 前缀匹配 rel_path 相对根的首段（如 ["comfyui-model-setup"]）
    - exclude: 子串匹配 rel_path 全路径（含根段，如 ["/code/"] 排除 code/ 目录）
    - 空 {} / 空数组 = 全收录
    """
    include = [p for p in (filters.get("include") or []) if isinstance(p, str)]
    exclude = [p for p in (filters.get("exclude") or []) if isinstance(p, str)]
    out: list[dict] = []
    for e in entries:
        rel = str(e.get("rel_path") or "").strip()
        if not rel:
            continue
        if include:
            first = _rel_to_root(rel, root_uri).split("/", 1)[0]
            if not any(first.startswith(p) for p in include):
                continue
        if any(p in rel for p in exclude):
            continue
        out.append(e)
    return out


def _load_ov_roots(conn: sqlite3.Connection | None = None) -> list[dict]:
    """读 ov_roots 表 enabled=1 的根（context-assembler 最高优先级，先收集）。

    Returns:
        [{"root_uri", "filters"}, ...]；表不可读 → [] + 警告（降级为空清单）。
    """
    if conn is None:
        conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT root_uri, filters FROM ov_roots WHERE enabled=1 "
            "ORDER BY CASE WHEN root_uri = ? THEN 0 ELSE 1 END, root_uri",
            (_OV_CA_ROOT,),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"  [WIKI_GRAPH] ov_roots read failed: {exc}")
        return []
    roots: list[dict] = []
    for r in rows:
        try:
            flt = json.loads(r["filters"] or "{}")
        except (json.JSONDecodeError, TypeError):
            flt = {}
        if not isinstance(flt, dict):
            flt = {}
        roots.append({"root_uri": r["root_uri"], "filters": flt})
    return roots


def _load_all_ov_docs() -> list[dict]:
    """多根递归拉取 OV 项目全量文档清单（决策 44，ov_roots 表驱动）。

    遍历 ov_roots 表 enabled=1 的根（context-assembler 优先）→ 每根递归
    fs/tree（max_depth=6、seen 去环）→ 应用该根 filters → 合并去重：
    同根内保留最短 rel_path（碎片/隐藏摘要去重）；跨根同 nid 保留高优先级根
    （后到者丢弃 → ov_nid_by_uri 一对一确定性）。每文档生成
    {"uri", "rel_path", "nid"}（URI 文件段规则，与 _ov_doc_nid 一致）。
    网络失败/超时 → 空列表 + 警告，不抛异常（降级保留原 TRACE_SOURCES 逻辑）。
    """
    if not _HAS_URLLIB:
        print("  [WIKI_GRAPH] fs/tree skipped (no urllib)")
        return []
    roots = _load_ov_roots()
    best: dict[str, dict] = {}
    for root in roots:
        root_uri = root["root_uri"]
        root_best: dict[str, dict] = {}
        for e in _apply_filters(_recursive_fs_tree(root_uri),
                                root["filters"], root_uri):
            uri = str(e.get("uri") or "").strip()
            rel = str(e.get("rel_path") or "").strip()
            if not uri or not rel or not rel.endswith(".md"):
                continue
            nid = _uri_ov_doc_nid(uri)
            if not nid:
                continue
            cur = root_best.get(nid)
            if cur is None or len(rel) < len(cur["rel_path"]):
                root_best[nid] = {"uri": uri, "rel_path": rel, "nid": nid}
        # 跨根合并：first-wins（根已按 context-assembler 优先排序）
        for nid, doc in root_best.items():
            if nid not in best:
                best[nid] = doc
    return sorted(best.values(), key=lambda d: d["rel_path"])

def _load_ov_doc_titles() -> list[dict]:
    """读取 OV 设计/架构文档的 title，供关键词匹配。"""
    docs: list[dict] = []
    for uri in TRACE_SOURCES:
        content = _fetch_ov_raw(uri)
        if not content:
            continue
        # 从 frontmatter 提取 title，或第一个 ##
        doc_title = ""
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                fm = content[3:end]
                for line in fm.split("\n"):
                    if line.strip().startswith("title:"):
                        doc_title = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
        if not doc_title:
            # fallback 顺序：H1（文档主标题）→ URI 末段（文件名）→ H2（章节标题，最易误命中）
            m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
            if m:
                doc_title = m.group(1).strip()
        if not doc_title:
            doc_title = uri.rstrip("/").split("/")[-1]
        if not doc_title:
            m = re.search(r"^##\s+(.+)", content, re.MULTILINE)
            if m:
                doc_title = m.group(1).strip()
        doc_label = doc_title[:60]
        # 2026-08-08：正常路径 trace 边 target 用全量节点 nid（URI 文件段规则，
        # 见 build_wiki_subgraph 的 ov_nid_by_uri）；此处 title 命名 nid 仅作
        # fs/tree 不可用时的降级回退（保留原 TRACE_SOURCES 逻辑）
        doc_nid = f"ov_doc_{doc_label.lower().replace(' ', '_')[:48]}"
        docs.append({
            "title": doc_title,
            "label": doc_label,
            "nid": doc_nid,
            "uri": uri,
            "words": _extract_bigrams(doc_title),
        })
    return docs


# ── OV 文档追溯边生成 ──

def _fetch_ov_raw(uri: str) -> str:
    """通过 OV WebDAV API 获取资源原始内容（含 frontmatter）。"""
    if not _HAS_URLLIB:
        return ""
    import urllib.parse
    # viking://resources/projects/... → /webdav/resources/projects/...
    resource_path = uri.replace("viking://resources/", "")
    if resource_path == uri:
        return ""  # 非 resources URI，跳过
    # 逐个 segment 编码
    segments = resource_path.split("/")
    encoded_segments = [urllib.parse.quote(s, safe="") for s in segments]
    encoded_path = "/".join(encoded_segments)
    url = f"{OV_API}/webdav/resources/{encoded_path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"  [trace] fetch {uri} failed: {exc}")
        return ""


def _parse_trace_frontmatter(content: str) -> dict:
    """从 YAML frontmatter 提取 trace: 字段（无需 yaml 依赖）。"""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm = content[3:end]

    result: dict = {"forward": [], "backward": []}
    section = None
    for line in fm.split("\n"):
        stripped = line.strip()
        # 跳过空行和纯 key:value（非 trace）
        if not stripped or stripped == "---":
            continue
        # 检测 trace: 下的子 section
        m = re.match(r"^\s{2}(forward|backward):", line)
        if m:
            section = m.group(1)
            continue
        # 读取 - item
        m = re.match(r"^\s{4}- (.+)", line)
        if m and section:
            result.setdefault(section, []).append(m.group(1).strip())
    return result


def _add_trace_edges(subgraph: dict, seen_nodes: set[str],
                     ca_root_enabled: bool = True) -> None:
    """从 OV 设计/架构文档读取 trace: → 生成 trace 边。

    R6 (决策 44 续)：CA 根 disabled → 直接返回（跳过 TRACE_SOURCES frontmatter
    trace 边；return 在 fetch 前）。缺省 True 保持旧调用方行为。
    """
    if not ca_root_enabled:
        return  # CA 根 disabled → 跳过 TRACE_SOURCES frontmatter trace 边（R6）
    nodes = subgraph["nodes"]
    edges = subgraph["edges"]

    for uri in TRACE_SOURCES:
        content = _fetch_ov_raw(uri)
        if not content:
            continue
        fm = _parse_trace_frontmatter(content)
        if not fm.get("forward") and not fm.get("backward"):
            continue

        # OV 文档作为源节点
        doc_label = uri.rstrip("/").split("/")[-1]
        # 2026-08-08 命名统一：doc 节点 id 用 URI 文件段规则（与全量节点一致），
        # 避免 topic-summarization-decision 等文档生成 title 命名重复节点
        # （被 _clean_knowledge_domain 清理后 trace 边丢失）
        doc_nid = _uri_ov_doc_nid(uri) or f"ov_doc_{doc_label[:48]}"
        doc_norm = doc_label.lower().replace(" ", "_")[:64]

        if doc_nid not in seen_nodes:
            nodes.append({
                "label": f"{NODE_PREFIX} {doc_label[:60]}",
                "norm_label": doc_norm,
                "file_type": "knowledge",
                "source_file": uri,
                "source_location": "trace",
                "_origin": "trace",
                "id": doc_nid,
                "community": 0,
                "metadata": {"uri": uri},
            })
            seen_nodes.add(doc_nid)

        # forward trace: 文档 → 代码
        code_dir = CA_CODE_DIR
        for entry in fm.get("forward", []):
            # entry 格式: "path/file.py:func_name" 或 "path/file.py"
            parts = entry.split(":", 1)
            file_path = parts[0]
            full_path = os.path.join(code_dir, file_path)
            # 用代码文件路径作为节点 id
            code_nid = f"code_{file_path.replace('/', '_')}"
            code_norm = f"code_{file_path.replace('/', '_')}".lower()[:64]
            if code_nid not in seen_nodes:
                nodes.append({
                    "label": file_path,
                    "norm_label": code_norm,
                    "file_type": "code",
                    "source_file": full_path,
                    "source_location": parts[1] if len(parts) > 1 else "",
                    "_origin": "trace",
                    "id": code_nid,
                    "community": 0,
                })
                seen_nodes.add(code_nid)
            # trace 边
            edges.append({
                "source": doc_nid,
                "target": code_nid,
                "relation": "trace",
                "confidence": "HIGH",
                "confidence_score": 0.95,
                "source_file": "trace",
                "source_location": f"forward:{entry}",
                "weight": 1.0,
            })

        # backward trace: 文档 ← 上游决策
        for entry in fm.get("backward", []):
            # entry 格式: "design/C-003.md"
            up_label = entry.rstrip("/").split("/")[-1]
            up_nid = f"ov_doc_{up_label[:48]}"
            up_norm = up_label.lower().replace(" ", "_")[:64]
            if up_nid not in seen_nodes:
                nodes.append({
                    "label": f"{NODE_PREFIX} {up_label[:60]}",
                    "norm_label": up_norm,
                    "file_type": "knowledge",
                    "source_file": f"viking://resources/projects/context-assembler/{entry}",
                    "source_location": "trace",
                    "_origin": "trace",
                    "id": up_nid,
                    "community": 0,
                })
                seen_nodes.add(up_nid)
            # backward trace 边（方向：上游 → 本文档）
            edges.append({
                "source": up_nid,
                "target": doc_nid,
                "relation": "trace",
                "confidence": "HIGH",
                "confidence_score": 0.95,
                "source_file": "trace",
                "source_location": f"backward:{entry}",
                "weight": 1.0,
            })


def build_wiki_subgraph() -> dict:
    """从 realities 表构建 wiki 子图（决策 41）。

    v6.5.4: 数据源从已废弃的 topic_wiki 表切换为 themes 表（v6.5 schema 迁移）；
    2026-08-07 (决策 41): themes → realities，节点 id reality_{reality_id}。
    节点 id 与 graphify_sync 增量同步对齐（幂等）。
    v6.4: 含 timeline 元数据（从 strand_summaries.turns 查询，strand 粒度）。
    含 OV 文档关键词匹配 → trace 边（决策↔会话知识）。

    Returns:
        graphify 兼容的 JSON dict: {nodes: [...], edges: [...]}
    """
    conn = _get_conn()
    entries = conn.execute(
        "SELECT reality_id, name, current_status, source_strands, updated_at, created_at "
        "FROM realities ORDER BY reality_id"
    ).fetchall()

    # 预加载所有 strand 的 turns（供 timeline 用）
    topic_ranges: dict[str, str] = {}  # "session/Sid" → "[1,2,3]"
    try:
        rows = conn.execute(
            "SELECT session_id, strand_id, turns FROM strand_summaries "
            "WHERE turns IS NOT NULL"
        ).fetchall()
        for r in rows:
            topic_ranges[f"{r['session_id']}/S{r['strand_id']}"] = r['turns']
    except Exception:
        pass

    # 预加载 OV 设计文档的 title（供关键词匹配用）
    ov_doc_titles: list[dict] = _load_ov_doc_titles()

    # v6.5.4: 用全部 reality 名称统计 bigram IDF（专有词加权，抑制 优化/话题 等泛词）
    all_titles = [e["name"] or "" for e in entries]
    idf = _bigram_idf(all_titles)

    # R6 (决策 44 续): CA 根 trace 门控 —— 三态语义：表不可读 → True；
    # row=None（表空/未播种/CA 根 remove）→ True（保守，保留 TRACE_SOURCES
    # 逻辑）；仅 row 存在且 enabled=0 → False（禁用 CA 根 trace 建边）。
    try:
        _row = conn.execute(
            "SELECT enabled FROM ov_roots WHERE root_uri=?",
            (_OV_CA_ROOT,)).fetchone()
        if _row is None:
            ca_root_enabled = True
        else:
            ca_root_enabled = bool(_row["enabled"] == 1)
    except sqlite3.Error:
        ca_root_enabled = True
    conn.close()

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[str] = set()

    # 2026-08-08：OV 资源全量入图（fs/tree 清单；网络失败 → [] 降级保留
    # TRACE_SOURCES 逻辑）。nid 用 URI 文件段规则（与 _ov_doc_nid 逐字节一致），
    # metadata 携带完整 viking:// URI；bigram 匹配只负责 trace 边。
    all_ov_docs: list[dict] = _load_all_ov_docs()
    ov_nid_by_uri = {d["uri"]: d["nid"] for d in all_ov_docs}
    for _doc in all_ov_docs:
        _nid = _doc["nid"]
        if _nid in seen_nodes:
            continue
        _base = _doc["rel_path"].rstrip("/").split("/")[-1]
        nodes.append({
            "label": f"{NODE_PREFIX} {_base[:60]}",
            "norm_label": _base.lower().replace(" ", "_")[:64],
            "file_type": "knowledge",
            "source_file": _doc["uri"],
            "source_location": _doc["rel_path"],
            "_origin": "ov_import",
            "id": _nid,
            "community": 0,
            "metadata": {"uri": _doc["uri"]},
        })
        seen_nodes.add(_nid)

    for e in entries:
        eid = e["reality_id"]
        title = (e["name"] or "").strip()
        try:
            cs = json.loads(e["current_status"]) if isinstance(e["current_status"], str) else (e["current_status"] or {})
        except Exception:
            cs = {}
        if not isinstance(cs, dict):
            cs = {}
        overview = " ".join(filter(None, [title, *[str(i) for v in cs.values()
                                              if isinstance(v, list) for i in v[:2]]]))[:300]
        sources_raw = e["source_strands"] or "{}"
        sources = json.loads(sources_raw) if isinstance(sources_raw, str) else sources_raw

        label = f"{NODE_PREFIX} {title[:80]}" if title else f"{NODE_PREFIX} Reality {eid}"
        nid = f"reality_{eid}"
        norm = label.lower().replace(" ", "_")[:64]

        # 构建 timeline
        timeline: list[dict] = []
        for sid, strand_ids in sources.items():
            for strand_id in strand_ids:
                key = f"{sid}/S{strand_id}"
                timeline.append({
                    "session": sid,
                    "strand": strand_id,
                    "turns": topic_ranges.get(key, "?"),
                })
        # 按时序排序
        timeline.sort(key=lambda x: (x["session"], x["strand"]))

        if nid not in seen_nodes:
            nodes.append({
                "label": label,
                "norm_label": norm,
                "file_type": "knowledge",
                "source_file": f"realities/reality_{eid}",
                "source_location": f"realities.reality_id={eid}",
                "_origin": "wiki",
                "id": nid,
                "community": 0,
                "metadata": {
                    "overview": overview[:200],
                    "source_sessions": list(sources.keys()),
                    "strand_count": sum(len(v) for v in sources.values()),
                    "created_at": e["created_at"],
                    "updated_at": e["updated_at"],
                    "timeline": timeline,
                },
            })
            seen_nodes.add(nid)

        # v6.5.4: OV 文档 trace 匹配 — 中文 bigram + log-IDF 加权
        # 匹配条件：有效词重叠 ≥2 且加权分 ≥5.0（有效词 = idf≥2.5，抑制 优化/机制 等泛词）
        _wiki_words = set(_extract_bigrams(title))
        for _doc in ov_doc_titles:
            _doc_label = _doc.get("label", "")
            _doc_uri = _doc.get("uri", "")
            # 2026-08-08：trace 边 target 用 URI 文件段规则 nid（引用已建的全量
            # 节点，不再生成 title 命名旧节点）；fs/tree 不可用 → 回退 _doc["nid"]
            _doc_nid = ov_nid_by_uri.get(_doc_uri) or _doc.get("nid")
            if not _doc_label or not _doc_uri or not _doc_nid:
                continue
            # R6 (B-2): CA 根 disabled → 该文档不建节点也不建边
            # （覆盖建节点 + edges.append 双出口）
            if not ca_root_enabled and _uri_belongs_to_root(_doc_uri, _OV_CA_ROOT):
                continue
            _doc_words = _doc.get("words") or set(_extract_bigrams(_doc.get("title", "")))
            _overlap = _wiki_words & set(_doc_words)
            if not _overlap:
                continue
            _valid = [w for w in _overlap if idf.get(w, 0) >= 2.5]
            _score = sum(idf.get(w, 0) for w in _overlap)
            if len(_valid) >= 2 and _score >= 5.0:
                if _doc_nid not in seen_nodes:
                    # 全量清单可用但该文档不在清单 → 不建节点不建边（防悬挂）
                    if ov_nid_by_uri:
                        continue
                    # fs/tree 不可用降级：保留原 TRACE_SOURCES 逻辑
                    nodes.append({
                        "label": f"{NODE_PREFIX} {_doc_label[:60]}",
                        "norm_label": _doc_label.lower().replace(" ", "_")[:64],
                        "file_type": "knowledge",
                        "source_file": _doc_uri,
                        "source_location": "trace",
                        "_origin": "trace",
                        "id": _doc_nid,
                        "community": 0,
                        "metadata": {"uri": _doc_uri},
                    })
                    seen_nodes.add(_doc_nid)
                edges.append({
                    "source": nid,
                    "target": _doc_nid,
                    "relation": "trace",
                    "confidence": "HIGH",
                    "confidence_score": round(min(_score / 8.0, 0.95), 3),
                    "source_file": "realities",
                    "source_location": f"bigram_match:{_doc_label[:40]}",
                    "weight": round(min(_score / 8.0, 0.95), 3),
                })

        # 归并关系：同一 entry 下的多个 strand 产生内部边
        topic_list: list[str] = []
        for sid, strand_ids in sources.items():
            for strand_id in strand_ids:
                topic_list.append(f"{sid}/S{strand_id}")

        # 从源码 strand 到 reality entry 的"知识提升"边
        for topic_ref in topic_list:
            topic_nid = f"topic_{topic_ref.replace('/', '_')}"
            if topic_nid not in seen_nodes:
                nodes.append({
                    "label": f"Strand {topic_ref}",
                    "norm_label": f"topic_{topic_ref}".lower(),
                    "file_type": "knowledge",
                    "source_file": f"strand_summaries/{topic_ref}",
                    "source_location": topic_ref,
                    "_origin": "wiki",
                    "id": topic_nid,
                    "community": 0,
                })
                seen_nodes.add(topic_nid)

            edges.append({
                "source": topic_nid,
                "target": nid,
                "relation": "merged_into",
                "confidence": "HIGH",
                "confidence_score": 0.95,
                "source_file": "realities",
                "source_location": "strand_to_reality",
                "weight": 1.0,
            })

    # 不跨 entry 建边（先只保留单 entry 内部结构）
    # R6: _meta.ca_root_enabled 供 main 决定是否追加 TRACE_SOURCES trace 边；
    # _meta 不进主图（merge_into_main_graph 只遍历 nodes/edges）。
    return {"nodes": nodes, "edges": edges,
            "_meta": {"ca_root_enabled": ca_root_enabled}}


# 第三轮 T2：strand-topic 节点正则（topic_{session_id}_S{strand_id}）。
# 注意：session_id 含下划线（如 topic_20260619_124810_e21229_S1），
# 故用 .+ 而非 [^_]+；配合 file_type=="knowledge" 过滤，代码 AST 节点
# （topic_manager_*，file_type=code/rationale）天然豁免，严禁误删。
_STRAND_TOPIC_RE = re.compile(r"^topic_.+_S\d+$")


def _load_db_knowledge_reference(db_path: str):
    """读 realities 表 → (reality_ids, strand_topic_ids 引用集)。

    Returns:
        (set, set) 或 None（DB 读失败 → 调用方跳过清理，不产生破坏性删除）。
    """
    reality_ids: set = set()
    strand_topics: set = set()
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            rows = conn.execute(
                "SELECT reality_id, source_strands FROM realities"
            ).fetchall()
            for rid, src_json in rows:
                reality_ids.add(rid)
                try:
                    sources = json.loads(src_json) if src_json else {}
                except (json.JSONDecodeError, TypeError):
                    sources = {}
                if isinstance(sources, dict):
                    for sid, strand_ids in sources.items():
                        for strand_id in strand_ids or []:
                            strand_topics.add(f"topic_{sid}_S{strand_id}")
        finally:
            conn.close()
    except Exception as exc:
        print(f"  [WIKI_GRAPH] knowledge reference load failed: {exc}")
        return None
    return reality_ids, strand_topics


def _clean_knowledge_domain(main: dict, db_path: str) -> tuple[int, int]:
    """merge 前一致性清理：knowledge 域以 realities 表为唯一权威。

    - reality_{id} 不在 DB → 删除（含其所有边；如僵尸 reality_420 连带 cooc 边）
    - theme_* 节点 → 全删（themes 冻结，34 v2.3 清残留，含 17 条 theme_ 相关边）
    - strand-topic（topic_{session_id}_S{strand_id} 格式且 file_type=knowledge）
      不在 DB source_strands
      引用集 → 删除（先建后删顺序由调用方保证：清理 → merge 全量替换）
    - ov_doc_* 节点不在 fs/tree 全量清单 → 删除（2026-08-08：清掉 frontmatter
      title 命名的旧节点，如 ov_doc_决策_38：...；清单为空=fs/tree 不可用 → 跳过）
    - 悬挂边（source/target 无对应节点）删除
    - code AST 节点（file_type != knowledge）一律不动（T2 红线）

    Returns:
        (removed_nodes, removed_edges)；DB 不可读 → (0, 0) 跳过清理。
    """
    ref = _load_db_knowledge_reference(db_path)
    if ref is None:
        print("  [WIKI_GRAPH] cleanup skipped (DB unavailable)")
        return (0, 0)
    reality_ids, strand_topics = ref

    # fs/tree 全量清单为 ov_doc 权威；不可用（空清单）→ 跳过该规则（保守）
    ov_docs = _load_all_ov_docs()
    ov_nids: set = {d["nid"] for d in ov_docs} if ov_docs else set()
    if not ov_docs:
        print("  [WIKI_GRAPH] ov_doc cleanup skipped (fs/tree unavailable)")

    nodes = main.get("nodes", [])
    links = main.get("links", [])

    removed_ids: set = set()
    for n in nodes:
        nid = n.get("id", "")
        if n.get("file_type") != "knowledge":
            continue
        if nid.startswith("reality_"):
            try:
                rid = int(nid.split("_", 1)[1])
            except (ValueError, IndexError):
                continue
            if rid not in reality_ids:
                removed_ids.add(nid)
        elif nid.startswith("theme_"):
            removed_ids.add(nid)
        elif _STRAND_TOPIC_RE.match(nid) and nid not in strand_topics:
            removed_ids.add(nid)
        elif ov_nids and nid.startswith("ov_doc_") and nid not in ov_nids:
            removed_ids.add(nid)

    if removed_ids:
        main["nodes"] = [n for n in nodes if n.get("id") not in removed_ids]
        links = main.get("links", [])

    node_ids = {n.get("id") for n in main["nodes"]}
    kept = [l for l in links
            if l.get("source") in node_ids and l.get("target") in node_ids]
    removed_edges = len(links) - len(kept)
    main["links"] = kept
    return (len(removed_ids), removed_edges)


def merge_into_main_graph(subgraph: dict, main_path: str = GRAPH_JSON,
                          db_path: str = CA_TOPICS_DB) -> bool:
    """将 wiki 子图合并到主 graph.json。

    第三轮 T2：合并前对主图 knowledge 域做一致性清理（realities 表为唯一权威，
    清僵尸 reality / theme 残留 / 悬挂边），然后 merge。
    不会覆盖已有 AST 节点。幂等——已存在的 wiki 节点不重复添加。
    """
    if not os.path.exists(main_path):
        print(f"[WIKI_GRAPH] main graph not found at {main_path}, skipping merge")
        return False

    try:
        with open(main_path) as f:
            main = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WIKI_GRAPH] main graph load failed: {exc}, skipping merge")
        return False

    removed_nodes, removed_edges = _clean_knowledge_domain(main, db_path)
    if removed_nodes or removed_edges:
        print(f"[WIKI_GRAPH] knowledge cleanup: -{removed_nodes} nodes, "
              f"-{removed_edges} edges")

    existing_ids = {n["id"] for n in main.get("nodes", [])}
    added_nodes = 0
    added_edges = 0

    for n in subgraph.get("nodes", []):
        if n["id"] not in existing_ids:
            main["nodes"].append(n)
            existing_ids.add(n["id"])
            added_nodes += 1

    # 边去重（graph.json 用 links 不是 edges）
    main_links = main.get("links", [])
    existing_edges = {(e["source"], e["target"], e.get("relation", ""))
                      for e in main_links}
    for e in subgraph.get("edges", []):
        key = (e["source"], e["target"], e.get("relation", ""))
        if key not in existing_edges:
            main.setdefault("links", []).append(e)
            existing_edges.add(key)
            added_edges += 1

    if removed_nodes or removed_edges or added_nodes > 0 or added_edges > 0:
        with open(main_path, "w") as f:
            json.dump(main, f, ensure_ascii=False, indent=2)
        print(f"[WIKI_GRAPH] merged: +{added_nodes} nodes, +{added_edges} edges"
              f" (cleanup: -{removed_nodes} nodes, -{removed_edges} edges)")
    else:
        print(f"[WIKI_GRAPH] no changes")

    return True


def main():
    os.makedirs(GRAPHIFY_OUT, exist_ok=True)
    subgraph = build_wiki_subgraph()

    # 追加 trace 边（决策↔代码追溯；CA 根 disabled → 门控跳过，R6）
    seen_nodes = {n["id"] for n in subgraph["nodes"]}
    _add_trace_edges(subgraph, seen_nodes,
                     ca_root_enabled=subgraph.get("_meta", {}).get("ca_root_enabled", True))

    # 写子图文件
    with open(WIKI_GRAPH_FILE, "w") as f:
        json.dump(subgraph, f, ensure_ascii=False, indent=2)
    print(f"[WIKI_GRAPH] wiki subgraph: {len(subgraph['nodes'])} nodes, {len(subgraph['edges'])} edges")
    print(f"  → {WIKI_GRAPH_FILE}")

    # 合并到主图
    if os.path.exists(GRAPH_JSON):
        merge_into_main_graph(subgraph)
        print(f"  → merged into {GRAPH_JSON}")

        # 构建 wiki 关联
        _build_associations()


def _build_associations():
    """从 graph.json 为 wiki entry 建关联表。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from ca.store import build_wiki_associations
        n = build_wiki_associations(GRAPH_JSON)
        print(f"  → associations: {n} links")
    except Exception as exc:
        print(f"  → associations skipped: {exc}")


if __name__ == "__main__":
    main()
