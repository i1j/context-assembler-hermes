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
                           temperature=0.1)
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
