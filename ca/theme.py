"""ca/theme.py — theme 生成引擎（v6.5 重构）

strand → theme 语义聚合链路（参考 strand 生成模式：prompt → 4B → 宽松解析 → 兜底）：
- format_theme_input:      strand 列表结构化格式化（create/merge prompt 复用）
- build_create_theme_prompt: 新 strand(s) → theme 初始状态（M4 前者，类 _format_turns_for_prompt）
- build_merge_theme_prompt:  已有 theme + 新 strand(s) → 融合更新（M4 后者，类 REFINE_SUMMARY_PROMPT）
- call_llm_for_theme:        4B 调用 + 宽松 JSON 解析（复用 topic_summary 解析器）
- assemble_theme:            组装 + 代码兜底（4B 失败）

设计决策（wiki theme 重构方案 v4/v5）：
- M4 双 prompt；M6 中文 + TOPIC_SUMMARY_MAX_CHARS=4000 预算
- 一级信息 = 当前详细状态（title + overview + OODA 四组 + key_facts + open_items）
- timeline_overview：每次归并追加一条该主题块时点的状态概述（仅 overview，不存快照）
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

OODA_LABELS = ["现象与问题", "背景与约束", "决策与方案", "后续行动"]

# ── 4B Prompt ──

CREATE_THEME_PROMPT = """你是知识库 wiki 的主题生成助手。给定一个或多个工作线（strand），生成一个主题（theme）条目。

主题（theme）是 strand 的语义聚合单元：跨主题块反复出现的相关 strand 归并为同一主题。

## 输出格式（仅 JSON）

{{
  "title": "中文主题名：一句话概括该主题做什么（禁止代码符号名/英文标识符/文件名/函数名）",
  "overview": "一段中文连贯概述：该主题当前状态——做了什么、为什么、进展到哪（不是列表）",
  "ooda": {{
    "现象与问题": ["..."],
    "背景与约束": ["..."],
    "决策与方案": ["..."],
    "后续行动": ["..."]
  }},
  "key_facts": ["已确认结论，最多 5 条"],
  "open_items": ["仍待解决的问题"],
  "timeline_overview": "本主题块时点的状态概述（追加进时间线，一句话）"
}}

规则：
- 全部输出为中文（保留必要的技术名词）
- title 必须基于 ooda 内容总结为中文语义短名，禁止抄用代码符号名
- overview 是连贯叙述（不是列表）；ooda 四组只输出有内容的组
- output size: {budget}

输入 strands：
{strands}

只输出 JSON 对象，不要其它文字。"""


MERGE_THEME_PROMPT = """你是知识库 wiki 的主题融合助手。给定一个已有主题（theme）和来自更晚主题块的新工作线（strand），将新信息融合进主题。

融合要求：
- 语义去重：新内容已被现有主题覆盖则不重复
- 更新当前状态：ooda 四组反映最新进展
- title 保留原值，除非新 strand 引入明显更重要的主题方向（重大转向）才更新
- 每次归并产生一条 timeline_overview（该主题块时点的状态概述）

## 输出格式（仅 JSON）

{{
  "merge": true,
  "title": "保留原 title，或重大转向时更新",
  "overview": "更新后的当前状态概述（中文连贯叙述）",
  "ooda": {{
    "现象与问题": ["..."],
    "背景与约束": ["..."],
    "决策与方案": ["..."],
    "后续行动": ["..."]
  }},
  "key_facts": ["已确认结论"],
  "open_items": ["仍待解决的问题"],
  "timeline_overview": "本主题块时点的状态概述（追加进时间线）"
}}

规则：
- 全部输出为中文
- merge: false 表示拒绝归并（新 strand 应另立主题）——仅在内容确实不相关时
- timeline_overview 必须输出（该块时点的快照概述）
- output size: {budget}

## 已有主题
{theme_json}

## 新 strands
{strands}

只输出 JSON 对象，不要其它文字。"""


def _budget_note(max_chars: Optional[int]) -> str:
    """预算指令：控制 4B 输出体积（M6，参考 TOPIC_SUMMARY_MAX_CHARS）。"""
    if max_chars is not None:
        return f"整个 JSON 输出必须在 {max_chars} 字符以内"
    return "保持简洁完整，无硬性字符限制"


def format_theme_input(strands: list[dict]) -> str:
    """strand 列表结构化格式化（create/merge prompt 复用）。

    每 strand：hdl + turns + ooda 四组 + changes（含内容的分组才输出）。
    """
    sections: list[str] = []
    for i, st in enumerate(strands, 1):
        lines = [f"# Strand {i}"]
        hdl = st.get("hdl", "")
        if hdl:
            lines.append(f"hdl: {hdl}")
        turns = st.get("turns") or []
        if turns:
            lines.append(f"turns: {turns}")
        ooda = st.get("ooda") or {}
        if isinstance(ooda, dict):
            for group in OODA_LABELS:
                items = ooda.get(group) or []
                if items:
                    lines.append(f"## {group}")
                    for it in items:
                        lines.append(f"- {it}")
        changes = st.get("changes") or []
        if changes:
            lines.append("## changes")
            for c in changes:
                lines.append(f"- {c}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def build_create_theme_prompt(
    strands: list[dict],
    max_chars: Optional[int] = None,
) -> str:
    """create 式 prompt：新 strand(s) → theme 初始状态。"""
    return CREATE_THEME_PROMPT.format(
        budget=_budget_note(max_chars),
        strands=format_theme_input(strands),
    )


def build_merge_theme_prompt(
    theme: dict,
    strands: list[dict],
    max_chars: Optional[int] = None,
) -> str:
    """merge 式 prompt：已有 theme + 新 strand(s) → 融合更新。"""
    theme_payload = {
        "title": theme.get("title", ""),
        "overview": theme.get("overview", ""),
        "ooda": theme.get("ooda", {}),
        "key_facts": theme.get("key_facts", []),
        "open_items": theme.get("open_items", []),
    }
    return MERGE_THEME_PROMPT.format(
        budget=_budget_note(max_chars),
        theme_json=json.dumps(theme_payload, ensure_ascii=False),
        strands=format_theme_input(strands),
    )


# ═══════════════════════════════════════════════════════════
# 归并决策（单向否决，v6.5）
# ═══════════════════════════════════════════════════════════

DECIDE_MERGE_PROMPT = """给定新工作线（strand）和候选主题（theme），决定每个 strand 应归入哪个候选主题，还是另立新主题。

规则：
- action "merge": 归入 target 指定的候选主题（target = 候选索引，0 起）
- action "new": 所有候选都不合适，另立新主题
- 宁分不并：不确定时选 "new"（保守优于错误归并）
- 只对有候选的 strand 决策（无候选的已自动 new）
- 候选按 S 值（图接近度）升序排列；S 仅表示该主题与当前上下文的图接近程度，
  不代表语义承接——必须按 strand 内容与候选的承接/推进关系判定
  （strand 的 goal 是否承接候选的 goals / 是否推进候选的状态）
- [注入主题] 是当前切换注入的上下文，优先考虑融合；但仅当真正承接时才归入，
  不承接必须选 "new"（禁止矮子里拔将军硬选）

## 输出格式（仅 JSON）

{{
  "assignments": {{
    "strand_1": {{"action": "merge", "target": 0}},
    "strand_2": {{"action": "new"}}
  }}
}}

输入 strands（含候选）：
{items}

只输出 JSON 对象，不要其它文字。"""


def format_decide_input(strands_with_candidates: list[dict]) -> str:
    """决策输入格式化：strand 内容 + 候选主题列表（title/overview/sim 标注）。"""
    sections: list[str] = []
    for i, item in enumerate(strands_with_candidates, 1):
        st = item.get("strand", item)
        lines = [f"# Strand {i}"]
        hdl = st.get("hdl", "")
        if hdl:
            lines.append(f"hdl: {hdl}")
        ooda = st.get("ooda") or {}
        if isinstance(ooda, dict):
            for group in OODA_LABELS:
                items = ooda.get(group) or []
                if items:
                    lines.append(f"## {group}")
                    for it in items:
                        lines.append(f"- {it}")
        cands = item.get("candidates") or []
        if cands:
            lines.append("## 候选主题")
            for ci, c in enumerate(cands):
                title = c.get("title", "")
                s = c.get("s_score")
                s_tag = f" (S={s:.2f})" if s is not None else ""
                prio = " [注入主题]" if c.get("_priority") else ""
                lines.append(f"  [{ci}] {title}{s_tag}{prio}")
                ov = c.get("overview") or ""
                if ov:
                    lines.append(f"      overview: {ov}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def build_decide_prompt(strands_with_candidates: list[dict]) -> str:
    """决策 prompt：内部过滤，仅含有候选的 strands（无候选已自动 new）。"""
    with_cand = [s for s in strands_with_candidates if s.get("candidates")]
    return DECIDE_MERGE_PROMPT.format(
        items=format_decide_input(with_cand))


def filter_strands_without_candidates(strands: list[dict]) -> list[dict]:
    """返回无候选（sim < 0.70，单向否决）的 strands → 调用方直接 new。"""
    return [s for s in strands if not (s.get("candidates") or [])]


def parse_assignments(text: Optional[str]) -> dict:
    """宽松解析决策 JSON → {"strand_N": {"action", "target"}}。

    容错：
    - 非法 JSON / 空 / 缺 assignments 键 → {}
    - action 非 merge/new → new
    - merge 但 target 缺失或非非负 int → 降级 new（宁分不并）
    - target 越界由 resolve_assignments 校验（有候选数上下文）
    """
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    assigns = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(assigns, dict):
        return {}
    out: dict[str, dict] = {}
    for key, val in assigns.items():
        if not isinstance(val, dict):
            continue
        action = str(val.get("action", "")).lower()
        if action not in ("merge", "new"):
            action = "new"
        entry: dict[str, Any] = {"action": action}
        if action == "merge":
            target = val.get("target")
            if isinstance(target, int) and not isinstance(target, bool) and target >= 0:
                entry["target"] = target
            else:
                entry["action"] = "new"
        out[str(key)] = entry
    return out


def resolve_assignments(
    strands: list[dict],
    llm_parsed: dict,
) -> dict:
    """完整 assignments：4B 结果 + 无候选/未覆盖 strand 默认 new。

    llm_parsed 的 key 支持两种格式：
    - "strand_N"（决策 prompt 中的索引，1 起，对应有候选 strands 的顺序）
    - strand hdl（直接引用）

    target 越界（>= 候选数）→ 降级 new（宁分不并）。
    """
    with_cand = [s for s in strands if s.get("candidates")]
    result: dict[str, dict] = {}
    for s in strands:
        hdl = s.get("hdl", "")
        entry: Optional[dict] = None
        if s.get("candidates"):
            # 先查 hdl 直引，再查索引 key
            if hdl in llm_parsed:
                entry = llm_parsed[hdl]
            else:
                idx = with_cand.index(s) + 1
                entry = llm_parsed.get(f"strand_{idx}")
        if entry is None:
            entry = {"action": "new"}
        # target 越界校验
        if entry.get("action") == "merge":
            n_cand = len(s.get("candidates") or [])
            t = entry.get("target")
            if not (isinstance(t, int) and not isinstance(t, bool) and 0 <= t < n_cand):
                entry = {"action": "new"}
        result[hdl] = dict(entry)
    return result


# ═══════════════════════════════════════════════════════════
# 同步归并链路（v6.5，两段式：向量召回 → 4B 决策 → 4B 更新）
# ═══════════════════════════════════════════════════════════


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def find_theme_candidates(
    strand_vec: list,
    themes: list[dict],
    threshold: float,
    top_k: int = 3,
    priority_themes: Optional[list] = None,
) -> list[dict]:
    """向量召回：与全量 theme centroid 余弦 → sim ≥ threshold 的 top-K 候选（降序）。

    单向否决（merge 窄）：< threshold 的 theme 不成为候选（4B 不主动拉入）。

    priority_themes (v6.5.3, 2026-08-02 用户确认): 话题切换时注入的候选主题
    （首轮 recall / FAR 检索的 3 个参考 theme）。归并优先级：
      1. 先看与注入 theme 的相似度（即使 sim 略低于 threshold 也纳入，按 sim 排序）
      2. 再考虑与其它 theme 的（threshold 硬门槛）
      3. 最后无候选 → 新建
    注入 theme 是语义检索的参考主题，应优先于普通向量召回。
    """
    priority_ids: set = set()
    if priority_themes:
        priority_ids = {t.get("theme_id") for t in priority_themes
                        if isinstance(t, dict) and t.get("theme_id") is not None}

    scored: list[dict] = []
    for t in themes:
        centroid = t.get("centroid")
        if not isinstance(centroid, list):
            continue
        sim = _cosine(strand_vec, centroid)
        is_priority = t["theme_id"] in priority_ids
        # 注入 theme：即使 sim 略低于 threshold 也纳入（优先匹配）
        if sim >= threshold or (is_priority and sim > 0):
            scored.append({"theme_id": t["theme_id"], "title": t.get("title", ""),
                           "overview": t.get("overview", ""), "sim": sim,
                           "_priority": is_priority})
    # 优先级：注入 theme 在前（其内部按 sim 降序），其余按 sim 降序
    scored.sort(key=lambda c: (not c["_priority"], -c["sim"]))
    return scored[:top_k]


def _strand_embed_text(strand: dict) -> str:
    """向量计算规范：只 embed 语义文本（hdl + ooda 内容），永不 embed 原始 JSON。"""
    parts = [strand.get("hdl", "")]
    ooda = strand.get("ooda") or {}
    if isinstance(ooda, dict):
        for group in OODA_LABELS:
            parts.extend(str(i) for i in ooda.get(group) or [] if str(i).strip())
    return " ".join(filter(None, parts))


# ── v7 (决策 38): S 匹配分候选生成（共现图行为信号，替代余弦正向门槛）──


def find_s_candidates(
    anchor_themes: Optional[list],
    themes: list[dict],
    cooc_edges: Optional[dict] = None,
    fused_ids: Optional[set] = None,
    alpha: float = 0.4,
    beta: float = 0.8,
    r_threshold: float = 0.5,
    top_k: int = 3,
) -> list[dict]:
    """S 匹配分候选生成（决策 38 §五）：加权平均图距离，排除 S>R，升序 top-K。

    S(r) = Σ_{a∈A} w_a·d(r,a) / Σ_{a∈A} w_a，其中：
      - A = 锚点集（注入集 I ∪ 块内已融合 F）
      - w_a = beta（a ∈ F，行为事实）| alpha（a ∈ I，语义猜测）
      - d(r,a) = 0（自锚）| 1/(w+1)（共现边）| ∞（无边，跳过）

    性质：
      - 锚点集内 reality S=0 → 排最前（"strand 首先融合进注入的 theme"）
      - 一跳 w≥1 共现伙伴 S≈0.5 → 次优先
      - 二跳及更远 S>0.5 → 被 R 排除（单参数天然控制跳数）
      - 与任何锚无边 → S=∞ → 不进候选（new，宁分不并）

    Returns:
        候选列表（含 s_score 字段，升序），空 = 无候选（strand 应 new）。
    """
    if not anchor_themes or not themes:
        return []
    cooc_edges = cooc_edges or {}
    anchors = [t.get("theme_id") for t in anchor_themes
               if isinstance(t, dict) and t.get("theme_id") is not None]
    if not anchors:
        return []
    fused = fused_ids or set()

    def dist(a: int, b: int) -> float:
        if a == b:
            return 0.0
        key = (min(a, b), max(a, b))
        w = cooc_edges.get(key, 0)
        return 1.0 / (w + 1) if w > 0 else float("inf")

    scored: list[dict] = []
    for t in themes:
        rid = t.get("theme_id")
        if rid is None:
            continue
        ws, num = 0.0, 0.0
        for a in anchors:
            w_a = beta if a in fused else alpha
            d = dist(int(rid), int(a))
            if math.isinf(d):
                continue
            ws += w_a * d
            num += w_a
        s = ws / num if num > 0 else float("inf")
        if s <= r_threshold:
            scored.append({
                "theme_id": rid,
                "title": t.get("title", ""),
                "overview": t.get("overview", ""),
                "s_score": s,
                "_priority": int(rid) in anchors,  # 注入集内 reality（4B prompt 标注）
            })
    scored.sort(key=lambda c: (c["s_score"], c["theme_id"]))
    return scored[:top_k]


def parse_theme_response(text: Optional[str]) -> Optional[dict]:
    """宽松解析 theme 生成/融合 JSON（复用 strand 链路的截断修复）。"""
    if not text:
        return None
    from .topic_summary import _lenient_json_parse
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        fixed = _lenient_json_parse(text)
        return fixed if isinstance(fixed, dict) else None


def _llm_call_default(prompt: str, max_chars: Optional[int]) -> Optional[str]:
    """默认 4B 调用：复用 topic_summary 的 LLM 请求逻辑（返回原始文本）。"""
    from .topic_summary import call_llm_raw
    return call_llm_raw(prompt, num_predict=Config.TOPIC_SUMMARY_MAX_TOKENS)


def _code_fallback_create(strand: dict, max_chars: int) -> dict:
    """create 兜底：4B 失败 → title=hdl，ooda=strand ooda，overview 空。"""
    return {
        "title": (strand.get("hdl") or "").strip() or "未命名主题",
        "overview": "",
        "ooda": strand.get("ooda") or {},
        "key_facts": strand.get("key_facts") or [],
        "open_items": [],
        "timeline_overview": "",
    }


def _pad_overview_from_ooda(result: dict) -> dict:
    """P2 (v6.5.1) 兜底：4B 缺 overview 时，用 ooda「决策与方案」+「现象与问题」拼接（≤3 条）。

    生成的是概览文本（分号连接），保证 [当前状态] 段不缺失；ooda 结构不变。
    """
    ooda = result.get("ooda") if isinstance(result.get("ooda"), dict) else {}
    items: list[str] = []
    for group in ("决策与方案", "现象与问题"):
        for it in ooda.get(group) or []:
            s = str(it).strip()
            if s and s not in items:
                items.append(s)
    overview = "；".join(items[:3]) if items else ""
    return {**result, "overview": overview}


def _code_fallback_merge(theme: dict, strands: list[dict]) -> dict:
    """merge 兜底：4B 失败 → 仅追加 timeline_overview + changes 去重，ooda 不变。"""
    hdls = "；".join(s.get("hdl", "") for s in strands if s.get("hdl"))
    return {
        "merge": True,
        "title": theme.get("title", ""),
        "overview": theme.get("overview", ""),
        "ooda": theme.get("ooda", {}),
        "key_facts": theme.get("key_facts", []),
        "open_items": theme.get("open_items", []),
        "timeline_overview": f"归并: {hdls}" if hdls else "",
    }


def _extract_timeline_overview(result: Optional[dict], fallback: str) -> str:
    """提取 timeline_overview（该主题块时点的状态概述），空则 fallback。"""
    if not result:
        return fallback
    tv = result.get("timeline_overview")
    return str(tv).strip() if tv else fallback


def run_theme_merge(
    strands: list[dict],
    themes: list[dict],
    embed_client: Any = None,
    threshold: float = 0.70,
    top_k: int = 3,
    max_chars: int = 4000,
    llm_call: Any = None,
    db_path: Any = None,
    profile: str = "",
    priority_themes: Optional[list] = None,
) -> dict:
    """两段式同步归并主入口（v6.5，M2/M3/M4/M7）。

    流程:
      1. 向量召回：每 strand embed → find_theme_candidates（0.70 门槛 top-K）
      2. 4B 决策（每主题块 1 次）：build_decide_prompt → assignments（单向否决）
      3. 按 theme 处理（每命中 theme 1 次）：
         - merge 式：build_merge_theme_prompt → update_theme（timeline 追加 + ooda 覆盖）
         - create 式：build_create_theme_prompt → create_theme
         - 4B 失败 → 代码兜底
      4. 写库：themes upsert + theme_strand_map(method)

    返回 {"merged": n, "created": n, "failed": n}。
    """
    from .store import (create_theme, insert_theme_strand_map,
                        load_all_themes, update_theme)

    call = llm_call or (lambda p: _llm_call_default(p, max_chars))
    merged = created = failed = 0

    # ① 候选生成（v7 S 模型，决策 38）
    #    v6.5.3: theme_ref 直连（summarize 时 4B 已判定归属，∈ 注入集才有效）
    #    v7: 无直连 → S 匹配分候选（共现图行为信号）
    direct_ids: set = set()
    for st in strands:
        st["candidates"] = []
        tr = st.get("theme_ref")
        if tr is not None and any(t["theme_id"] == tr for t in themes):
            st["_direct"] = tr
            direct_ids.add(tr)

    # v7: 共现边（每块 1 次，复合键聚合）——S 模型的图数据源
    cooc_edges: dict = {}
    try:
        from .store import query_cooccurrences
        cooc_edges, _, _ = query_cooccurrences(profile=profile, db_path=db_path)
    except Exception as exc:
        logger.warning("[CA_THEME] query_cooccurrences failed: %s", exc)

    anchor_themes = list(priority_themes or [])   # 注入集 I（S 锚点，α 权重）
    fused_ids = direct_ids                        # 块内直连目标（F 锚，β 权重）
    use_s = bool(anchor_themes)                   # 有注入锚 → S 模型（锚内 S=0 必进候选）
    for st in strands:
        if "_direct" in st:
            continue
        if use_s:
            # v7: S 匹配分候选——注入集内 reality S=0 排最前（"首先融合进注入 theme"），
            #     一跳共现伙伴次之，其余排除（S>R → new，宁分不并）
            st["candidates"] = find_s_candidates(
                anchor_themes, themes, cooc_edges,
                fused_ids=fused_ids,
                alpha=Config.S_ALPHA, beta=Config.S_BETA,
                r_threshold=Config.S_R_THRESHOLD, top_k=top_k)
            continue
        # 冷启动退化（无注入锚：全新话题空注入）→ 余弦候选（v6.5.3 行为，保持可用）
        if embed_client is None:
            continue
        text = _strand_embed_text(st)
        if not text.strip():
            continue
        vec = embed_client.embed(text[:500])
        if not vec:
            continue
        # 归并优先级 = 注入 theme 优先 → 其它 theme → 新建
        st["candidates"] = find_theme_candidates(
            vec, themes, threshold, top_k,
            priority_themes=priority_themes)

    # ② 4B 决策（仅含有候选的 strands，theme_ref 直连的跳过）
    llm_parsed: dict = {}
    with_cand = [s for s in strands if s.get("candidates") and "_direct" not in s]
    if with_cand:
        prompt = build_decide_prompt(with_cand)
        raw = call(prompt)
        if raw:
            llm_parsed = parse_assignments(raw)
    assignments = resolve_assignments(strands, llm_parsed)

    # ③ 按 action 分组执行（theme_ref 直连优先，其余按 decide 结果）
    merge_groups: dict[int, list[dict]] = {}   # theme_id -> strands
    new_strands: list[dict] = []
    for st in strands:
        if "_direct" in st:
            # v6.5.3: summarize 时 4B 已判定归属 → 直接归入指定 theme
            merge_groups.setdefault(st["_direct"], []).append(st)
            continue
        hdl = st.get("hdl", "")
        a = assignments.get(hdl, {"action": "new"})
        if a.get("action") == "merge":
            theme = st["candidates"][a["target"]]
            merge_groups.setdefault(theme["theme_id"], []).append(st)
        else:
            new_strands.append(st)

    touched_themes: set[int] = set()  # v6.5.3: 本次涉及的 theme_id（供 graphify 增量同步）

    # merge 处理（每命中 theme 1 次）
    for theme_id, group in merge_groups.items():
        theme = next((t for t in themes if t["theme_id"] == theme_id), None)
        if theme is None:
            failed += len(group)
            continue
        try:
            prompt = build_merge_theme_prompt(theme, group, max_chars=max_chars)
            raw = call(prompt)
            result = parse_theme_response(raw)
            used_fallback = False
            if result is None:
                # 4B 生成失败 → 代码兜底合并（保留向量+决策结论，仅追加 timeline_overview）
                logger.info("[CA_THEME] merge 4B failed, code fallback for theme %d", theme_id)
                result = _code_fallback_merge(theme, group)
                used_fallback = True
            elif result.get("merge") is False:
                # 4B 明确拒绝归并 → 这些 strands 转 create
                new_strands.extend(group)
                continue
            fallback = _code_fallback_merge(theme, group)
            timeline_ov = _extract_timeline_overview(result, fallback["timeline_overview"])
            timeline_entry = {
                "topic_id": group[0].get("topic_id"),
                "turns": group[0].get("turns") or [],
                "session_id": group[0].get("session_id", ""),
                "overview": timeline_ov,
            }
            new_ooda = result.get("ooda") if result and isinstance(result.get("ooda"), dict) else fallback["ooda"]
            new_key_facts = result.get("key_facts") if result and isinstance(result.get("key_facts"), list) else fallback["key_facts"]
            new_open_items = result.get("open_items") if result and isinstance(result.get("open_items"), list) else fallback["open_items"]
            new_title = result.get("title") if result and result.get("title") else fallback["title"]
            new_overview = result.get("overview") if result and result.get("overview") else fallback["overview"]
            # centroid 重算（embed overview + ooda + key_facts）
            centroid_json = None
            if embed_client is not None:
                centroid_text = " ".join(filter(None, [
                    str(new_overview),
                    *[str(i) for g in (new_ooda or {}).values() if isinstance(g, list) for i in g if str(i).strip()],
                    *[str(kf) for kf in new_key_facts],
                ]))
                if centroid_text.strip():
                    cv = embed_client.embed(centroid_text[:500])
                    if cv:
                        centroid_json = json.dumps(cv, ensure_ascii=False)
            update_theme(
                theme_id=theme_id,
                title=new_title, overview=new_overview,
                ooda=new_ooda, key_facts=new_key_facts,
                open_items=new_open_items,
                timeline_entry=timeline_entry,
                changes=[str(c) for s in group for c in (s.get("changes") or []) if str(c).strip()],
                centroid_json=centroid_json,
                source_strand={"session_id": group[0].get("session_id", ""),
                               "strand_id": group[0].get("strand_id")},
                db_path=db_path,
            )
            for st in group:
                insert_theme_strand_map(
                    st.get("session_id", ""), st.get("strand_id"), theme_id,
                    method="fallback" if used_fallback else "llm", db_path=db_path)
            merged += len(group)
            touched_themes.add(theme_id)
        except Exception as exc:
            logger.warning("[CA_THEME] merge theme %d failed: %s", theme_id, exc)
            failed += len(group)

    # create 处理（每个 new strand 独立 create，宁分不并）
    for st in new_strands:
        try:
            prompt = build_create_theme_prompt([st], max_chars=max_chars)
            raw = call(prompt)
            result = parse_theme_response(raw)
            fb = _code_fallback_create(st, max_chars)
            # P2 (v6.5.1): 4B 返回 ooda 但缺 overview → 重试一次（对齐 strand P1-1
            # 温度抖动防御）；重试仍缺 → 代码拼接（决策与方案 + 现象与问题）。
            # v6.5.2: 放宽为「result 非 None 且 overview 空」即重试（4B 偶发返回
            # 结构性无效 JSON——title/overview/ooda 全缺，实测 1.5%——此前直接
            # 落 hdl fallback 导致 overview 恒空；重试仍缺 → 用最终 ooda 拼接）
            if (result is not None
                    and not (result.get("overview") or "").strip()):
                logger.info("[CA_THEME] create missing overview, retrying once")
                retry_raw = call(prompt)
                retry = parse_theme_response(retry_raw)
                if retry is not None and (retry.get("overview") or "").strip():
                    result = retry
                else:
                    src = dict(result) if result else {}
                    if not src.get("ooda"):
                        src["ooda"] = fb["ooda"]
                    result = _pad_overview_from_ooda(src)
            title = result.get("title") if result and result.get("title") else fb["title"]
            overview = result.get("overview") if result and result.get("overview") else fb["overview"]
            ooda = result.get("ooda") if result and isinstance(result.get("ooda"), dict) else fb["ooda"]
            key_facts = result.get("key_facts") if result and isinstance(result.get("key_facts"), list) else fb["key_facts"]
            open_items = result.get("open_items") if result and isinstance(result.get("open_items"), list) else fb["open_items"]
            timeline_ov = _extract_timeline_overview(result, "")
            timeline_entry = {
                "topic_id": st.get("topic_id"),
                "turns": st.get("turns") or [],
                "session_id": st.get("session_id", ""),
                "overview": timeline_ov or overview,
            }
            centroid_json = None
            if embed_client is not None:
                centroid_text = " ".join(filter(None, [
                    str(overview),
                    *[str(i) for g in (ooda or {}).values() if isinstance(g, list) for i in g if str(i).strip()],
                    *[str(kf) for kf in key_facts],
                ]))
                if centroid_text.strip():
                    cv = embed_client.embed(centroid_text[:500])
                    if cv:
                        centroid_json = json.dumps(cv, ensure_ascii=False)
            tid = create_theme(
                profile=profile,
                title=title, overview=overview, ooda=ooda,
                key_facts=key_facts, open_items=open_items,
                timeline_entry=timeline_entry,
                source_strand={"session_id": st.get("session_id", ""),
                               "strand_id": st.get("strand_id")},
                centroid_json=centroid_json,
                changes=[str(c) for c in (st.get("changes") or []) if str(c).strip()],
                db_path=db_path,
            )
            if tid is None:
                failed += 1
                continue
            insert_theme_strand_map(
                st.get("session_id", ""), st.get("strand_id"), tid,
                method="llm", db_path=db_path)
            created += 1
            touched_themes.add(tid)
        except Exception as exc:
            logger.warning("[CA_THEME] create theme failed: %s", exc)
            failed += 1

    logger.info("[CA_THEME] sync merge done: %d merged, %d created, %d failed",
                merged, created, failed)
    return {"merged": merged, "created": created, "failed": failed,
            "theme_ids": sorted(touched_themes)}
