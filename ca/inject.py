"""注入侧 4B 拣选（决策 38 §四②，2026-08-03）。

替代 query_themes_by_semantics 的"余弦 top-N 正向选择"：
  提问 → ① 向量负向排除（d > 0.55 剔除，仅缩小 4B 输入）
       → ② 4B 拣选 top-K（工作关联判定，允许空集 = 空注入）
       → ③ 返回注入集（兼容 theme dict 格式，供 _format_wiki_carryover）
       → ④ 4B 失败 → 退化为余弦（v6.5 行为，保持可用）

设计对照（决策 38 / 38-inject-prompt.md）:
  - 空注入显式化：全新话题 → 4B 输出 [] → 注入 []（宁缺勿错，防 P5 硬选幻觉）
  - index 引用候选（防 reality_id 幻觉越界）
  - relevance 理由必填（审计日志 + 空注入率健康指标）
  - 向量只做负向排除（彻底弃 sim 思维：向量不承担正向选择）
"""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 负向排除阈值（决策 37 §5.1 原型验证值）：cos < 0.45（d > 0.55）剔除
NEG_EXCLUDE_COS = 0.45
# 给 4B 的候选预算上限（防止候选过多撑爆 prompt）
MAX_CANDIDATES = 15
# 注入拣选 4B 输出预算
INJECT_MAX_TOKENS = 600

INJECT_PROMPT = """你是上下文检索器。给定用户的当前提问和候选现实工作对象（reality）列表，拣选出与提问最相关的 top-3 reality 作为注入上下文。

【reality 定义】现实工作对象（工作线）：多个语义独立但工作中有关联的 strand 的集合，跨话题块持续演进。其当前状态由 current_status 描述（goals=进行中的目标 / state=现状）。

【用户提问】
{query}

【候选 reality】（已按语义负向排除，仅保留可能与提问相关的）
{candidates}

【拣选规则】
1. 相关性判定：该 reality 的 goals/state 是否与提问的工作对象承接/相关？用户问这个提问时，是否需要该 reality 的背景才能有效回答？
2. 宁缺勿错：若没有任何 reality 与提问相关（全新话题），必须输出空列表——空注入是合法且正确的结果，禁止硬选"最不无关"的 reality。
3. 数量：0~3 个，按相关度降序。
4. 只能引用候选列表中的 index，禁止编造列表外的 reality。

【输出格式】（严格 JSON，不要 markdown 围栏）
{{"selected": [{{"index": 0, "relevance": "承接理由（中文，说明与提问的工作关联）", "priority": 1}}]}}
全新话题 → {{"selected": []}}"""


def build_inject_prompt(query: str, candidates: list[dict]) -> str:
    """候选格式化 → 注入拣选 prompt（38-inject-prompt.md 草稿落地）。"""
    lines = []
    for i, c in enumerate(candidates):
        title = c.get("title", "")
        hdl = c.get("hdl", "")
        goals = c.get("goals") or []
        state = c.get("state") or []
        parts = [f"[{i}] {title[:60]}"]
        if hdl:
            parts.append(f"    hdl: {str(hdl)[:80]}")
        if goals:
            parts.append(f"    goals: {' | '.join(str(g)[:60] for g in goals[:3])}")
        if state:
            parts.append(f"    state: {' | '.join(str(s)[:60] for s in state[:3])}")
        lines.append("\n".join(parts))
    return INJECT_PROMPT.format(query=query[:500], candidates="\n\n".join(lines))


def parse_inject_response(text: Optional[str], n_candidates: int) -> Optional[list]:
    """宽松解析 4B 拣选结果 → index 列表。

    Returns:
        list[int]: 合法 selected index（0 <= idx < n_candidates，按 priority 排序）
        []: 4B 明确空注入（全新话题）
        None: 解析失败（调用方应 fallback 余弦）
    """
    if not text:
        return None
    t = text.strip()
    # 剥离 markdown 围栏
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t.replace("```", "")
        t = t.rsplit("```", 1)[0].strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(t[i:j + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    selected = data.get("selected")
    if selected is None:
        return None
    if not isinstance(selected, list):
        return None
    result = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        idx_raw = item.get("index")
        if idx_raw is None:
            continue
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_candidates and idx not in result:
            result.append(idx)
    return result


def _exclude_session(rows, exclude_session_id: Optional[str]) -> list:
    """剔除 source_strands 含当前 session 的 theme（防自注入，v6.5.2）。

    CR-13: 依赖 pick_injection_themes 的 SELECT 列序 ——
    (theme_id, title, overview, ooda_json, key_facts_json, open_items_json,
     centroid_json, source_strands) → source_strands 是第 8 列（index 7）。
    调整该查询列序时必须同步下方 row[7] 索引。
    """
    if not exclude_session_id:
        return rows
    filtered = []
    for row in rows:
        ss_raw = row[7]  # source_strands
        hit = False
        if ss_raw:
            try:
                ss = json.loads(ss_raw)
                if isinstance(ss, dict) and exclude_session_id in ss:
                    hit = True
            except (json.JSONDecodeError, TypeError):
                pass
        if not hit:
            filtered.append(row)
    return filtered


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def pick_injection_themes(
    query: str,
    q_emb: list,
    profile: str,
    limit: int = 3,
    exclude_session_id: Optional[str] = None,
    db_path=None,
) -> list[dict]:
    """注入拣选主流程（决策 38 §四②）。

    ① 向量负向排除（d > 0.55 剔除，缩小 4B 输入）
    ② 4B 拣选 top-K（允许空 = 空注入）
    ③ 4B 失败 → 退化余弦（query_themes_by_semantics）

    Returns:
        theme dict 列表（theme_id/title/overview/ooda/key_facts/open_items），
        空列表 = 空注入（合法）或全库无匹配。
    """
    from ca.store import _get_topic_conn

    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT theme_id, title, overview, ooda_json, key_facts_json, "
            "       open_items_json, centroid_json, source_strands "
            "FROM themes "
            "WHERE centroid_json IS NOT NULL AND centroid_json != '' "
            "  AND profile = ? "
            "ORDER BY updated_at DESC",
            (profile,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        logger.warning("[CA_INJECT] read themes failed: %s", exc)
        return []
    rows = _exclude_session(rows, exclude_session_id)
    if not rows:
        return []

    # ① 负向排除：cos < 0.45 剔除（向量只做排除，不承担选择）
    cands: list[dict] = []
    for row in rows:
        try:
            centroid = json.loads(row[6]) if row[6] else []
        except (json.JSONDecodeError, TypeError):
            continue
        cos = _cosine(q_emb, centroid)
        if cos < NEG_EXCLUDE_COS:
            continue
        ooda = {}
        try:
            ooda = json.loads(row[3]) if row[3] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        cands.append({
            "index": len(cands),
            "theme_id": row[0],
            "title": row[1] or "",
            "overview": row[2] or "",
            "hdl": (ooda.get("hdl") or (row[1] or "")),
            "goals": ooda.get("goals") or [],
            "state": (ooda.get("current_state")
                      or ooda.get("现象与问题") or [])[:3],
            "ooda": ooda,
            "key_facts": json.loads(row[4]) if row[4] else [],
            "open_items": json.loads(row[5]) if row[5] else [],
        })
    if not cands:
        return []

    # 候选预算截断（给 4B 的输入上限）
    cands = cands[:MAX_CANDIDATES]

    # ② 4B 拣选
    try:
        from ca.topic_summary import call_llm_raw
        prompt = build_inject_prompt(query, cands)
        raw = call_llm_raw(prompt, num_predict=INJECT_MAX_TOKENS,
                           temperature=0.1, max_retries=1)
    except Exception as exc:
        logger.warning("[CA_INJECT] LLM call failed: %s", exc)
        raw = None

    if raw is not None:
        picked = parse_inject_response(raw, len(cands))
        if picked is not None:
            # picked=[] → 空注入（合法）；非空 → 按候选顺序返回
            picked_set = set(picked)
            result = [
                {"theme_id": c["theme_id"], "title": c["title"],
                 "overview": c["overview"], "ooda": c["ooda"],
                 "key_facts": c["key_facts"], "open_items": c["open_items"]}
                for c in cands if c["index"] in picked_set
            ]
            result = result[:limit]
            logger.info("[CA_INJECT] 4B picked %d/%d themes (query=%.40s)",
                        len(result), len(cands), query)
            return result
        logger.info("[CA_INJECT] 4B parse failed → cosine fallback")
    else:
        logger.info("[CA_INJECT] 4B unavailable → cosine fallback")

    # ④ fallback：余弦 top-N（v6.5 行为）
    try:
        from ca.store import query_themes_by_semantics
        return query_themes_by_semantics(
            q_emb, profile, limit=limit,
            exclude_session_id=exclude_session_id, db_path=db_path)
    except Exception as exc:
        logger.warning("[CA_INJECT] cosine fallback failed: %s", exc)
        return []


# ══════════════════════════════════════════════════════════════════════
# v7 reality 注入拣选（决策 41 §2.4b：提问云形心散度距离，2026-08-07）
# ══════════════════════════════════════════════════════════════════════

# 提问云形心距离范围安全网（θ_max=0.5：归属对召回 93%，实测）
THETA_MAX = 0.5
# 距离排序后给 4B 的候选预算（4B 输入均值 14.9）
QUERY_CLOUD_TOP_K = 15

INJECT_PROMPT_REALITY = """你是上下文检索器。给定用户的当前提问和候选现实工作对象（reality）列表，拣选出与提问最相关的 top-3 reality 作为注入上下文。

【reality 定义】现实工作对象（工作线）：多个语义独立但工作中有关联的 strand 的集合，跨话题块持续演进。其当前状态由 current_status 描述（goals=进行中的目标 / current_state=现状 / key_facts=持久事实）。

【用户提问】
{query}

【候选 reality】（已按提问云形心散度距离预筛，仅保留距离范围内的）
{candidates}

【拣选规则】
1. 相关性判定：该 reality 的 goals/current_state 是否与提问的工作对象承接/相关？用户问这个提问时，是否需要该 reality 的背景才能有效回答？
2. 宁缺勿错：若没有任何 reality 与提问相关（全新话题），必须输出空列表——空注入是合法且正确的结果，禁止硬选"最不无关"的 reality。
3. 数量：0~3 个，按相关度降序。
4. 只能引用候选列表中的 index，禁止编造列表外的 reality。

【输出格式】（严格 JSON，不要 markdown 围栏）
{{"selected": [{{"index": 0, "relevance": "承接理由（中文，说明与提问的工作关联）", "priority": 1}}]}}
全新话题 → {{"selected": []}}"""


def build_inject_prompt_reality(query: str, candidates: list[dict]) -> str:
    """reality 候选格式化 → 注入拣选 prompt（决策 41 §2.4b）。"""
    lines = []
    for i, c in enumerate(candidates):
        name = c.get("name", "")
        hdl = c.get("hdl", "")
        cs = c.get("current_status") or {}
        goals = cs.get("goals") or []
        state = cs.get("current_state") or []
        parts = [f"[{i}] {str(name)[:60]}"]
        if hdl:
            parts.append(f"    hdl: {str(hdl)[:80]}")
        if goals:
            parts.append(f"    goals: {' | '.join(str(g)[:60] for g in goals[:3])}")
        if state:
            parts.append(f"    state: {' | '.join(str(s)[:60] for s in state[:3])}")
        lines.append("\n".join(parts))
    return INJECT_PROMPT_REALITY.format(query=query[:500], candidates="\n\n".join(lines))


def _bigram_jaccard(a: str, b: str) -> float:
    """字符 bigram jaccard（embed 失败时的字面兜底；字符重叠≠语义相关，仅兜底）。"""
    def bigrams(s):
        s = (s or "").lower().replace(" ", "").replace("-", "")
        return {s[i:i + 2] for i in range(len(s) - 1)}
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _exclude_session_realities(rows, exclude_session_id: Optional[str]) -> list:
    """剔除 source_strands 含当前 session 的 reality（防自注入）。

    列序：(reality_id, name, hdl, current_status, timeline, centroid_json,
           query_centroid_json, source_strands) → source_strands 是第 8 列（index 7）。
    """
    if not exclude_session_id:
        return rows
    filtered = []
    for row in rows:
        ss_raw = row[7]
        hit = False
        if ss_raw:
            try:
                ss = json.loads(ss_raw)
                if isinstance(ss, dict) and exclude_session_id in ss:
                    hit = True
            except (json.JSONDecodeError, TypeError):
                pass
        if not hit:
            filtered.append(row)
    return filtered


def pick_injection_realities(
    query: str,
    q_emb: list,
    profile: str,
    limit: int = 3,
    exclude_session_id: Optional[str] = None,
    db_path=None,
) -> list[dict]:
    """reality 注入拣选（决策 41 §2.4b：提问云形心散度距离主路径）。

    ① 全量计算 d(q, C_r) = 1 - cos(q_emb, query_centroid_r)
    ② 范围预筛 d ≤ THETA_MAX（0.5，安全网）
    ③ 距离升序 → 截断 top-15（QUERY_CLOUD_TOP_K，4B 输入预算）
    ④ 4B 拣选 top-K（工作关联判定，允许空注入）
    ⑤ 4B 失败 → 距离 top-3 兜底；q_emb 不可用 → jaccard 字符兜底；
       全部失败 → 余弦 fallback（query_realities_by_semantics）

    Returns:
        reality dict 列表（reality_id/name/hdl/current_status/timeline），
        空列表 = 空注入（合法）或全库无匹配。
    """
    from ca.store import _get_topic_conn

    conn = _get_topic_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT reality_id, name, hdl, current_status, timeline, "
            "       centroid_json, query_centroid_json, source_strands "
            "FROM realities "
            "WHERE profile = ? "
            "  AND (query_centroid_json IS NOT NULL AND query_centroid_json != 'null' "
            "       OR centroid_json IS NOT NULL AND centroid_json != 'null') "
            "ORDER BY updated_at DESC",
            (profile,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        logger.warning("[CA_INJECT] read realities failed: %s", exc)
        return []
    rows = _exclude_session_realities(rows, exclude_session_id)
    if not rows:
        return []

    def _to_candidate(row):
        try:
            cs = json.loads(row[3]) if row[3] else {}
        except (json.JSONDecodeError, TypeError):
            cs = {}
        try:
            tl = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            tl = []
        return {
            "index": None,  # 排序后赋值
            "reality_id": row[0],
            "name": row[1] or "",
            "hdl": row[2] or "",
            "current_status": cs,
            "timeline": tl,
            "centroid": row[5],
            "query_centroid": row[6],
        }

    candidates = [_to_candidate(r) for r in rows]

    # ── ① 主路径：提问云形心距离（q_emb 可用）──
    if q_emb:
        scored = []
        for c in candidates:
            qc = None
            try:
                qc = json.loads(c["query_centroid"]) if c["query_centroid"] else None
            except (json.JSONDecodeError, TypeError):
                qc = None
            if not qc:
                continue
            d = 1.0 - _cosine(q_emb, qc)
            c["_d"] = d
            scored.append(c)
        if scored:
            scored.sort(key=lambda x: x["_d"])
            in_range = [c for c in scored if c["_d"] <= THETA_MAX]
            if not in_range:
                # 决策 41 §2.4b ④：范围空 → 空注入宁缺勿错（θ_max 安全网不可绕过）
                logger.info("[CA_INJECT] query cloud in-range empty "
                            "(θ_max=%.2f), empty injection", THETA_MAX)
                return []
            budget = in_range[:QUERY_CLOUD_TOP_K]
            for i, c in enumerate(budget):
                c["index"] = i
            picked = _pick_by_4b(query, budget, limit)
            if picked is not None:
                return picked
            logger.info("[CA_INJECT] 4B reality 拣选失败 → 距离 top-%d 兜底", limit)
            return budget[:limit]

    # ── ② jaccard 字符兜底（q_emb 不可用 / 无形心）──
    scored_j = []
    for c in candidates:
        j = max(_bigram_jaccard(query, c["name"]),
                _bigram_jaccard(query, c["hdl"]))
        c["_d"] = 1.0 - j
        scored_j.append(c)
    scored_j.sort(key=lambda x: x["_d"])
    for i, c in enumerate(scored_j[:QUERY_CLOUD_TOP_K]):
        c["index"] = i
    picked = _pick_by_4b(query, scored_j[:QUERY_CLOUD_TOP_K], limit)
    if picked is not None:
        return picked
    return scored_j[:limit]


def _pick_by_4b(query: str, budget: list[dict], limit: int) -> Optional[list]:
    """4B 拣选（工作关联判定）。None = 4B 不可用/解析失败（调用方兜底）。"""
    try:
        from ca.topic_summary import call_llm_raw
        prompt = build_inject_prompt_reality(query, budget)
        raw = call_llm_raw(prompt, num_predict=INJECT_MAX_TOKENS,
                           temperature=0.1, max_retries=1)
    except Exception as exc:
        logger.warning("[CA_INJECT] reality 4B call failed: %s", exc)
        return None
    if raw is None:
        return None
    picked = parse_inject_response(raw, len(budget))
    if picked is None:
        return None
    picked_set = set(picked)
    result = [
        {"reality_id": c["reality_id"], "name": c["name"], "hdl": c["hdl"],
         "current_status": c["current_status"], "timeline": c["timeline"]}
        for c in budget if c["index"] in picked_set
    ]
    logger.info("[CA_INJECT] reality 4B picked %d/%d (query=%.40s)",
                len(result), len(budget), query)
    return result[:limit]
