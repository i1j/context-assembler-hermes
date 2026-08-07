"""ca/fact_linking.py — 精炼轮 Step 8.5 事实关联建边（决策 34 v2.4/v2.6/v2.7）。

四类边：shares_topic（弱，信号 A 本轮暂缓）/ depends_on（强）/ continues（强）/
references_ov（reality↔OV 项目数据）。本轮实现：
  - 信号 B（L2 4B）：reality 内容含承接动词「基于/承接/详见/延续/参照」→
    与有文本关联的另一 reality 成对 → 4B 判定 depends_on / continues / 无关联
    （解析失败重试一次，仍败跳过该候选并记日志；信号 B 不上 L3）
  - 信号 C（L1 OV 语义检索）：name/hdl → POST /api/v1/search/find →
    top-1 命中且相似度 ≥ 阈值 → references_ov 边（target 命名对齐
    build_wiki_subgraph 的 ov_doc_{label} 约定，U-6）
    相似度阈值无先验（34 v2.7）：默认探测模式（跑 10 reality 小样本记录命中
    分布到日志，不落图）；CA_OV_REFERENCE_THRESHOLD 配置后才按阈值落图。
关联层不上 L3。新增边数记 refinement_meta.associations_added。
依赖决策 42 R-1（图路消费 depends_on/continues/co_occurs_with 边）。

硬约束：graph.json 读写加锁 + 容错（文件不存在/解析失败 → 跳过不崩）；
路径用 Path(__file__) 相对，禁止硬编码家目录。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 信号 B：承接/引用动词（34 v2.4 实测 11 条候选）──
LINK_VERBS = ("基于", "承接", "详见", "延续", "参照")

# ── 信号 C：OV 语义检索（L1 API）──
OV_API = os.getenv("OV_API", "http://127.0.0.1:1933")
OV_SEARCH_PATH = "/api/v1/search/find"
# 相似度阈值无先验（34 v2.7）：默认 None = 探测模式（记录命中分布，不落图）；
# 小样本验证后可经 CA_OV_REFERENCE_THRESHOLD 配置（勿写死未验证值）。
_OV_THRESHOLD_RAW = os.getenv("CA_OV_REFERENCE_THRESHOLD", "").strip()
OV_REFERENCE_THRESHOLD: Optional[float] = (
    float(_OV_THRESHOLD_RAW) if _OV_THRESHOLD_RAW else None)
# 信号 C 小样本探测规模（34 v2.7：先 10 reality 看 top-1 命中合理性）
OV_PROBE_SAMPLE = 10
# 探测连续失败上限：OV 检索慢/不稳（实测 8s 超时）——连续失败即停止探测，防挂死
OV_PROBE_FAIL_LIMIT = 3
# OV 检索超时（实测 8s 超时，这里放宽并允许 env 覆盖）
OV_SEARCH_TIMEOUT = float(os.getenv("CA_OV_SEARCH_TIMEOUT", "15"))

# graph.json 写锁（硬约束 5：读写加锁）
_GRAPH_LOCK = threading.Lock()


# ── 信号 C：profile 过滤（2026-08-07 用户约束）──
# references_ov 边允许非 CA 项目资源（resources/projects/windows/... 等），
# 但禁止**其它 profile** 的资源（user/winker/、topics/sysadmin/、
# resources/winker/...）——profile 边界外数据不得混入图。
# profile 名单运行时扫描 ~/.hermes/profiles/（不硬编码）。

def _known_profiles() -> set:
    """扫描本地已安装的 Hermes profile 名单。

    ~/.hermes/profiles/ 下混有非 profile 目录（.git/ca_cache/scripts 等，
    虽有 config.yaml/memories 但结构不完整）。用 auth.json 判 profile 身份
    （真 profile 必有，2026-08-07 实测 tester/winker/sysadmin/pmgr 有、
    .git/ca_cache/scripts 无）。
    """
    try:
        profiles_root = Path.home() / ".hermes" / "profiles"
        if profiles_root.is_dir():
            out = set()
            for p in profiles_root.iterdir():
                if p.is_dir() and (p / "auth.json").is_file():
                    out.add(p.name)
            return out
    except OSError:
        pass
    return set()


_PROFILES_CACHE: Optional[frozenset] = None


def known_profiles() -> set:
    """带缓存扫描（进程内 profile 名单不变）。"""
    global _PROFILES_CACHE
    if _PROFILES_CACHE is None:
        _PROFILES_CACHE = frozenset(_known_profiles())
    return set(_PROFILES_CACHE)


def current_profile() -> Optional[str]:
    """当前 profile 名：HERMES_HOME 尾段（多 profile 环境事实源）。"""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).name
    try:
        from hermes_constants import get_hermes_home  # 生产 daemon 内可用
        return Path(get_hermes_home()).name
    except Exception:
        return None


def filter_foreign_profiles(hits: List[dict]) -> List[dict]:
    """剔除其它 profile 的命中；保留本 profile 与无 profile 段（共享资源）命中。

    用户约束（2026-08-07）：references_ov 允许非 CA 项目资源，但禁止其它
    profile 资源。uri 形态：
      viking://user/<profile>/...        → user 后第一段 = profile
      viking://topics/<profile>/...      → topics 后第一段 = profile
      viking://resources/<profile>/...   → resources 后第一段 = profile
                                           （resources/projects/... 除外——共享项目）
      viking://resources/projects/...    → 无 profile 段 → 保留（含非 CA 项目）
    """
    if not hits:
        return hits
    profs = known_profiles()
    if not profs:
        return hits  # 扫描不到名单 → 不过滤（保守）
    mine = current_profile()
    out = []
    for h in hits:
        uri = str(h.get("uri") or "")
        if not uri or not uri.startswith("viking://"):
            out.append(h)  # 无 uri / 非 viking → 无法判定，保留
            continue
        segs = uri[len("viking://"):].split("/")
        profile_seg = None
        for i, s in enumerate(segs):
            if s in ("user", "topics") and i + 1 < len(segs):
                profile_seg = segs[i + 1]
                break
            if s == "resources" and i + 1 < len(segs) and segs[i + 1] != "projects":
                cand = segs[i + 1]
                if cand in profs:
                    profile_seg = cand
                    break
        if profile_seg is None:
            out.append(h)  # 共享项目/架构资源 → 保留
        elif mine and profile_seg == mine:
            out.append(h)  # 本 profile → 保留
        # 其它 profile → 剔除
    return out


def _graph_path() -> Path:
    return Path(__file__).resolve().parent.parent / "graphify-out" / "graph.json"


def _load_graph() -> Optional[dict]:
    """读 graph.json（容错：不存在/解析失败 → None，跳过不崩）。"""
    try:
        with open(_graph_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("[CA_L4] fact_linking: graph.json load failed: %s", exc)
        return None


def _save_graph(graph: dict) -> bool:
    try:
        with open(_graph_path(), "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.warning("[CA_L4] fact_linking: graph.json save failed: %s", exc)
        return False


def _add_edges(new_edges: List[dict]) -> int:
    """向 graph.json 幂等追加边（锁内读-改-写；返回实际新增数）。"""
    if not new_edges:
        return 0
    added = 0
    with _GRAPH_LOCK:
        graph = _load_graph()
        if graph is None:
            return 0
        links = graph.setdefault("links", [])
        existing = {(l.get("source"), l.get("target"), l.get("relation"))
                    for l in links}
        for e in new_edges:
            key = (e["source"], e["target"], e["relation"])
            if key not in existing:
                links.append(e)
                existing.add(key)
                added += 1
        if added:
            if not _save_graph(graph):
                return 0
    return added


# ── 信号 B：承接/延续判定（L2 4B）──

FACT_LINK_PROMPT = """你是工作线关系判定助手。给定两个现实工作对象（reality），判断 src 与 tgt 的工作关系。

【reality 定义】现实工作对象（工作线）：多个语义独立但工作中有关联的 strand 的集合，跨话题块持续演进。current_status 四段：current_state（现状）/ key_facts（持久事实）/ goals（进行中目标）/ context（相关资源）。

【判定规则】
1. depends_on：src 的工作基于/依赖 tgt 的产出（承接其方案、参考其结论、依赖其状态）
2. continues：src 延续 tgt 的同一工作线继续推进（同一任务的不同阶段）
3. none：两者无工作关联（如同领域不同任务）

【src reality】
name: {src_name}
hdl: {src_hdl}
current_status: {src_cs}

【tgt reality】
name: {tgt_name}
hdl: {tgt_hdl}
current_status: {tgt_cs}

【输出格式】（严格 JSON，不要其他文字）
{{"relation": "depends_on" | "continues" | "none"}}"""


def _reality_text(reality: dict) -> str:
    """reality 可比较文本：name + hdl + current_status 各段。"""
    parts = [str(reality.get("name") or ""), str(reality.get("hdl") or "")]
    cs = reality.get("current_status") or {}
    if isinstance(cs, dict):
        for v in cs.values():
            if isinstance(v, list):
                parts.extend(str(i) for i in v if str(i).strip())
    return " ".join(parts)


def _bigrams(text: str) -> set:
    """中英混合 bigram（中文连续 2 字 + 英文 token）。"""
    out = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(chunk) - 1):
            out.add(chunk[i:i + 2])
    for m in re.findall(r"[A-Za-z]{2,}", text or ""):
        out.add(m.lower())
    return out


def build_link_candidates(realities: List[dict]) -> List[Tuple[dict, dict]]:
    """信号 B 候选：含承接动词的 reality × 有文本关联的另一 reality 成对。

    承接动词命中（name/hdl/current_status）→ 与共享 ≥1 bigram 的其他 reality
    成对（src = 含动词方，有向判定）。
    """
    pairs: List[Tuple[dict, dict]] = []
    for r in realities:
        text = _reality_text(r)
        if not any(v in text for v in LINK_VERBS):
            continue
        words = _bigrams(text)
        for other in realities:
            if other.get("reality_id") == r.get("reality_id"):
                continue
            if words & _bigrams(_reality_text(other)):
                pairs.append((r, other))
    return pairs


def _parse_relation(text: Optional[str]) -> Optional[str]:
    """宽松解析 4B 判定输出 → depends_on/continues/none。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t.replace("```", "")
        t = t.rsplit("```", 1)[0].strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        for rel in ("depends_on", "continues", "none"):
            if rel in t:
                return rel
        return None
    try:
        data = json.loads(t[i:j + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    rel = data.get("relation") if isinstance(data, dict) else None
    if rel in ("depends_on", "continues", "none"):
        return rel
    return None


def build_fact_link_prompt(src: dict, tgt: dict) -> str:
    return FACT_LINK_PROMPT.format(
        src_name=json.dumps(src.get("name") or "", ensure_ascii=False),
        src_hdl=json.dumps(src.get("hdl") or "", ensure_ascii=False),
        src_cs=json.dumps(src.get("current_status") or {}, ensure_ascii=False),
        tgt_name=json.dumps(tgt.get("name") or "", ensure_ascii=False),
        tgt_hdl=json.dumps(tgt.get("hdl") or "", ensure_ascii=False),
        tgt_cs=json.dumps(tgt.get("current_status") or {}, ensure_ascii=False),
    )


def _judge_link_relation(src: dict, tgt: dict) -> Optional[str]:
    """信号 B L2 判定（复用 4B 调用模式；解析失败重试一次，仍败 → None）。

    对齐 34 v2.2 升级原则的 L2 部分：信号 B 不上 L3，无「升 L3」路径。
    """
    from .topic_summary import call_llm_raw

    prompt = build_fact_link_prompt(src, tgt)
    for attempt in range(2):
        try:
            raw = call_llm_raw(prompt, temperature=0.2 if attempt == 0 else 0.1,
                               max_retries=1)
        except Exception as exc:
            logger.warning("[CA_L4] fact_linking 4B call failed: %s", exc)
            return None
        rel = _parse_relation(raw)
        if rel is not None:
            return rel
        logger.warning("[CA_L4] fact_linking 4B parse failed (attempt %d/%d), "
                       "skip candidate", attempt + 1, 2)
    return None


# ── 信号 C：reality↔OV 语义检索（L1 API）──

def _extract_ov_hits(data: Any) -> Optional[List[dict]]:
    """宽松提取 OV 检索命中列表（响应结构防御式解析，勿假设单一 schema）。

    已适配 OV /api/v1/search/find 真实响应（2026-08-07 实测）：
      {"status":"ok","result":{"memories":[...],"resources":[...],"skills":[...],"total":N}}
    顶层键仅 status/result——旧实现只查顶层 list 键导致恒空（references_ov
    边 0 条、探测连续 3 次失败的根因）。
    """
    if isinstance(data, dict):
        # ① result.* 嵌套（OV search/find 真实结构）。
        #    resources 优先：references_ov 边目标是 OV 项目文档资源；
        #    memories（用户/agent 记忆）与 skills 仅兜底（不应用作边目标）。
        result = data.get("result")
        if isinstance(result, dict):
            for key in ("resources", "memories", "skills", "hits", "results",
                        "items", "matches", "documents"):
                v = result.get(key)
                if isinstance(v, list) and v:
                    return [d for d in v if isinstance(d, dict)]
            hit = result.get("hit")
            if isinstance(hit, dict):
                return [hit]
        # ② 顶层 list 键（宽松兼容其它响应形态）
        for key in ("hits", "results", "items", "matches", "documents"):
            v = data.get(key)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
        hit = data.get("hit")
        if isinstance(hit, dict):
            return [hit]
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return None


def _hit_score(hit: dict) -> float:
    for key in ("score", "similarity", "relevance", "confidence"):
        v = hit.get(key)
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _ov_doc_nid(hit: dict) -> Optional[str]:
    """references_ov 边 target 命名：对齐 build_wiki_subgraph 的 ov_doc_{label}（U-6）。

    2026-08-07 适配：OV search/find 命中是 viking:// 资源（无 title/name 字段，
    只有 uri）。uri 常指向**文档碎片/隐藏摘要**（如
      .../34-idle-refinement/34-idle-refinement.md/背景.md
      .../34-idle-refinement/34-idle-refinement.md/.overview.md
      .../37-reality-restructure.md/决策_37CA_Reali...
    ）——旧实现取 uri 尾段 → 生成 ov_doc_背景.md / ov_doc_.overview.md 等
    **悬挂节点**（图内无此 id）。修复：取 uri 中**第一个非隐藏 .md 段**
    （主文档，跳过 .overview/.abstract 等隐藏摘要与碎片），并解析到图内
    source_file 尾段一致的 ov_doc 节点；解析不到 → None（宁缺勿错，防悬挂）。
    """
    label = (hit.get("title") or hit.get("name") or hit.get("label")
             or hit.get("path") or hit.get("uri") or hit.get("id") or "")
    label = str(label).strip()
    if not label:
        return None

    # ① uri 形态：取第一个非隐藏 .md 段（主文档名）
    if label.startswith("viking://") or "/" in label:
        parts = [p for p in label.rstrip("/").split("/") if p]
        for p in parts:
            if p.endswith(".md") and not p.startswith("."):
                label = p
                break
        else:
            # 无主文档段（如 .../34-idle-refinement/.overview.md）→ 无稳定锚点
            return None
    # ② 非 uri 形态（title/name/label 直接给出）→ 原逻辑
    return f"ov_doc_{label.lower().replace(' ', '_')[:48]}"


def _ov_search_find(query: str) -> Optional[List[dict]]:
    """OV 语义检索 top 命中（L1 API；超时/失败 → None，降级跳过不崩）。

    2026-08-07：结果经 filter_foreign_profiles 剔除其它 profile 命中
    （用户约束：references_ov 允许非 CA 项目，禁止其它 profile 资源）。
    """
    import urllib.request

    url = f"{OV_API.rstrip('/')}{OV_SEARCH_PATH}"
    body = json.dumps({"query": query}).encode()
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=OV_SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[CA_L4] fact_linking OV search failed (query=%.30s): %s",
                       query, exc)
        return None
    hits = _extract_ov_hits(data)
    if hits:
        hits = filter_foreign_profiles(hits)
    return hits


def _run_ov_references(realities: List[dict]) -> int:
    """信号 C 主流程：探测模式（默认）或按阈值落图（配置后）。

    阈值无先验（34 v2.7）：先跑 OV_PROBE_SAMPLE(10) reality 小样本，记录 top-1
    命中与相似度分布到日志（供人工判合理性）；CA_OV_REFERENCE_THRESHOLD 配置后
    对全部 reality 检索并按阈值建 references_ov 边。
    """
    if OV_REFERENCE_THRESHOLD is None:
        # 探测模式：不落图
        hits: List[tuple] = []
        fails = 0
        for r in realities[:OV_PROBE_SAMPLE]:
            query = " ".join(filter(None, [
                str(r.get("name") or ""), str(r.get("hdl") or "")]))[:200]
            if not query.strip():
                continue
            top = _ov_search_find(query)
            if not top:
                fails += 1
                if fails >= OV_PROBE_FAIL_LIMIT:
                    logger.info("[CA_L4] fact_linking OV 探测中断：连续 %d 次失败"
                                "（OV 检索不可用，降级跳过）", fails)
                    break
                continue
            fails = 0
            hits.append((r.get("reality_id"), _hit_score(top[0]),
                         str(top[0].get("uri") or top[0].get("title")
                             or top[0].get("name") or "")[-60:]))
        if hits:
            scores = sorted(h[1] for h in hits)
            logger.info("[CA_L4] fact_linking OV 探测（阈值未验证，不落图）: "
                        "%d 命中，score min=%.3f med=%.3f max=%.3f，样例=%s",
                        len(hits), scores[0], scores[len(scores) // 2],
                        scores[-1], hits[:3])
        else:
            logger.info("[CA_L4] fact_linking OV 探测: 无命中（OV 检索失败/超时，"
                        "降级跳过）")
        return 0

    edges: List[dict] = []
    # 图内已有 ov_doc 节点集合（防悬挂：references_ov 只连已入图文档，
    # 未入图文档宁缺勿错——34 v2.7 信号 C 边界）
    known_ov = set()
    try:
        g = _load_graph()
        if g:
            known_ov = {n.get("id") for n in g.get("nodes", [])
                        if str(n.get("id", "")).startswith("ov_doc_")}
    except Exception:
        pass
    if not known_ov:
        logger.info("[CA_L4] fact_linking OV 落图跳过：图内无 ov_doc 节点（先重建 wiki 子图）")
        return 0

    for r in realities:
        query = " ".join(filter(None, [
            str(r.get("name") or ""), str(r.get("hdl") or "")]))[:200]
        if not query.strip():
            continue
        top = _ov_search_find(query)
        if not top:
            continue
        score = _hit_score(top[0])
        if score < OV_REFERENCE_THRESHOLD:
            continue
        tgt = _ov_doc_nid(top[0])
        if not tgt:
            continue
        if tgt not in known_ov:
            # 命中文档未入图 → 不建悬挂边（宁缺勿错）
            logger.debug("[CA_L4] fact_linking OV 命中未入图，跳过: %s → %s",
                         r.get("reality_id"), tgt)
            continue
        edges.append({
            "source": f"reality_{r.get('reality_id')}",
            "target": tgt,
            "relation": "references_ov",
            "confidence": "MEDIUM",
            "confidence_score": round(min(score, 0.95), 3),
            "source_file": "realities",
            "source_location": f"ov_search:{r.get('reality_id')}",
            "_origin": "fact_linking",
            "weight": round(min(score, 0.95), 3),
        })
    return _add_edges(edges)


def run_fact_linking(conn, realities: List[dict]) -> int:
    """Step 8.5 事实关联建边：信号 B（L2 4B）+ 信号 C（L1 OV）。

    Args:
        conn: ca_topics.db 连接（保留用于未来扩展/审计，当前不写 DB）
        realities: [{reality_id, name, hdl, current_status}]

    Returns:
        新增边数（供 refinement_meta.associations_added 记账）。
    """
    added = 0

    # ── 信号 B：承接/延续判定（L2 4B，不上 L3）──
    b_edges: List[dict] = []
    for src, tgt in build_link_candidates(realities):
        rel = _judge_link_relation(src, tgt)
        if rel in ("depends_on", "continues"):
            b_edges.append({
                "source": f"reality_{src.get('reality_id')}",
                "target": f"reality_{tgt.get('reality_id')}",
                "relation": rel,
                "confidence": "MEDIUM",
                "confidence_score": 0.7,
                "source_file": "realities",
                "source_location": (
                    f"fact_linking:{src.get('reality_id')}"
                    f"->{tgt.get('reality_id')}"),
                "_origin": "fact_linking",
                "weight": 1.0,
            })
        elif rel is None:
            logger.info("[CA_L4] fact_linking: pair %s→%s 判定失败，跳过",
                        src.get("reality_id"), tgt.get("reality_id"))
    added += _add_edges(b_edges)
    if b_edges:
        logger.info("[CA_L4] fact_linking 信号 B: +%d 边（depends_on/continues）",
                    len(b_edges))

    # ── 信号 C：reality↔OV 语义检索（L1 API）──
    added += _run_ov_references(realities)
    return added
