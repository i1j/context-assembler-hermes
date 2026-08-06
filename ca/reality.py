"""ca/reality.py — reality 生成引擎 prompt（决策 37 reality 重构，prompt 部分）。

reality = 现实工作对象：多个语义独立但工作中有关联的 strand 的集合，跨块/跨会话演进。
本模块只承载 prompt 三件套 + 输入格式化 + 宽松解析（接入/落库为后续 reprocess 步骤）：

- REALITY_CREATE_PROMPT:  新 strand(s) → reality 初始状态（OV L1 五段映射）
- REALITY_MERGE_PROMPT:   已有 reality + 新 strand(s) → 融合更新（OV update 语义）
- REALITY_DECIDE_PROMPT:  strand「现象与问题」承接 current_status.goals → merge/new

数据模型（§4.1）:
  {name(固定标识), hdl(状态锚点,可改), current_status{current_state, key_facts,
   goals, context}, timeline(旧 hdl 序列,代码维护)}

设计溯源（§4.3 对齐 OV L1 + §5.1 判定机制 + OV update 借鉴）:
  OV Current State      → current_status.current_state（快照，2-5 条）
  OV Task & Goals       → current_status.goals（衔接判定锚）
  OV Key Facts & Decis. → current_status.key_facts（持久事实，带日期）
  OV Files & Context    → current_status.context（相关文件/资源，可空）
  OV Abstract           → hdl（可改锚点，非 name）
  判定：strand「现象与问题」是否承接 reality.current_status.goals？
        是 → merge（更新 current_status + 重写 hdl，旧 hdl 入 timeline）
        否 → 新 reality（宁分不并：不确定 → new）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .config import Config
from .theme import (OODA_LABELS, _cosine, _llm_call_default,
                    _strand_embed_text,
                    parse_assignments as parse_reality_assignments)

logger = logging.getLogger(__name__)

__all__ = [
    "REALITY_CREATE_PROMPT",
    "REALITY_MERGE_PROMPT",
    "REALITY_DECIDE_PROMPT",
    "format_reality_input",
    "format_decide_reality_input",
    "build_create_reality_prompt",
    "build_merge_reality_prompt",
    "build_reality_decide_prompt",
    "parse_reality_response",
    "parse_reality_assignments",
    # 防膨胀守卫
    "SECTION_LIMITS",
    "extract_lexical_anchors",
    "guard_merge_consolidation",
    "salvage_new_items",
    "throttle_append",
    "build_size_warnings",
    "find_oversized_sections",
    "build_force_note",
    "truncate_by_anchor_priority",
    "enforce_section_limits",
    "fill_current_status_fallback",
    "append_timeline_hdl",
]

# ── 4B Prompt ──

REALITY_CREATE_PROMPT = """你是知识库的现实工作对象（reality）创建助手。给定一个或多个工作线（strand），创建一个 reality 条目。

reality 是现实工作对象的聚合单元：多个语义独立但工作中有关联的 strand 集合，跨话题块/跨会话演进。

## 输出格式（仅 JSON）

{{
  "name": "固定事物名：一句话概括该 reality 是什么工作对象（不变；禁止代码符号名/英文标识符/文件名/函数名）",
  "hdl": "当前状态锚点：一句话描述现状与进展（可改，每次归并重写，旧值由系统追加进 timeline）",
  "current_status": {{
    "current_state": ["当前状态快照：最新进展，2-5 条，不是事实堆砌"],
    "key_facts": ["已确认持久事实/结论，带日期（YYYY-MM-DD: ...），最多 5 条"],
    "goals": ["当前目标：待完成/待验证事项（衔接判定锚，后续 strand 靠它判定是否承接）"],
    "context": ["相关文件路径/资源（可空）"]
  }}
}}

规则：
- 全部输出为中文（保留必要的技术名词）
- name 必须基于 ooda 内容总结为中文语义短名，禁止抄用代码符号名（同 hdl 命名规范）
- **name ≠ hdl**：name 是固定事物名（该工作对象"是什么"，长期不变）；
  hdl 是当前状态锚点（"进展到哪"，随演进可改）。禁止把 strand 的 hdl 直接抄作 name——
  name 应比 hdl 更抽象（工作对象本身），hdl 描述当前进度
- current_state 是当前状态快照（2-5 条），详细事实放 key_facts；不编造
- key_facts 记录持久事实（结论/决策/约束），不是过程步骤
- goals 写清"还要做什么"——它是后续 strand 衔接判定的锚
- output size: {budget}

输入 strands：
{strands}

只输出 JSON 对象，不要其它文字。"""


REALITY_MERGE_PROMPT = """你是知识库的现实工作对象（reality）融合助手。给定一个已有 reality 和来自更晚话题块的新工作线（strand），将新信息融合进 reality。

## 已有 reality（JSON）
{reality_json}

## 新 strands
{strands}

{size_warnings}
{force_note}

## 输出格式（仅 JSON）

{{
  "merge": true,
  "hdl": "重写后的当前状态锚点：一句话（旧 hdl 由系统追加进 timeline）",
  "current_status": {{
    "current_state": ["更新后的当前状态快照（2-5 条）：最新进展 + 仍未解决的旧项"],
    "key_facts": ["持久事实：新增 + 保留未过期旧事实（带日期），最多 5 条，不重复"],
    "goals": ["目标：见下方 goals 规则"],
    "context": ["相关文件路径/资源：只保留未来答案可能依赖的，同资源合并为一条"]
  }}
}}

规则：
- 全部输出为中文
- merge: false 表示拒绝归并（新 strand 应另立 reality）——仅在内容确实不相关时

### 各字段更新语义（重要，逐字段遵守）

- **current_state（快照，允许缩小）**：重写为最新快照，2-5 条。
  上一个快照中**已解决的条目必须删除**（历史在 timeline 里，不用保留）；
  只保留仍未解决的事项 + 新进展。它是唯一允许变小的字段。
- **key_facts（持久事实，按主题合并）**：新增持久事实；已被现有覆盖的不重复。
  超限时**按主题归类合并**——同主题多条合并为一条，保留日期/数值等关键锚点
  （例：5 条"清理了 X 缓存"→"清理了 5 类缓存，最近一次 [日期]"）。
- **goals（目标清单，默认 KEEP）**：默认**原样保留**已有 goals，不做任何改动。
  仅当：新 strand 引入新目标 → 添加；旧目标已完成/失效 → **显式标记完成并从列表移除**。
  不允许无理由地改写已有目标措辞。
- **context（资源引用，过滤去重）**：只保留未来答案可能依赖的**文件路径/URL**；
  同一资源的多次出现**合并为一条**；**禁止函数名/类名/无路径描述文字**
  （如 `get_gguf_models()`、`OV Wiki: 资源文档整合页` 类条目删除）。

- 不编造信息；只基于已有 reality + 新 strands 更新
- output size: {budget}

只输出 JSON 对象，不要其它文字。"""


REALITY_DECIDE_PROMPT = """给定新工作线（strand）和候选 reality（现实工作对象），决定每个 strand 应归入哪个候选 reality，还是另立新 reality。

## 衔接判定（核心：状态承接，不是语义相似）

判断 strand 与候选 reality 是否是同一工作对象：
- strand 的「现象与问题」是否承接该 reality.current_status.goals？（正在解决该 reality 的目标）
- strand 是否推进/更新该 reality 的状态？（同一工作的延续/深化/修复）
- **家族一致性**：候选 reality 的**成员工作线**（member hdls）与 strand 的
  语义家族是否一致？（strand 是"GGUF 模型加载"，候选成员是"GGUF 路径扫描/文件结构"
  → 同族，优先归入；仅 goals 宽泛相关但成员工作线与 strand 无关 → 不归入）

→ 是：merge（归入该 reality）
→ 否：new（另立新 reality）
→ 不确定：new（宁分不并——错误归并不可逆，宁可碎片留给精炼轮合并）

规则：
- action "merge": 归入 target 指定的候选 reality（target = 候选索引，0 起）
- action "new": 所有候选都不合适，另立新 reality
- 宁分不并：不确定时选 "new"（保守优于错误归并）
- 只对有候选的 strand 决策（无候选的已自动 new）
- **成员工作线与 strand 家族一致 > goals 宽泛承接**：当某候选 goals 面宽
  （大杂烩）但成员与 strand 不同族时，倾向 new 或归入同族候选

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


# ── 输入格式化 ──


def format_reality_input(strands: list[dict]) -> str:
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


def format_decide_reality_input(strands_with_candidates: list[dict]) -> str:
    """决策输入格式化：strand 内容 + 候选（name/hdl/goals/成员标注）。

    判定锚是 goals + 家族一致性——候选展示 current_status.goals；
    候选带 member_hdls 时展示成员工作线（A1：家族一致性判定锚）；
    重聚场景候选是 strand 形态（无 current_status）→ 兜底用
    hdl + ooda「后续行动」（早期 strand 的后续 = 晚期 strand 承接的目标）。
    """
    sections: list[str] = []
    for i, item in enumerate(strands_with_candidates, 1):
        st = item.get("strand", item)
        lines = [f"# Strand {i}"]
        hdl = st.get("hdl", "")
        if hdl:
            lines.append(f"hdl: {hdl}")
        ooda = st.get("ooda") or {}
        if isinstance(ooda, dict):
            # 判定锚是「现象与问题」——优先完整呈现，其余组含内容才输出
            ordered = (["现象与问题"]
                       + [g for g in OODA_LABELS if g != "现象与问题"])
            for group in ordered:
                items = ooda.get(group) or []
                if items:
                    lines.append(f"## {group}")
                    for it in items:
                        lines.append(f"- {it}")
        cands = item.get("candidates") or []
        if cands:
            lines.append("## 候选 reality")
            for ci, c in enumerate(cands):
                name = c.get("name") or c.get("title", "")
                h = c.get("hdl", "")
                cs = c.get("current_status") or {}
                if isinstance(cs, dict):
                    goals = cs.get("goals") or []
                else:
                    goals = []
                if not goals:
                    # strand 形态候选：后续行动作承接锚
                    o = c.get("ooda") or {}
                    if isinstance(o, dict):
                        goals = o.get("后续行动") or []
                lines.append(f"  [{ci}] {name or h or '(未命名)'}")
                if h and h != name:
                    lines.append(f"      hdl: {h}")
                if goals:
                    lines.append(f"      goals: {'; '.join(str(g) for g in goals)}")
                members = c.get("member_hdls") or []
                if members:
                    lines.append("      成员: " + "; ".join(str(m) for m in members))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


# ── 构建 ──


def _budget_note(max_chars: Optional[int]) -> str:
    """预算指令：控制 4B 输出体积（对齐 TOPIC_SUMMARY_MAX_CHARS 约定）。"""
    if max_chars is not None:
        return f"整个 JSON 输出必须在 {max_chars} 字符以内"
    return "保持简洁完整，无硬性字符限制"


def build_create_reality_prompt(
    strands: list[dict],
    max_chars: Optional[int] = None,
) -> str:
    """create 式 prompt：新 strand(s) → reality 初始状态（OV L1 五段映射）。"""
    return REALITY_CREATE_PROMPT.format(
        budget=_budget_note(max_chars),
        strands=format_reality_input(strands),
    )


def build_merge_reality_prompt(
    reality: dict,
    strands: list[dict],
    max_chars: Optional[int] = None,
    force_note: str = "",
) -> str:
    """merge 式 prompt：已有 reality + 新 strand(s) → 融合更新（OV update 语义）。

    注入 size_warnings（服务端监控层）：reality 某段超限时，在 prompt 中
    明确要求 4B 合并该段（对齐 OV _build_wm_section_reminders）。
    注入 force_note（N2 方向 A）：上次输出超限被拒后的强化重试指令。
    """
    payload = {
        "name": reality.get("name", ""),
        "hdl": reality.get("hdl", ""),
        "current_status": reality.get("current_status", {}),
    }
    return REALITY_MERGE_PROMPT.format(
        budget=_budget_note(max_chars),
        reality_json=json.dumps(payload, ensure_ascii=False),
        strands=format_reality_input(strands),
        size_warnings=build_size_warnings(reality),
        force_note=force_note or "",
    )


def build_reality_decide_prompt(strands_with_candidates: list[dict]) -> str:
    """决策 prompt：内部过滤，仅含有候选的 strands（无候选已自动 new，单向否决）。"""
    with_cand = [s for s in strands_with_candidates if s.get("candidates")]
    return REALITY_DECIDE_PROMPT.format(
        items=format_decide_reality_input(with_cand))


# ── 防膨胀守卫（对齐 OV WM 三层防线第 2/3 层，2026-08-03）──

# 各段条目数上限（reality 单对象粒度，小于 OV 会话级 25；实验实测分布定）
SECTION_LIMITS = {
    "current_state": 5,
    "key_facts": 5,
    "goals": 8,
    "context": 8,
}
_SECTION_CN_NAMES = {
    "current_state": "current_state（当前状态）",
    "key_facts": "key_facts（持久事实）",
    "goals": "goals（目标）",
    "context": "context（资源）",
}

# 锚点提取（中文适配；锚点 = 可验证的事实载体）
_ANCHOR_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ANCHOR_NUMBER_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:GB|MB|KB|TB|ms|秒|分钟|小时|天|次|条|个|人|万|亿)\b",
    re.IGNORECASE,
)
_ANCHOR_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"']+|"
    r"[\w./\\-]+\.(?:py|ts|js|jsx|md|yaml|yml|json|sh|ps1|cmd|bat|toml|ini|cfg|rs|go|safetensors|gguf)|"
    r"(?:https?://\S+|hf-mirror\.com|huggingface\.co))",
    re.IGNORECASE,
)
_ANCHOR_ASCII_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{3,}\b")
_ANCHOR_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "have", "been",
    "were", "will", "would", "should", "into", "upon", "after", "before",
    "配置", "进行", "完成", "确认", "实现", "优化", "问题", "方案",
})


def extract_lexical_anchors(text: str) -> set:
    """提取事实锚点：日期 / 数字+单位 / 路径 / ASCII 专名。

    中文正文不作为锚点（无法可靠比对），只取可验证载体。
    对齐 OV _extract_lexical_anchors，适配中文 reality 内容。
    """
    anchors: set = set()
    if not text:
        return anchors
    for m in _ANCHOR_DATE_RE.finditer(text):
        anchors.add(m.group().lower())
    for m in _ANCHOR_NUMBER_RE.finditer(text):
        anchors.add(m.group().lower())
    for m in _ANCHOR_PATH_RE.finditer(text):
        anchors.add(m.group().lower())
    for m in _ANCHOR_ASCII_TOKEN_RE.finditer(text):
        tok = m.group().lower()
        if tok not in _ANCHOR_STOPWORDS:
            anchors.add(tok)
    return anchors


def _section_anchor_coverage(old_items: list, new_items: list) -> float:
    """新列表对旧列表的锚点覆盖率（0~1）。"""
    old_anchors = set()
    for it in old_items:
        old_anchors |= extract_lexical_anchors(str(it))
    if not old_anchors:
        return 1.0  # 旧无锚点（纯中文）→ 不拦（保守放行）
    new_anchors = set()
    for it in new_items:
        new_anchors |= extract_lexical_anchors(str(it))
    return len(old_anchors & new_anchors) / len(old_anchors)


def guard_merge_consolidation(old_cs: dict, new_cs: dict) -> dict:
    """merge 结果守卫（对齐 OV _wm_enforce_key_facts_consolidation）。

    对 key_facts/goals 两段校验：
      Layer 1 trivial_shrink：新条数 < 旧条数 50% → 拒绝（合并过度，丢事实风险）
      Layer 2 low_anchor_coverage：锚点覆盖 < 70% → 拒绝（关键锚点丢失）
    返回 {"approved": bool, "reason": str|None, "failed_sections": [..]}
    """
    failed: list[str] = []
    reason = None
    for section in ("key_facts", "goals"):
        old_items = [str(x) for x in (old_cs.get(section) or []) if str(x).strip()]
        new_items = [str(x) for x in (new_cs.get(section) or []) if str(x).strip()]
        if not old_items:
            continue
        # Layer 1: trivial shrink（新显著小于旧 → 合并过度）
        if len(new_items) < len(old_items) * 0.5:
            failed.append(section)
            reason = reason or "trivial_shrink"
            continue
        # Layer 2: anchor coverage（合并后锚点必须保留）
        cov = _section_anchor_coverage(old_items, new_items)
        if cov < 0.70:
            failed.append(section)
            reason = reason or "low_anchor_coverage"
    if failed:
        return {"approved": False, "reason": reason, "failed_sections": failed}
    return {"approved": True, "reason": None, "failed_sections": []}


def salvage_new_items(old_cs: dict, new_cs: dict, section: str) -> dict:
    """守卫拒绝后 salvage：从被拒内容提取真新增条目（去重后 APPEND）。

    对齐 OV _salvage_new_items_from_rejected_update：
    旧列表中没有的新条目 → 保留；否则丢弃。
    返回 {"op": "APPEND", "items": [...]} 或 {"op": "KEEP"}。
    """
    old_items = [str(x) for x in (old_cs.get(section) or []) if str(x).strip()]
    new_items = [str(x) for x in (new_cs.get(section) or []) if str(x).strip()]
    old_lower = {str(x).strip().lower() for x in old_items}
    fresh = []
    for it in new_items:
        key = it.strip().lower()
        if key and key not in old_lower:
            fresh.append(it)
    if fresh:
        return {"op": "APPEND", "items": fresh}
    return {"op": "KEEP"}


def throttle_append(old_items: list, new_items: list, cap: int = 5) -> list:
    """超限 APPEND 节流：只收真新增，上限 cap 条（对齐 _WM_OVERSIZED_APPEND_CAP=5）。"""
    old_lower = {str(x).strip().lower() for x in old_items}
    fresh = []
    for it in new_items:
        key = str(it).strip().lower()
        if key and key not in old_lower:
            fresh.append(it)
    return fresh[:cap]


def build_size_warnings(reality: dict) -> str:
    """服务端监控：统计 reality 各段条数，超限生成合并警告（注入 merge prompt）。

    对齐 OV _build_wm_section_reminders（阈值取 SECTION_LIMITS）。
    返回 XML 块或空串（无超限）。
    """
    cs = reality.get("current_status") or {}
    if not isinstance(cs, dict):
        return ""
    warnings: list[str] = []
    for section, limit in SECTION_LIMITS.items():
        items = [str(x) for x in (cs.get(section) or []) if str(x).strip()]
        if len(items) > limit:
            warnings.append(
                f'WARNING: "{_SECTION_CN_NAMES.get(section, section)}" '
                f"已有 {len(items)} 条（上限 {limit} 条）。\n"
                f"本次必须合并该段：按主题归类，重复/已解决条目合并或删除，"
                f"保留日期/数值/路径等关键锚点。"
            )
    if not warnings:
        return ""
    return ("<section_size_warnings>\n" + "\n\n".join(warnings)
            + "\n</section_size_warnings>")


def find_oversized_sections(cs: dict) -> dict:
    """超限检测：返回 {section: count}（count > SECTION_LIMITS[section]）。"""
    if not isinstance(cs, dict):
        return {}
    over: dict = {}
    for section, limit in SECTION_LIMITS.items():
        items = [str(x) for x in (cs.get(section) or []) if str(x).strip()]
        if len(items) > limit:
            over[section] = len(items)
    return over


def build_force_note(oversized: dict) -> str:
    """强化重试指令：上次输出超限被拒 → 要求 4B 必须合并到限内。

    N2 方向 A（2026-08-03）：4B 全量重写时不压缩（逐条保留旧条目 + 新增）
    → key_facts/goals/context 超限。重试时注入本指令硬性要求压缩。
    """
    if not oversized:
        return ""
    lines = ["<force_consolidation>",
             "上次输出被拒绝：以下字段超过上限，本次必须压缩到限内（硬性要求，不允许逐条保留）："]
    for section, count in sorted(oversized.items()):
        limit = SECTION_LIMITS.get(section, 5)
        lines.append(
            f'- {_SECTION_CN_NAMES.get(section, section)}: {count} 条 → 必须 ≤{limit} 条。'
            f" 按主题归类合并，同主题多条并成一条，保留日期/数值/路径锚点，"
            f"丢弃纯描述/已解决条目。")
    lines.append("</force_consolidation>")
    return "\n".join(lines)


def truncate_by_anchor_priority(items: list, limit: int) -> list:
    """代码兜底：超限重试仍不合格 → 按锚点丰富度截断保留。

    锚点 = 日期/数字+单位/路径/ASCII 专名（extract_lexical_anchors）。
    带锚点的条目优先保留（可验证事实），纯中文描述性条目最后丢弃。
    """
    scored = []
    for it in items:
        s = str(it)
        n_anchors = len(extract_lexical_anchors(s))
        scored.append((n_anchors, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


def enforce_section_limits(cs: dict) -> dict:
    """强制各段不超限：超限段按锚点优先截断（代码层最终兜底）。

    N2 方向 A：正常 merge 路径超限重试仍不合格 / fallback 保留旧超限值
    时，本函数保证落库的 current_status 各段 ≤ SECTION_LIMITS。
    原地修改并返回。
    """
    if not isinstance(cs, dict):
        return cs
    for section, limit in SECTION_LIMITS.items():
        items = [str(x) for x in (cs.get(section) or []) if str(x).strip()]
        if len(items) > limit:
            cs[section] = truncate_by_anchor_priority(items, limit)
    return cs


def append_timeline_hdl(timeline: list, hdl: str) -> list:
    """timeline 追加防重（A2：2026-08-03 审计）。

    根因：merge fallback 路径只 append old_hdl 但 hdl 不重写 → 下一次
    merge 的 old_hdl 仍是同一值，重复追加（reality@35 第 3/4 条相同）。
    规则：空 hdl 不追加；与末条相同不追加（保持演变序列唯一）。
    返回新 timeline（原地修改并返回）。
    """
    h = (hdl or "").strip()
    if not h:
        return timeline
    if timeline and str(timeline[-1]).strip() == h:
        return timeline
    timeline.append(h)
    return timeline


def fill_current_status_fallback(strand: dict, parsed: dict) -> dict:
    """create 兜底：current_status 字段缺失/全空 → 从 strand ooda 填充。

    修复 2026-08-03 实验 @27/@28/@41/@45：4B 偶发只输出 name/hdl 不输出
    current_status（JSON 合法所以 fallback 未触发）；后补 @7/@31/@33 等
    「键存在但四段全空」的空结构 dict（truthy）同样触发。映射（对齐 §7 ③）：
      current_state ← ooda「决策与方案」+「现象与问题」
      goals         ← ooda「后续行动」
      key_facts     ← changes 派生（确认性结论）
      context       ← 保留（可空）
    已有字段有值 → 不覆盖（只填空缺）。
    """
    cs = parsed.get("current_status")
    if not isinstance(cs, dict):
        cs = {}
    # 四段全空的空结构 dict 视为缺失（键存在但无内容）
    if not any((cs.get(k) or []) for k in ("current_state", "key_facts",
                                           "goals", "context")):
        cs = {}
    ooda = strand.get("ooda") or {}
    if not isinstance(ooda, dict):
        ooda = {}
    changes = [str(c) for c in (strand.get("changes") or []) if str(c).strip()]
    if not cs.get("current_state"):
        cs["current_state"] = (
            [str(x) for x in ooda.get("决策与方案") or [] if str(x).strip()]
            or [str(x) for x in ooda.get("现象与问题") or [] if str(x).strip()])
    if not cs.get("goals"):
        cs["goals"] = [str(x) for x in ooda.get("后续行动") or [] if str(x).strip()]
    if not cs.get("key_facts"):
        cs["key_facts"] = changes[:5]
    if "context" not in cs:
        cs["context"] = []
    parsed["current_status"] = cs
    return parsed


# ── 解析 ──

# merge prompt 字段说明被 4B 抄进输出值的已知前缀（Q1：hdl 前缀污染）
_HDL_FIELD_PREFIXES = (
    "重写后的当前状态锚点：",
    "重写后的当前状态锚点:",
    "当前状态锚点：",
    "当前状态锚点:",
)


def _strip_hdl_field_prefix(text: str) -> str:
    """剥离 4B 抄写的字段说明前缀（"重写后的当前状态锚点：" 类）。

    实测（winker 46 strand 实验）：merge 输出 hdl 时 4B 把 prompt 的
    字段说明原文抄进值（7/20 次 merge 命中），污染锚点与 timeline。
    清洗规则：仅剥离已知前缀，不动正文（保守，防误删用户内容）。
    """
    t = (text or "").strip()
    for p in _HDL_FIELD_PREFIXES:
        if t.startswith(p):
            return t[len(p):].strip()
    return t


def normalize_reality_response(parsed: Optional[dict]) -> Optional[dict]:
    """规范化 4B 输出：剥离字段说明前缀污染（hdl/name/current_status 逐项）。

    Q1（2026-08-03 实验）：merge 时 4B 把 prompt 字段说明抄进 hdl 值。
    清洗 hdl/name；current_status 四段条目若带前缀同样剥离。
    """
    if not isinstance(parsed, dict):
        return parsed
    for field in ("hdl", "name"):
        v = parsed.get(field)
        if isinstance(v, str):
            parsed[field] = _strip_hdl_field_prefix(v)
    cs = parsed.get("current_status")
    if isinstance(cs, dict):
        for k in ("current_state", "key_facts", "goals", "context"):
            items = cs.get(k)
            if isinstance(items, list):
                cs[k] = [_strip_hdl_field_prefix(str(i)) if isinstance(i, str) else i
                         for i in items]
    return parsed


def parse_reality_response(text: Optional[str]) -> Optional[dict]:
    """宽松解析 reality 生成/融合 JSON（复用 strand 链路的截断修复）。

    容错：空/非法 JSON → None（调用方走代码兜底）；
    成功后经 normalize_reality_response 剥离字段说明前缀污染。
    """
    if not text:
        return None
    from .topic_summary import _lenient_json_parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return normalize_reality_response(data)
        return None
    except (json.JSONDecodeError, TypeError):
        fixed = _lenient_json_parse(text)
        if isinstance(fixed, dict):
            return normalize_reality_response(fixed)
        return None


# ══════════════════════════════════════════════════════════════════════
# 主流程：run_reality_merge（决策 41 生产接入，2026-08-07）
# ══════════════════════════════════════════════════════════════════════

def _reality_code_fallback_create(strand: dict, max_chars: int) -> dict:
    """create 兜底：strand 数据构造最小 reality（4B 失败时，宁缺勿错但保可用）。"""
    ooda = strand.get("ooda") or {}
    if isinstance(ooda, str):
        try:
            ooda = json.loads(ooda)
        except (json.JSONDecodeError, TypeError):
            ooda = {}
    name = strand.get("hdl") or strand.get("title") or "未命名工作线"
    return {
        "name": str(name)[:60],
        "hdl": str(strand.get("hdl") or "")[:120],
        "current_status": {
            "current_state": (ooda.get("现象与问题") or ooda.get("current_state") or [])[:5],
            "key_facts": (ooda.get("决策与方案") or ooda.get("key_facts") or [])[:5],
            "goals": (ooda.get("后续行动") or ooda.get("goals") or [])[:8],
            "context": (ooda.get("相关文件") or ooda.get("context") or [])[:8],
        },
        "timeline_overview": " ".join(
            str(i) for i in (ooda.get("现象与问题") or [])[:2]),
    }


def _reality_code_fallback_merge(reality: dict, group: list) -> dict:
    """merge 兜底：保留现有 reality，新 strand 现象并入 current_state（去重截断）。"""
    cs = dict(reality.get("current_status") or {})
    cs["current_state"] = list((cs.get("current_state") or [])[:5])
    new_items = []
    for s in group:
        ooda = s.get("ooda") or {}
        if isinstance(ooda, str):
            try:
                ooda = json.loads(ooda)
            except (json.JSONDecodeError, TypeError):
                ooda = {}
        for v in ooda.get("现象与问题") or []:
            if str(v).strip() and str(v) not in new_items:
                new_items.append(str(v))
    if new_items:
        cs["current_state"] = (cs.get("current_state") + new_items)[:5]
    return {
        "name": reality.get("name", ""),
        "hdl": reality.get("hdl", ""),
        "current_status": cs,
        "timeline_overview": new_items[0] if new_items else "",
    }


def _update_query_centroid(
    conn,
    reality_id: int,
    query_text: str,
    embed_client: Any,
) -> None:
    """增量维护 reality 提问云形心：(旧形心×n + 新向量)/(n+1)。

    query_text 缺失（未快照/冷启动）→ 跳过（形心由离线迁移 + 后续快照维护）。
    """
    if not query_text or embed_client is None:
        return
    qv = embed_client.embed(str(query_text)[:500])
    if not qv:
        return
    row = conn.execute(
        "SELECT query_centroid_json, query_count FROM realities WHERE reality_id=?",
        (reality_id,)).fetchone()
    if not row:
        return
    n = row[1] or 0
    if row[0] and row[0] != "null":
        try:
            old = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            old = []
        if old and len(old) == len(qv):
            new = [(old[i] * n + qv[i]) / (n + 1) for i in range(len(qv))]
        else:
            new = qv
    else:
        new = qv
    conn.execute(
        "UPDATE realities SET query_centroid_json=?, query_count=? WHERE reality_id=?",
        (json.dumps(new, ensure_ascii=False), n + 1, reality_id))


def _cold_start_cosine_candidates(
    strand_vec: list,
    realities: list[dict],
    r_threshold: float = 0.5,
    top_k: int = 3,
) -> list[dict]:
    """冷启动余弦候选（决策 38 §8：无注入锚/空注入时退化余弦保持可用）。

    与 theme 链路 find_theme_candidates 对齐：余弦距离 s = 1 - sim ≤ R 的
    reality 进候选，按 s 升序取 top-K。候选 dict 键为 reality_id（reality 类型）。
    """
    scored: list[dict] = []
    for t in realities:
        centroid = t.get("centroid")
        if not isinstance(centroid, list) or not centroid:
            continue
        rid = t.get("reality_id")
        if rid is None:
            continue
        s = 1.0 - _cosine(strand_vec, centroid)
        if s <= r_threshold:
            scored.append({
                "reality_id": rid,
                "name": t.get("name", ""),
                "hdl": t.get("hdl", ""),
                "s_score": s,
                "_priority": False,
            })
    scored.sort(key=lambda c: (c["s_score"], int(c["reality_id"])))
    return scored[:top_k]


def run_reality_merge(
    strands: list[dict],
    realities: list[dict],
    embed_client: Any = None,
    threshold: float = 0.70,
    top_k: int = 3,
    max_chars: int = 4000,
    llm_call: Any = None,
    db_path: Any = None,
    profile: str = "",
    priority_realities: Optional[list] = None,
) -> dict:
    """两段式同步归并主流程（决策 41：生产 reality 化，对齐 run_theme_merge 骨架）。

    流程:
      1. S 匹配分候选（决策 38：注入锚 S=0 必进；共现边行为信号；冷启动退化余弦）
      2. 4B 决策（build_reality_decide_prompt → assignments，单向否决）
      3. 按 reality 处理：merge（build_merge_reality_prompt → update_reality）/
         create（build_create_reality_prompt → create_reality）；4B 失败 → 代码兜底
      4. 写库：realities upsert + strand_to_reality + 提问云形心增量

    返回 {"merged": n, "created": n, "failed": n, "reality_ids": [...]}。
    """
    from .store import (create_reality, insert_reality_strand_map,
                        update_reality)
    from .theme import find_s_candidates, resolve_assignments

    call = llm_call or (lambda p: _llm_call_default(p, max_chars))
    merged = created = failed = 0

    # ① 候选生成（v7 S 模型，决策 38）
    cooc_edges: dict = {}
    try:
        from .store import query_cooccurrences
        cooc_edges, _, _ = query_cooccurrences(profile=profile, db_path=db_path)
    except Exception as exc:
        logger.warning("[CA_REALITY] query_cooccurrences failed: %s", exc)

    anchor_realities = list(priority_realities or [])  # 注入集 I（S 锚，α 权重）
    use_s = bool(anchor_realities)
    for st in strands:
        st["candidates"] = []
        if use_s:
            st["candidates"] = find_s_candidates(
                anchor_realities, realities, cooc_edges,
                # fused_ids 恒空（决策 38 期望 beta 分支；reality 无块内融合概念，
                # S 权重退化为仅 alpha——保持行为，不引入未设计机制）
                fused_ids=set(),
                alpha=Config.S_ALPHA, beta=Config.S_BETA,
                r_threshold=Config.S_R_THRESHOLD, top_k=top_k,
                id_key="reality_id")
            continue
        # 冷启动退化（无注入锚：全新话题空注入）→ 余弦候选
        if embed_client is None:
            continue
        text = _strand_embed_text(st)
        if not text.strip():
            continue
        vec = embed_client.embed(text[:500])
        if not vec:
            continue
        st["candidates"] = _cold_start_cosine_candidates(
            vec, realities,
            r_threshold=Config.S_R_THRESHOLD, top_k=top_k)

    # ② 4B 决策（仅含有候选的 strands；无候选 → 自动 new）
    llm_parsed: dict = {}
    with_cand = [s for s in strands if s.get("candidates")]
    if with_cand:
        prompt = build_reality_decide_prompt(with_cand)
        raw = call(prompt)
        if raw:
            llm_parsed = parse_reality_assignments(raw)
    assignments = resolve_assignments(strands, llm_parsed)

    # ③ 按 action 分组
    merge_groups: dict[int, list[dict]] = {}
    new_strands: list[dict] = []
    for st in strands:
        hdl = st.get("hdl", "")
        a = assignments.get(hdl, {"action": "new"})
        if a.get("action") == "merge" and st.get("candidates"):
            cand = st["candidates"][a["target"]]
            merge_groups.setdefault(cand.get("reality_id"), []).append(st)
        else:
            new_strands.append(st)

    touched: set[int] = set()
    conn = None
    try:
        from .store import _get_topic_conn
        conn = _get_topic_conn(db_path)
    except Exception:
        conn = None

    # merge 处理
    for rid, group in merge_groups.items():
        reality = next((r for r in realities if r.get("reality_id") == rid), None)
        if reality is None:
            failed += len(group)
            continue
        try:
            prompt = build_merge_reality_prompt(reality, group, max_chars=max_chars)
            raw = call(prompt)
            result = parse_reality_response(raw)
            used_fallback = False
            if result is None:
                logger.info("[CA_REALITY] merge 4B failed, code fallback for reality %d", rid)
                result = _reality_code_fallback_merge(reality, group)
                used_fallback = True
            elif result.get("merge") is False:
                new_strands.extend(group)
                continue
            fallback = _reality_code_fallback_merge(reality, group)
            new_name = result.get("name") or fallback["name"]
            new_hdl = result.get("hdl") or fallback["hdl"]
            new_cs = result.get("current_status") if isinstance(
                result.get("current_status"), dict) else fallback["current_status"]
            new_cs = enforce_section_limits(new_cs or {})
            timeline_ov = result.get("timeline_overview") or fallback["timeline_overview"]
            timeline_entry = {
                "topic_id": group[0].get("topic_id"),
                "turns": group[0].get("turns") or [],
                "session_id": group[0].get("session_id", ""),
                "overview": timeline_ov,
            }
            centroid_json = None
            if embed_client is not None:
                ctext = " ".join(filter(None, [
                    str(new_name), str(new_hdl),
                    *[str(i) for v in (new_cs or {}).values()
                      if isinstance(v, list) for i in v if str(i).strip()],
                ]))
                if ctext.strip():
                    cv = embed_client.embed(ctext[:500])
                    if cv:
                        centroid_json = json.dumps(cv, ensure_ascii=False)
            update_reality(
                reality_id=rid, name=new_name, hdl=new_hdl,
                current_status=new_cs, timeline_entry=timeline_entry,
                changes=[str(c) for s in group for c in (s.get("changes") or []) if str(c).strip()],
                centroid_json=centroid_json,
                source_strand={"session_id": group[0].get("session_id", ""),
                               "strand_id": group[0].get("strand_id")},
                db_path=db_path,
            )
            for st in group:
                insert_reality_strand_map(st.get("strand_id"), rid,
                                          method="fallback" if used_fallback else "llm",
                                          db_path=db_path)
                if conn is not None:
                    _update_query_centroid(conn, rid, st.get("query_text", ""), embed_client)
            merged += len(group)
            touched.add(rid)
        except Exception as exc:
            logger.warning("[CA_REALITY] merge reality %d failed: %s", rid, exc)
            failed += len(group)

    # create 处理（每 new strand 独立 create，宁分不并）
    for st in new_strands:
        try:
            prompt = build_create_reality_prompt([st], max_chars=max_chars)
            raw = call(prompt)
            result = parse_reality_response(raw)
            fb = _reality_code_fallback_create(st, max_chars)
            new_name = result.get("name") if result and result.get("name") else fb["name"]
            new_hdl = result.get("hdl") if result and result.get("hdl") else fb["hdl"]
            new_cs = result.get("current_status") if result and isinstance(
                result.get("current_status"), dict) else fb["current_status"]
            new_cs = enforce_section_limits(new_cs or {})
            timeline_ov = (result.get("timeline_overview") or "") if result else fb["timeline_overview"]
            timeline_entry = {
                "topic_id": st.get("topic_id"),
                "turns": st.get("turns") or [],
                "session_id": st.get("session_id", ""),
                "overview": timeline_ov,
            }
            centroid_json = None
            if embed_client is not None:
                ctext = " ".join(filter(None, [
                    str(new_name), str(new_hdl),
                    *[str(i) for v in (new_cs or {}).values()
                      if isinstance(v, list) for i in v if str(i).strip()],
                ]))
                if ctext.strip():
                    cv = embed_client.embed(ctext[:500])
                    if cv:
                        centroid_json = json.dumps(cv, ensure_ascii=False)
            rid = create_reality(
                profile=profile, name=new_name, hdl=new_hdl,
                current_status=new_cs, timeline_entry=timeline_entry,
                source_strand={"session_id": st.get("session_id", ""),
                               "strand_id": st.get("strand_id")},
                centroid_json=centroid_json,
                changes=[str(c) for c in (st.get("changes") or []) if str(c).strip()],
                db_path=db_path,
            )
            if rid is None:
                failed += 1
                continue
            insert_reality_strand_map(st.get("strand_id"), rid,
                                      method="llm", db_path=db_path)
            if conn is not None:
                _update_query_centroid(conn, rid, st.get("query_text", ""), embed_client)
            created += 1
            touched.add(rid)
        except Exception as exc:
            logger.warning("[CA_REALITY] create reality failed: %s", exc)
            failed += 1

    if conn is not None:
        try:
            conn.commit()
        except Exception:
            pass
    logger.info("[CA_REALITY] sync merge done: %d merged, %d created, %d failed",
                merged, created, failed)
    return {"merged": merged, "created": created, "failed": failed,
            "reality_ids": sorted(touched)}
