"""ca/flash_reprocess.py — flash 全链路重跑 pilot 核心逻辑（任务书 40）。

pilot 流程（winker 12 session/46 strand 小样本快验）：
  1. 话题检测（沿用 topic_mgr）→ 块
  2. 每块：flash 生成 strand（hdl + ooda 四组 + 归属判断）→ 写 flash_pilot.db
  3. 全部块完成后：flash 按 reality 模型（name/hdl/current_status/timeline，
     决策 37）一步到位生成 reality（stream：首批 build + 后续批 refine）
  4. 注入判断：块首提问 → 提问云匹配（决策 39 镜像）→ 快照注入日志

关键坑（任务书 §四，会话教训）：
  - 聚合键铁律：(session_id, topic_id) 复合键
  - 云端返回 strand id "S7" → parse_sid 容忍前缀
  - json.load 后 dict key 全 str，int key 需显式转换
  - 提问从 state.db messages 取（strand.turns[0] → 该 session 第 N 条 user 消息）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .cloud_llm import call_cloud_llm, lenient_parse, parse_sid

logger = logging.getLogger(__name__)

OODA_LABELS = ["现象与问题", "背景与约束", "决策与方案", "后续行动"]

# 措施 3（2026-08-05）：块首提问截断上限。实测 S38 query_text=29269 字
# （整段 ComfyUI 错误报告）——提问云构建时单样本巨无霸主导均值。
# 落库与云构建双端截断（偶发超长防噪声，正常提问远低于此）。
QUERY_TEXT_MAX = 500

# ── flash strand prompt ──

FLASH_STRAND_PROMPT = """你是对话分析器。给定一个话题块（多轮对话的 Fct 变更记录），识别其中独立的工作线（strand）并生成结构化摘要。

## 输出格式（仅 JSON，不要 markdown 围栏）
{{
  "title": "该话题块整体的一句话摘要",
  "strands": [
    {{
      "hdl": "中文短名（≤30 字）：基于该 strand 的 ooda 内容总结的工作线名称，禁止代码符号名/英文标识符/文件名/函数名，如「连接池与超时配置优化」",
      "turns": [7, 8, 9],
      "ooda": {{
        "现象与问题": ["observed problems or symptoms"],
        "背景与约束": ["context, constraints, or background"],
        "决策与方案": ["decisions made or solutions implemented"],
        "后续行动": ["follow-up actions or next steps"]
      }}
    }}
  ],
  "key_facts": ["已确认结论，最多 5 条"],
  "consumable": true
}}

## 规则
1. 识别 2-6 条独立工作线（strand）。**宁多勿少**：不确定时拆分为独立 strand，避免过度合并（后续 reality 聚合会归并相关 strand）。
2. 每条 strand：hdl（中文短名 ≤30 字，先组织 ooda 内容再总结）、turns（该 strand 出现的轮次号）、ooda（四组，只保留有内容的组）。
3. hdl 禁止抄用输入中的代码符号、变量名、函数名、文件名。
4. 若整块纯确认/元信息无实质内容 → 单 strand 或 consumable:false。
5. key_facts 只收已确认结论，不收推测/备选。
6. title 是有信息量的一句话，不是轮次标题拼接。空/纯确认 → "无新内容"。
7. 全部中文输出（保留必要技术名词）。

## 块首提问（用户进入本话题块时的原始提问，帮助判断工作线归属）
{query_section}
## 话题块内容
{turns}

只输出 JSON 对象，不要其它文字。"""


def build_flash_strand_prompt(
    turns_data: list[dict], query_text: str = "", max_chars: Optional[int] = None
) -> str:
    """构建 flash strand 生成 prompt（块首提问 + 各轮 Fct 内容）。"""
    from .topic_summary import _format_turns_for_prompt

    query_section = (
        f"{query_text.strip()[:300]}" if query_text and query_text.strip() else "（无）"
    )
    prompt = FLASH_STRAND_PROMPT.format(
        query_section=query_section, turns=_format_turns_for_prompt(turns_data)
    )
    if max_chars is not None:
        prompt += (
            f"\n\n## 预算\n整个 JSON 输出必须控制在 {max_chars} 字符内，"
            f"必要时合并/裁剪条目，不得超过预算。"
        )
    return prompt


def _normalize_turns(turns: Any) -> list:
    """turns 字段归一为 int 列表（容忍字符串 JSON / 单 int / 缺失）。"""
    if turns is None:
        return []
    if isinstance(turns, str):
        try:
            turns = json.loads(turns)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(turns, int):
        return [turns]
    if isinstance(turns, list):
        out = []
        for t in turns:
            try:
                out.append(int(t))
            except (TypeError, ValueError):
                continue
        return out
    return []


def parse_flash_strands(text: Optional[str]) -> Optional[dict]:
    """宽松解析 flash strand 输出。

    Returns:
        {"title": str, "strands": [{"hdl","turns","ooda"}], "key_facts": [],
         "consumable": bool}；解析失败 → None
    """
    data = lenient_parse(text)
    if not isinstance(data, dict):
        return None
    raw_strands = data.get("strands")
    if not isinstance(raw_strands, list):
        # 允许 ooda_groups 旧格式包装（兼容 4B 退化输出）
        if data.get("ooda_groups"):
            raw_strands = [{"hdl": data.get("hdl", ""), "ooda": data["ooda_groups"]}]
        else:
            return None
    strands = []
    for st in raw_strands:
        if not isinstance(st, dict):
            continue
        hdl = str(st.get("hdl") or "").strip()
        ooda = st.get("ooda")
        if not isinstance(ooda, dict):
            ooda = {}
        # 只保留合法 OODA 组（固定顺序），缺失组不补空
        clean_ooda = {
            label: list(v) for label, v in ooda.items() if label in OODA_LABELS and v
        }
        if not hdl and not clean_ooda:
            continue
        strands.append(
            {
                "hdl": hdl,
                "turns": _normalize_turns(st.get("turns")),
                "ooda": clean_ooda,
            }
        )
    return {
        "title": str(data.get("title") or "").strip(),
        "strands": strands,
        "key_facts": data.get("key_facts") or [],
        "consumable": bool(data.get("consumable", True)),
    }


# ── reality 一步到位 prompt（决策 37 模型）──

REALITY_CREATE_PROMPT = """你是知识库的现实工作对象（reality）构建助手。给定一批工作线（strand），将它们归并为 reality 并一步到位生成结构化条目。

【reality 定义】现实工作对象（工作线）= 多个语义独立但工作中有关联的 strand 的集合，跨话题块/跨会话持续演进。判定同一工作线的依据是 strand 内容呈现承接/延续关系（诊断→方案→实施→验证 各阶段）。reality 不是语义簇——词面相似但工作无关的不合并。

## 输入 strands（S=id, T=话题块, hdl=状态锚点, OODA 四组）
{strands}

## 构建规则
1. 分组：strand 内容承接/延续同一工作对象 → 归为同一 reality。
2. **宁分不并（最高优先级）**：判断依据必须是「工作承接/延续」——新 strand 的「现象与问题/决策与方案」与已有 reality 的 goals/状态存在**明确推进关系**（诊断→方案→实施→验证 的同一件事）。以下情况**必须拆分**：
   - 仅主题/领域相同（都是 ComfyUI、都是模型）但做的是不同的事 → 不同 reality
   - 词面相似但工作无关 → 不同 reality
   - 不确定 → 独立 reality（错并不可逆，宁可不并；单 strand reality 完全可接受）
   - 宁可输出更多 reality（10-25 个粒度），不要合并成少数大 reality（5 个以内通常是过度合并）
3. 剔除：非工作线的 strand（一次性临时事项、纯状态快照无后续演进）→ discarded_strand_ids。
4. 输出必须覆盖全部 strand 的最终归属，不得遗漏。
5. **成员>4 自检（措施 6，2026-08-05）**：若某 reality 成员数 >4，先自查这些 strand 是否确属**同一工作线**（同一事务的承接/延续阶段）。若只是同领域/同会话的多个独立事务被揉在一起 → 必须拆分；若确属同一工作线的多阶段（诊断→方案→实施→验证）→ 保留合并。**判断依据是工作承接关系，不是主题相似度。**

## 每条 reality 的输出结构（决策 37 数据模型）
{{
  "reality_id": 新编号(1..N),
  "name": "固定事物名：该 reality 是什么工作对象（长期不变；禁止代码符号名）",
  "hdl": "当前状态锚点：一句话现状与进展（可改，随演进重写）",
  "current_status": {{
    "current_state": ["当前状态快照，2-5 条，不是事实堆砌"],
    "key_facts": ["已确认持久事实/结论，带日期，最多 5 条；从成员 strand 的决策与方案中提炼，必须至少 1 条"],
    "goals": ["当前目标：待完成/待验证事项（衔接判定锚），必须至少 1 条"],
    "context": ["相关文件路径/资源（可空）"]
  }},
  "timeline": ["当前 hdl 对应的一句话状态（历史演变由系统维护）"],
  "member_strands": [strand_id...]
}}

## 输出格式（严格 JSON，不要 markdown 围栏）
{{
  "realities": [ ...上述结构... ],
  "discarded_strand_ids": [strand_id...],
  "strand_to_reality": {{"strand_id": reality_id, ...}},
  "stats": {{"reality数": N, "分组数": X, "丢弃strand数": Y}},
  "notes": "构建说明(中文)"
}}"""

REALITY_REFINE_PROMPT = """你是知识库的现实工作对象（reality）融合助手。已有 N 个 reality，新一批 strand 到达，将新 strand 融合进正确 reality 或新建。

## 已有 realities（JSON）
{realities_json}

## 新 strands
{strands}

## 规则
1. 承接判定：strand 的「现象与问题」是否承接某 reality 的 current_status.goals？是否推进/更新该 reality 的状态 → 归入（更新 current_status + 重写 hdl + 旧 hdl 入 timeline）。
2. **宁分不并（最高优先级）**：判断依据必须是「工作承接/延续」——明确推进关系才归入。以下情况**必须新建**：
   - 仅主题/领域相同（都是 ComfyUI、都是模型）但做的是不同的事 → 新 reality
   - 词面相似但工作无关 → 新 reality
   - 不确定 → 新 reality（错并不可逆；单 strand reality 完全可接受）
   - 宁可增加 reality 数量，不要膨胀已有 reality（reality 超过 ~6 条 member 时优先拆分）
3. 剔除：非工作线 strand → discarded_strand_ids。
4. strand_to_reality 必须完整覆盖**本批所有新 strand** 的最终归属（含新建/归入/丢弃判定）。
5. **成员>4 自检（措施 6，2026-08-05）**：合并后若某 reality 成员数 >4，先自查是否确属**同一工作线**（同一事务的承接/延续阶段）。若只是同领域/同会话的多个独立事务被揉在一起 → 必须拆分；若确属同一工作线的多阶段 → 保留合并。**判断依据是工作承接关系，不是主题相似度。**
6. **增量输出（防输出超长截断，最高优先级）**：`realities` 数组**只输出【新增 reality】和【内容发生变化的已有 reality】**（hdl 重写 / 成员增减 / 状态更新 / 合并拆分），**未变化的已有 reality 绝不重复输出**（程序会自动保留其原有记录）。若全量重复输出，输出必超长被截断、整个批次作废重跑。未变化的 reality 只需在 `unchanged_reality_ids` 里列出其 id。

## 输出格式（严格 JSON，不要 markdown 围栏）
{{
  "realities": [
    {{
      "reality_id": 已有则沿用原编号 / 新建则新编号（只列新增或变化的 reality）,
      "name": "固定事物名",
      "hdl": "重写后的当前状态锚点",
      "current_status": {{
        "current_state": ["当前状态快照，2-3 条短句"],
        "key_facts": ["已确认持久事实，带日期，最多 3 条；必须至少 1 条"],
        "goals": ["当前目标，最多 3 条；必须至少 1 条"],
        "context": ["相关文件路径，最多 3 条"]
      }},
      "timeline": ["追加本次更新前的 hdl（旧值），已含则跳过"]
    }}
  ],
  "unchanged_reality_ids": [未变化的已有 reality_id，全部列出],
  "discarded_strand_ids": [strand_id...],
  "strand_to_reality": {{"strand_id": reality_id, ...}},
  "stats": {{"reality数": N, "合并组数": X, "丢弃strand数": Y}},
  "notes": "融合说明(中文)"
}}

⚠️ realities 数组必须包含**全部 reality**（已有全部 + 本次新建），不得只输出本批涉及的条目——下游以本数组为准持久化。"""


# ── refine 两阶段（2026-08-05 全量 tester 重构）──
# 背景：refine 单调用时 flash 全量输出 realities（无视"增量输出"指令，行为铁律），
# 累积 ~200+ reality 后输出必超 max_tokens 截断（实测批次 16/212 reality 失败）。
# 拆两阶段根治：
#   阶段 1 决策层：全量紧凑 ref + 新 strand → 只输出归属映射（s2r/discarded/affected），输出极小
#   阶段 2 内容层：对 affected reality 分批（≤10）生成完整详情，输出永不超限
# 代码兜底：affected = 声明的 ∪ s2r 涉及的已有 reality（s2r 正确则漏报自动补齐）

REALITY_REFINE_DECISION_PROMPT = """你是知识库的现实工作对象（reality）融合助手。已有 N 个 reality，新一批 strand 到达。你的任务：**只做归属决策**——不要生成 reality 详情（详情由后续步骤生成）。

## 已有 realities（紧凑参考：R=id, name, hdl, member_count）
{realities_json}

## 新 strands（S=id, hdl, OODA）
{strands}

## 判定规则
1. 承接判定：strand 的「现象与问题/决策与方案」是否**明确承接**某 reality 的 goals（同一工作线的 诊断→方案→实施→验证 连续阶段）→ 归入该 reality。**仅 name/hdl 语义相近（同领域、同话题）不构成承接**。
2. **宁分不并（最高优先级）**：仅主题/领域相同但做的是不同的事 → 新建；词面相似但工作无关 → 新建；不确定 → 新建（错并不可逆；单 strand reality 完全可接受）。
   - **member_count ≥ 6 的 reality 只接受「明确承接其 goals」的 strand；其余一律新建**——大 reality 持续吞新 strand 即过度归并，严禁（实测 172 成员 reality 由语义相近误归并形成）。
3. 剔除：非工作线 strand（一次性临时事项、纯元信息无工作演进）→ discarded_strand_ids。
4. 新建 reality 用临时编号 "NEW1"、"NEW2"…（按新建顺序），新 strand 映射到该临时编号。
5. 只能归入「已有 realities」中列出的 reality；若承接对象不在列表中（极少，粗筛漏选），按新建处理。

## 输出格式（严格 JSON，不要 markdown 围栏；输出很小，只含决策不含详情）
{{
  "strand_to_reality": {{"strand_id": 已有reality编号 或 "NEW1" 等, ...}},
  "discarded_strand_ids": [strand_id...],
  "affected_reality_ids": [所有需要重新生成详情的 reality（承接了新 strand 的已有 reality + 全部 NEW*）；未变化的不列],
  "notes": "归属说明(中文，简短)"
}}"""


REALITY_DETAIL_PROMPT = """你是知识库的现实工作对象（reality）条目生成助手。为指定的 reality 生成完整结构化条目（只输出这些 reality，不要输出其他内容）。reality_id 已由系统分配，你只需输出 name/hdl/current_status。

## 需要生成的 reality
{detail_items}

## 每条 reality 的输出结构
{{
  "reality_id": 对应输入中的编号,
  "name": "固定事物名：该 reality 是什么工作对象（长期不变；禁止代码符号名）",
  "hdl": "当前状态锚点：一句话现状与进展（随演进重写）",
  "current_status": {{
    "current_state": ["当前状态快照，2-3 条短句"],
    "key_facts": ["已确认持久事实/结论，带日期，最多 3 条；必须至少 1 条"],
    "goals": ["当前目标：待完成/待验证事项（衔接判定锚），最多 3 条；必须至少 1 条"],
    "context": ["相关文件路径/资源，最多 3 条（可空）"]
  }}
}}

## 规则
1. 已有 reality：融合新成员 strand 的进展，重写 hdl/current_status；name 保持原事物名（除非成员演进明确改名）。
2. 新建 reality：从成员 strand 的 ooda 归纳 name/hdl/current_status（key_facts 必须至少 1 条带日期事实）。
3. **不要拆分**：成员归属由决策层决定（strand_to_reality 已定），本步只生成详情。若你判断某 reality 成员>4 且确属不同工作线揉杂 → 在 notes 说明"建议拆分"（决策层下一轮会处理），**不要输出新编号 reality**——拆分产物无成员映射会成孤儿。
4. 成员>4 自检（措施 6）：合并后成员数 >4 时，自查这些 strand 是否确属同一工作线（同一事务的承接/延续多阶段）。只是同领域多个独立事务被揉在一起 → 在 notes 注明建议拆分。**判断依据是工作承接关系，不是主题相似度。**

## 输出格式（严格 JSON 数组，不要 markdown 围栏）
[
  {{"reality_id": ..., "name": ..., "hdl": ..., "current_status": {{...}}}},
  ...
]"""


def build_reality_decision_prompt(strands: list[dict],
                                  ref_realities: list[dict]) -> str:
    """决策层 prompt：全量/候选紧凑 ref + 新 strand → 归属决策（输出极小）。"""
    strand_txt = "\n\n".join(_fmt_strand(s) for s in strands)
    realities_txt = "\n\n".join(
        _fmt_reality(r, compact=True) for r in ref_realities)
    return REALITY_REFINE_DECISION_PROMPT.format(
        realities_json=realities_txt, strands=strand_txt)


def build_reality_detail_prompt(items: list[dict],
                                batch_strands: list[dict]) -> str:
    """内容层 prompt：affected reality 详情生成。

    items: [{"rid": int, "old": dict|None, "strand_ids": [int...]}]
    batch_strands: 本批 strand 全集（查成员详情）。
    """
    strand_lookup = {s["id"]: s for s in batch_strands}
    parts = []
    for it in items:
        rid = it["rid"]
        old = it.get("old")
        head = f"### reality {rid}"
        if old:
            head += "（已有，当前简况）\n" + _fmt_reality(old, compact=True)
        else:
            head += "（新建，无旧信息）"
        member_txt = []
        for sid in it.get("strand_ids") or []:
            s = strand_lookup.get(sid)
            if s is not None:
                member_txt.append(_fmt_strand(s))
        parts.append(head + "\n成员 strands:\n" + "\n".join(member_txt))
    return REALITY_DETAIL_PROMPT.format(detail_items="\n\n".join(parts))


def parse_reality_decision(text: Optional[str]) -> Optional[dict]:
    """解析决策层输出。

    Returns:
        {"s2r": {int: int|str(NEW*)}, "discarded": [int], "affected": set,
         "notes": str} | None
    """
    data = lenient_parse(text)
    if not isinstance(data, dict):
        return None
    s2r_raw = data.get("strand_to_reality")
    if not isinstance(s2r_raw, dict):
        return None
    s2r: dict = {}
    for k, v in s2r_raw.items():
        try:
            sid = parse_sid(k)
        except (TypeError, ValueError):
            continue
        vs = str(v).strip()
        if vs.upper().startswith("NEW"):
            s2r[sid] = vs
        else:
            try:
                s2r[sid] = parse_sid(vs)
            except (TypeError, ValueError):
                continue
    if not s2r:
        return None
    discarded = []
    for d in data.get("discarded_strand_ids") or []:
        try:
            discarded.append(parse_sid(d))
        except (TypeError, ValueError):
            continue
    affected: set = set()
    for a in data.get("affected_reality_ids") or []:
        vs = str(a).strip()
        if vs.upper().startswith("NEW"):
            affected.add(vs)
        else:
            try:
                affected.add(parse_sid(vs))
            except (TypeError, ValueError):
                continue
    return {
        "s2r": s2r,
        "discarded": discarded,
        "affected": affected,
        "notes": data.get("notes", ""),
    }


def parse_reality_detail(text: Optional[str]) -> Optional[list]:
    """解析内容层输出（reality 详情数组）。失败 → None。

    ⚠️ 不用 lenient_parse：它按 {…} 截取（dict 假设），顶层 JSON 数组
    （内容层输出契约）会截到数组内部 → 必失败。此处剥离围栏后直接 loads。
    """
    if not text:
        return None
    t = text.strip()
    # 只剥离首尾 markdown 围栏（CR-11: 原 re.sub 全局替换会误删正文中的 ```）
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t.replace("```", "")
        t = t.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(t)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    clean = []
    for r in data:
        if not isinstance(r, dict):
            continue
        try:
            rid = parse_sid(r.get("reality_id"))
        except (TypeError, ValueError):
            continue
        r = dict(r)
        r["reality_id"] = rid
        clean.append(r)
    return clean or None


def _fmt_strand(s: dict) -> str:
    parts = [f"S{s['id']} [T{s.get('topic_id','?')}] hdl: {str(s.get('hdl',''))[:150]}"]
    for grp in OODA_LABELS:
        items = (s.get("ooda") or {}).get(grp) or []
        if items:
            parts.append(f"  {grp}: {'; '.join(str(i)[:120] for i in items[:3])}")
    return "\n".join(parts)


def _fmt_reality(r: dict, compact: bool = False) -> str:
    cs = r.get("current_status") or {}
    parts = [f"R{r['reality_id']} name: {str(r.get('name',''))[:60]}"]
    parts.append(f"  hdl: {str(r.get('hdl',''))[:120]}")
    ms = r.get("member_strands") or []
    if compact:
        # 紧凑参考（refine prompt 防超长）：不展开 current_status 细节。
        # goals 是承接判定锚（决策 37 §5.1）——必须给决策层，否则 flash 只能
        # 凭 name/hdl 语义相近误判承接（实测大 reality 吞 172 strand 过度归并）
        parts.append(f"  member_count: {len(ms)}")
        goals = cs.get("goals") or []
        if goals:
            parts.append("  goals: " + "; ".join(str(g)[:60] for g in goals[:2]))
        return "\n".join(parts)
    for k in ("current_state", "key_facts", "goals", "context"):
        v = cs.get(k) or []
        if v:
            parts.append(f"  {k}: {'; '.join(str(i)[:100] for i in v[:3])}")
    if ms:
        parts.append("  member_strands: " + ", ".join(f"S{m}" for m in ms[:10]))
    return "\n".join(parts)


def build_reality_create_prompt(strands: list[dict]) -> str:
    strand_txt = "\n\n".join(_fmt_strand(s) for s in strands)
    return REALITY_CREATE_PROMPT.format(strands=strand_txt)


def build_reality_refine_prompt(strands: list[dict], realities: list[dict],
                                max_ref: int = 40) -> str:
    """refine prompt：参考 realities 用紧凑格式（防输出超长截断）。

    max_ref：参考上限（realities 膨胀时裁剪，只保留最近的 N 条；
    按 reality_id 排序稳定，pilot 阶段无时间信息可用）。
    """
    ref = realities[-max_ref:] if len(realities) > max_ref else realities
    strand_txt = "\n\n".join(_fmt_strand(s) for s in strands)
    realities_txt = "\n\n".join(_fmt_reality(r, compact=True) for r in ref)
    return REALITY_REFINE_PROMPT.format(
        realities_json=realities_txt, strands=strand_txt
    )


def parse_reality_build_result(text: Optional[str]) -> Optional[dict]:
    """宽松解析 flash reality 构建/融合结果。

    Returns:
        {"realities": [...], "strand_to_reality": {int: int}, "discarded_strand_ids": []}
        解析失败 → None
    """
    data = lenient_parse(text)
    if not isinstance(data, dict):
        return None
    realities = data.get("realities")
    if not isinstance(realities, list):
        return None
    s2r_raw = data.get("strand_to_reality")
    s2r = {}
    if isinstance(s2r_raw, dict):
        for k, v in s2r_raw.items():
            try:
                s2r[parse_sid(k)] = parse_sid(v)
            except (TypeError, ValueError):
                continue
    discarded = []
    for d in data.get("discarded_strand_ids") or []:
        try:
            discarded.append(parse_sid(d))
        except (TypeError, ValueError):
            continue
    # 归一 realities 内部 id 字段（json.load 后全 str；云端可能 "S1"/"SS1"）
    clean_realities = []
    for r in realities:
        if not isinstance(r, dict):
            continue
        try:
            rid = parse_sid(r.get("reality_id"))
        except (TypeError, ValueError):
            continue
        ms = []
        for m in r.get("member_strands") or []:
            try:
                ms.append(parse_sid(m))
            except (TypeError, ValueError):
                continue
        r = dict(r)
        r["reality_id"] = rid
        r["member_strands"] = ms
        clean_realities.append(r)
    return {
        "realities": clean_realities,
        "strand_to_reality": s2r,
        "discarded_strand_ids": discarded,
        "notes": data.get("notes", ""),
    }


# ── flash_pilot.db schema ──

_FLASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS strands (
    strand_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    topic_id      INTEGER NOT NULL,
    hdl           TEXT,
    turns         TEXT    NOT NULL DEFAULT '[]',
    ooda_json     TEXT    DEFAULT '{}',
    changes_json  TEXT    DEFAULT '[]',
    key_facts_json TEXT   DEFAULT '[]',
    query_text    TEXT    NOT NULL DEFAULT '',   -- 块首提问（决策 39 基建）
    status        TEXT    NOT NULL DEFAULT 'completed',
    created_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_flash_strands_key
    ON strands (session_id, topic_id);

CREATE TABLE IF NOT EXISTS realities (
    reality_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    hdl             TEXT,
    current_status  TEXT    DEFAULT '{}',
    timeline        TEXT    DEFAULT '[]',
    source_strands  TEXT    DEFAULT '[]',
    profile         TEXT    NOT NULL DEFAULT '',
    created_at      REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS strand_to_reality (
    strand_id   INTEGER NOT NULL,
    reality_id  INTEGER NOT NULL,
    PRIMARY KEY (strand_id)
);

CREATE TABLE IF NOT EXISTS inject_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    topic_id    INTEGER,
    query       TEXT,               -- 块首提问
    picked      TEXT DEFAULT '[]',  -- JSON array [reality_id...]
    empty       INTEGER DEFAULT 0,  -- 1 = 空注入（宁缺勿错）
    created_at  REAL
);
"""


def init_flash_db(db_path: Path) -> sqlite3.Connection:
    """初始化（或复用）flash_pilot.db，返回连接。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_FLASH_SCHEMA)
    return conn


def write_flash_strand(
    conn: sqlite3.Connection,
    session_id: str,
    topic_id: int,
    hdl: str = "",
    turns: Optional[list] = None,
    ooda: Optional[dict] = None,
    changes: Optional[list] = None,
    key_facts: Optional[list] = None,
    query_text: str = "",
    status: str = "completed",
) -> Optional[int]:
    """写入一条 flash strand，返回 strand_id。

    query_text 截断到 QUERY_TEXT_MAX（措施 3：偶发超长块首提问防噪声）。
    """
    q = (query_text or "")[:QUERY_TEXT_MAX]
    cur = conn.execute(
        "INSERT INTO strands (session_id, topic_id, hdl, turns, ooda_json, "
        "changes_json, key_facts_json, query_text, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            topic_id,
            hdl,
            json.dumps(turns or [], ensure_ascii=False),
            json.dumps(ooda or {}, ensure_ascii=False),
            json.dumps(changes or [], ensure_ascii=False),
            json.dumps(key_facts or [], ensure_ascii=False),
            q,
            status,
            time.time(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def write_flash_reality(
    conn: sqlite3.Connection,
    name: str,
    hdl: str,
    current_status: Optional[dict] = None,
    timeline: Optional[list] = None,
    source_strands: Optional[list] = None,
    profile: str = "",
) -> Optional[int]:
    """写入一条 flash reality，返回 reality_id。

    ⚠️ 防膨胀守卫：current_status 各段超限（key_facts>5/current_state>5/
    goals>8/context>8）→ enforce_section_limits 锚点优先截断（对齐 4B
    路径 reality.py 的 N2 守卫——flash 输出超限时同样兜底）。
    """
    cs = current_status or {}
    try:
        from .reality import enforce_section_limits
        cs = enforce_section_limits(dict(cs))
    except ImportError:
        pass  # 守卫不可用时原样写入（不阻断主流程）
    now = time.time()
    cur = conn.execute(
        "INSERT INTO realities (name, hdl, current_status, timeline, "
        "source_strands, profile, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            name,
            hdl,
            json.dumps(cs, ensure_ascii=False),
            json.dumps(timeline or [], ensure_ascii=False),
            json.dumps(source_strands or [], ensure_ascii=False),
            profile,
            now,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def write_strand_to_reality(
    conn: sqlite3.Connection, strand_id: int, reality_id: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO strand_to_reality (strand_id, reality_id) "
        "VALUES (?,?)",
        (strand_id, reality_id),
    )
    conn.commit()


def write_inject_log(
    conn: sqlite3.Connection,
    session_id: str,
    topic_id: int,
    query: str,
    picked: Optional[list] = None,
    empty: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO inject_log (session_id, topic_id, query, picked, empty, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (
            session_id,
            topic_id,
            (query or "")[:500],
            json.dumps(picked or [], ensure_ascii=False),
            1 if empty else 0,
            time.time(),
        ),
    )
    conn.commit()


# ── 注入快照（决策 39：提问域成员云匹配 + 空注入宁缺勿错）──


def build_question_cloud(strands: list[dict], embed_fn) -> Optional[list]:
    """reality 提问云 = 成员 strand 块首提问 embed 均值（决策 39 §六）。

    - 同块同提问去重：同块多 strand 共享同一提问只计一次（防块内 strand
      数偏置云均值）
    - 短提问（<4 字）过滤
    返回 embed 均值列表；无有效提问 → None。
    """
    seen: set = set()
    vecs = []
    for s in strands:
        q = str(s.get("query_text") or "").strip()[:QUERY_TEXT_MAX]
        if len(q) < 4 or q in seen:
            continue
        seen.add(q)
        v = embed_fn(q)
        if v:
            vecs.append(v)
    if not vecs:
        return None
    dim = len(vecs[0])
    mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    return mean


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def match_question_clouds(
    query: str, q_emb: Optional[list], clouds: dict, k: int = 3
) -> list:
    """提问 → 全库 reality 提问云 top-k（cos 降序）。

    Returns:
        [(reality_id, cos), ...]；无云/无提问 → []
    """
    if not query or not q_emb or not clouds:
        return []
    scored = []
    for rid, cloud in clouds.items():
        if not cloud:
            continue
        c = _cosine(q_emb, cloud)
        if c > 0:
            scored.append((rid, c))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def snapshot_injections(
    conn: sqlite3.Connection,
    strands: list[dict],
    realities: list[dict],
    s2r: dict,
    embed_fn,
    k: int = 3,
) -> dict:
    """重跑时快照注入结果（决策 39 镜像 A）→ inject_log。

    对每个话题块（(session_id, topic_id) 复合键）：块首提问 → 与全库
    reality 提问云比 cos → top-3。全库无云/提问过短 → 空注入（宁缺勿错）。

    Returns: {"blocks": N, "matched": M, "empty": E} 统计。
    """
    # 1) 每个 reality 的提问云（成员 strand query_text）
    member_by_reality: dict = {}
    for strand_id, reality_id in s2r.items():
        member_by_reality.setdefault(reality_id, []).append(strand_id)
    by_id = {s["id"]: s for s in strands}
    clouds: dict = {}
    for rid, member_ids in member_by_reality.items():
        members = [by_id[i] for i in member_ids if i in by_id]
        cloud = build_question_cloud(members, embed_fn)
        if cloud:
            clouds[rid] = cloud

    # 2) 按块（(session_id, topic_id)）聚合首提问
    block_query: dict = {}
    for s in strands:
        key = (s.get("session_id"), s.get("topic_id"))
        q = str(s.get("query_text") or "").strip()
        if key not in block_query and len(q) >= 4:
            block_query[key] = q

    n_matched = 0
    for (sess, tid), q in block_query.items():
        q_emb = embed_fn(q)
        top = match_question_clouds(q, q_emb, clouds, k=k)
        picked = [rid for rid, _ in top]
        if not top:
            write_inject_log(conn, sess, tid, q, picked=[], empty=True)
        else:
            n_matched += 1
            write_inject_log(conn, sess, tid, q, picked=picked, empty=False)

    return {"blocks": len(block_query), "matched": n_matched,
            "empty": len(block_query) - n_matched}
