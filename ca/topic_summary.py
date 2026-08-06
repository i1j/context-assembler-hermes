"""ca/topic_summary.py — 话题块摘要引擎 (v5.10)

取代 OV Memory Provider 的话题摘要管线。
- summarize_topic_chunk: 从 Fct 数据生成结构化摘要（4B + 规则）
- format_topic_carryover: 格式化为 <topic_carryover> 注入块
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

# ── 4B Prompt ──

TOPIC_SUMMARIZE_PROMPT = """Given the topic changes grouped by turn and OODA category, produce a structured summary.

Each change may carry a [stage_tag] prefix: [已实施] [计划] [探讨] [已取消] [评估中]

Each turn may also have supplementary sections:
- 额外信息 > 共识: decisions agreed upon that are not in the changes list
- 额外信息 > 客观事实: verified facts/observations
- 额外信息 > 新增素材: new user input context or material

Incorporate these supplementary items into the summary just like changes — merge related items, deduplicate, and order logically.

## Output format (JSON only)

```json
{{
  "title": "One-sentence summary of what this topic block is about",
  "strands": [
    {{
      "hdl": "中文短名：基于该 strand 的 ooda 内容总结的工作线名称（禁止代码符号名/英文标识符），如「连接池与超时配置优化」",
      "turns": [7, 8, 9],
      "theme_ref": 30,
      "ooda": {{
        "现象与问题": ["observed problems or symptoms in this strand"],
        "背景与约束": ["context, constraints, or background"],
        "决策与方案": ["decisions made or solutions implemented"],
        "后续行动": ["follow-up actions or next steps"]
      }}
    }}
  ],
  "key_facts": [
    "Confirmed conclusions derived from the changes (action→outcome reasoning)",
    "Keep at most 5 items"
  ],
  "consumable": true
}}
```

Rules:
- Identify 2-6 distinct work strands in this topic block. **When in doubt, split into separate strands** — prefer over-merging into one (a wiki entry aggregation pass will group related strands under the same theme later).
- Each strand: hdl (short name), turns (which turn numbers it appears in — use the "# 轮次 N" markers in the input), ooda (4 groups, only include groups that have content).
- theme_ref (optional): if the 候选主题参考 section lists candidate themes, and this strand is the SAME piece of work as one candidate (continuation / deepening / fixing the same problem), output that candidate's theme_id; otherwise OMIT the theme_ref field entirely (uncertain → omit; better to create a new theme than wrongly merge).
- ⚠️ hdl 命名规范（必须先组织该 strand 的 ooda 内容，再基于 ooda 总结 hdl）：
  - ✅ 正确示例："wiki entry 核心粒度与字段设计"、"连接池与超时配置优化"（中文语义短名，概括该工作线做了什么）
  - ❌ 错误示例（禁止）："embedding_service"、"topic_find_ca"、"consensus"、"l2_clustering"（代码符号名/英文标识符/文件名/函数名）
  - hdl 必须是对该 strand 的 ooda 四组内容（现象与问题/背景与约束/决策与方案/后续行动）的语义总结，概括这条工作线做的事
  - 禁止直接抄用输入中的代码符号、变量名、函数名、文件名作为 hdl
- If the block is purely confirmatory/meta with no real content, output a single strand or consumable:false.
- title: informative single sentence, NOT concatenated turn headings. If empty/purely confirmatory → "无新内容"
- key_facts: only confirmed findings/conclusions, NOT speculation or alternatives
- consumable: false when all changes are already covered in previous topics or the topic is purely meta/confirmatory with no new actions
- output size: {budget}

{candidate_refs}
Input:
{turns}

Respond ONLY with the JSON object, no extra text."""


def _budget_note(max_chars: Optional[int]) -> str:
    """预算指令：控制 4B 输出体积（v8）。"""
    if max_chars is not None:
        return (f"the entire JSON output must fit within {max_chars} characters "
                f"(title + strands + key_facts). Merge or trim items "
                f"as needed; never exceed the budget.")
    return ("keep it concise and complete; no hard character limit. "
            "Prefer 2-6 strands / 5 key_facts as a target.")


def _format_turns_for_prompt(turns_data: list[dict]) -> str:
    """将各轮 Fct 数据格式化为 prompt 的轮次段落（供摘要/融合 prompt 复用）。"""
    OODA_LABELS = ["现象与问题", "背景与约束", "决策与方案", "后续行动"]

    sections = []
    sections.append("changes:")
    for td in turns_data:
        turn = td.get("turn", 0)
        changes = td.get("changes", [])
        tags = td.get("tags", {})
        ooda_tags = td.get("ooda_tags", {})

        # 按 OODA 归类
        ooda_groups: dict[str, list[str]] = {l: [] for l in OODA_LABELS}
        uncategorized: list[str] = []

        for c in changes:
            if not isinstance(c, str):
                continue
            tag = tags.get(c, "")
            ooda = ooda_tags.get(c, "")
            prefix = ""
            if tag:
                prefix = f"[{tag}] "
            if ooda in ooda_groups:
                ooda_groups[ooda].append(f"    - {prefix}{c}")
            else:
                uncategorized.append(f"    - {prefix}{c}")

        sections.append("")
        sections.append(f"# 轮次 {turn}")
        for label in OODA_LABELS:
            items = ooda_groups[label]
            if items:
                sections.append(f"    ## {label}")
                sections.extend(items)
        if uncategorized:
            sections.append("    ## 其他")
            sections.extend(uncategorized)

        # 补充字段：共识 / 客观事实 / 新增素材（只在有数据时输出）
        consensus = td.get("consensus", [])
        key_facts = td.get("key_facts_supp", [])
        new_mat = td.get("new_materials", [])
        if consensus or key_facts or new_mat:
            sections.append(f"# 轮次 {turn} 额外信息")
            if consensus:
                sections.append("    ## 共识")
                for item in consensus:
                    sections.append(f"    - {item}")
            if key_facts:
                sections.append("    ## 客观事实")
                for item in key_facts:
                    sections.append(f"    - {item}")
            if new_mat:
                sections.append("    ## 新增素材")
                for item in new_mat:
                    sections.append(f"    - {item}")

    return "\n".join(sections)


def _format_candidate_refs(candidate_themes: Optional[list]) -> str:
    """候选主题参考段（v6.5.3）：切换注入的 theme 摘要，供 4B 判断 strand 归属。

    无候选 → 空串（prompt 零额外输入）；有候选 → 「候选主题参考」段，
    声明仅参考非强制 + theme_ref 判定规则（宁新建不错并）。
    """
    if not candidate_themes:
        return ""
    lines = ["## 候选主题参考（仅参考，非强制归属）"]
    for t in candidate_themes[:3]:
        title = (t.get("title") or "").strip()
        overview = (t.get("overview") or "").strip()
        lines.append(f"- theme_id={t.get('theme_id')}: {title}")
        if overview:
            lines.append(f"  overview: {overview[:200]}")
    lines.append(
        "判断：若某 strand 与某候选主题是「同一件事」（延续/深化/修复同一问题），"
        "在该 strand 输出 theme_ref=<该 theme_id>；否则不输出 theme_ref（宁新建，不错并）。"
    )
    return "\n".join(lines)


def build_summarize_prompt(
    turns_data: list[dict],
    max_chars: Optional[int] = None,
    candidate_themes: Optional[list] = None,
) -> str:
    """从该话题块各轮的 Fct 数据构建 4B prompt。

    格式:
        changes:

        # 轮次 1
            ## 决策与方案
            - [已实施] 连接池扩容
            ## 现象与问题
            - 发现连接池耗尽
            ## 后续行动
            - 监控超时命中率

        # 轮次 1 额外信息
            ## 共识
            - 确定连接池上限
            ## 客观事实
            - 连接池耗尽导致超时
            ## 新增素材
            - 用户反馈连接池不足

    max_chars (v8): 注入预算——4B 输出必须 ≤ max_chars 字符。
    candidate_themes (v6.5.3): 切换时注入的候选主题（title+overview），
        供 4B 生成 strand 时判断 theme 归属（theme_ref 字段）。
    """
    turns_block = _format_turns_for_prompt(turns_data)
    return TOPIC_SUMMARIZE_PROMPT.format(
        turns=turns_block,
        budget=_budget_note(max_chars),
        candidate_refs=_format_candidate_refs(candidate_themes),
    )


def call_llm_raw(
    prompt: str,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Optional[str]:
    """4B 原始调用（通用）：返回响应文本，不做 JSON 解析。

    v6.5: theme 链路（ca/theme.py）复用此请求逻辑；
    与 call_llm_for_summary 的区别仅在返回 raw text（由调用方自行解析）。
    """
    import urllib.request

    llm_start = time.monotonic()
    req_body = {
        "model": Config.LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": (
                num_predict if num_predict is not None
                else Config.TOPIC_SUMMARY_MAX_TOKENS),
            "temperature": (
                temperature if temperature is not None
                else Config.L1_TEMPERATURE),
        },
        "keep_alive": -1,
    }
    req_body["think"] = Config.LLM_THINK if Config.LLM_THINK is not None else False
    payload = json.dumps(req_body).encode()

    response_text = ""
    for attempt in range(Config.LLM_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{Config.LLM_ENDPOINT.rstrip('/')}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
            response_text = data.get("response", "")
            if response_text.strip():
                break
        except Exception as e:
            logger.warning(
                "[CA_LLM] attempt %d/%d failed: %s",
                attempt + 1, Config.LLM_MAX_RETRIES, e,
            )
            time.sleep(2 ** attempt)

    elapsed_ms = int((time.monotonic() - llm_start) * 1000)
    logger.info("[CA_LLM] LLM call completed in %dms, response_len=%d",
                elapsed_ms, len(response_text))

    if not response_text.strip():
        logger.warning("[CA_LLM] LLM returned empty response")
        return None
    return response_text


def call_llm_for_summary(prompt: str) -> Optional[Dict[str, Any]]:
    """调用 LLM (4B) 生成话题摘要，返回解析后的 JSON dict。"""
    import urllib.request

    llm_start = time.monotonic()
    req_body = {
        "model": Config.LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            # v6.4.1: 独立 num_predict——L1_MAX_TOKENS=2048 是 F-stage 参数，
            # 多 strand 输出会被 2048 截断（stop=length → JSON parse 失败 → fallback）
            "num_predict": Config.TOPIC_SUMMARY_MAX_TOKENS,
            "temperature": Config.L1_TEMPERATURE,
        },
        "keep_alive": -1,
    }
    req_body["think"] = Config.LLM_THINK if Config.LLM_THINK is not None else False
    payload = json.dumps(req_body).encode()

    response_text = ""
    for attempt in range(Config.LLM_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{Config.LLM_ENDPOINT.rstrip('/')}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
            response_text = data.get("response", "")
            if response_text.strip():
                break
        except Exception as e:
            logger.warning(
                "[CA_TOPIC_SUM] LLM attempt %d/%d failed: %s",
                attempt + 1, Config.LLM_MAX_RETRIES, e,
            )
            time.sleep(2 ** attempt)

    elapsed_ms = int((time.monotonic() - llm_start) * 1000)
    logger.info("[CA_TOPIC_SUM] LLM call completed in %dms, response_len=%d",
                elapsed_ms, len(response_text))

    if not response_text.strip():
        logger.warning("[CA_TOPIC_SUM] LLM returned empty response")
        return None

    return parse_summary_response(response_text)


def _call_llm_for_title(prompt: str) -> Optional[str]:
    """调用 LLM 生成标题文本（非 JSON），返回原始文本或 None。"""
    import urllib.request

    llm_start = time.monotonic()
    req_body = {
        "model": Config.LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            # v6.4.1: 独立 num_predict（与 call_llm_for_summary 对齐）
            "num_predict": Config.TOPIC_SUMMARY_MAX_TOKENS,
            "temperature": Config.L1_TEMPERATURE,
        },
        "keep_alive": -1,
    }
    req_body["think"] = Config.LLM_THINK if Config.LLM_THINK is not None else False
    payload = json.dumps(req_body).encode()

    for attempt in range(Config.LLM_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{Config.LLM_ENDPOINT.rstrip('/')}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
            response_text = data.get("response", "").strip()
            if response_text:
                break
        except Exception as e:
            logger.warning(
                "[CA_TOPIC_SUM] Title LLM attempt %d/%d failed: %s",
                attempt + 1, Config.LLM_MAX_RETRIES, e,
            )
            time.sleep(2 ** attempt)
    else:
        return None

    elapsed_ms = int((time.monotonic() - llm_start) * 1000)
    logger.info("[CA_TOPIC_SUM] Title LLM call: %dms, %d chars", elapsed_ms, len(response_text))

    # Clean: strip quotes and extra whitespace
    title = response_text.strip().strip('"').strip("'").strip()
    return title


def _lenient_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """宽松 JSON 解析：处理 Ollama 截断导致的常见 JSON 错误。

    处理场景:
    1. Unterminated string at end（字符串内容被截断）→ 补闭合引号
    2. Missing closing brackets → 补缺失括号
    3. Trailing comma → 删末尾逗号
    """
    original = text
    # Try common fixes
    fixes = [
        # Unterminated string at end: add close quote
        lambda t: t + '"' if t.count('"') % 2 == 1 else t,
        # Missing closing braces/brackets
        lambda t: _fix_truncated_json(t),
    ]

    for fix in fixes:
        candidate = fix(text)
        try:
            data = json.loads(candidate)
            logger.debug("[CA_TOPIC_SUM] Lenient JSON parse succeeded after fix: %s",
                         _summary_fix_name(fix))
            return data
        except json.JSONDecodeError:
            continue

    # Last resort: try emulating missing closing braces
    for depth in range(5, 0, -1):
        try:
            data = json.loads(text + "}" * depth)
            return data
        except json.JSONDecodeError:
            pass
        try:
            data = json.loads(text + "]" * depth + "}" * depth)
            return data
        except json.JSONDecodeError:
            pass

    return None


def _fix_truncated_json(text: str) -> str:
    """尝试修复截断的 JSON：计算括号平衡并补缺失。"""
    stack = []
    in_str = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in '{[':
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    # Re-add closing brackets in reverse order
    closing = []
    for ch in reversed(stack):
        closing.append('}' if ch == '{' else ']')
    return text + ''.join(closing)


def _summary_fix_name(fix) -> str:
    """Get a short name for the fix function for logging."""
    import types
    if isinstance(fix, types.LambdaType):
        return fix.__name__ if hasattr(fix, '__name__') else 'lambda'
    return fix.__name__


def parse_summary_response(response_text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 响应中解析出结构化摘要 dict。"""
    text = response_text.strip()
    # 尝试提取 JSON 块（可能被 markdown 包裹）
    if "```json" in text:
        text = text.split("```json")[1]
        text = text.split("```")[0]
    elif "```" in text:
        text = text.split("```")[1]
        text = text.split("```")[0]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _lenient_json_parse(text)
        if data is None:
            logger.warning("[CA_TOPIC_SUM] Failed to parse LLM response as JSON")
            return None

    changes = data.get("changes", [])
    key_facts = data.get("key_facts", [])

    if isinstance(changes, list) and isinstance(key_facts, list):
        result = {"changes": changes, "key_facts": key_facts}
        title = data.get("title", "")
        if title and isinstance(title, str):
            result["title"] = title
        # v6.4: 透传 ooda_groups / strands（Bug 1 修复——之前被 parse 丢弃）
        ooda_groups = data.get("ooda_groups")
        if isinstance(ooda_groups, dict) and ooda_groups:
            result["ooda_groups"] = ooda_groups
        strands = data.get("strands")
        if isinstance(strands, list) and strands:
            result["strands"] = strands
        # consumable: 4B 判定，缺省 true（保守：不影响现有工作流）
        consumable = data.get("consumable")
        if isinstance(consumable, bool):
            result["consumable"] = consumable
        else:
            result["consumable"] = True
        return result

    logger.warning("[CA_TOPIC_SUM] LLM response missing expected fields: %s", list(data.keys()))
    return None


# ── 规则处理 ──

# P0-2 (v6.4.3): hdl 长度上限——strand 35 超长 hdl（91 chars）根因：
# _build_hdl 拼接首末轮 Fct hdl，而 Fct 行级 Hdl 本身是完整句子（60+ 字），
# 无长度约束直接落库。上限 30 chars，优先按标点/逗号切段。
_HDL_MAX_LEN = 30


def _truncate_hdl(text: str, max_len: int = _HDL_MAX_LEN) -> str:
    """hdl 长度上限：优先句末标点/逗号切段，硬截断兜底。"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    for sep in ("。", "；", ". ", "; ", "，", "、", ", "):
        idx = text.find(sep)
        if 0 < idx <= max_len:
            return text[:idx].rstrip()
    return text[:max_len]


def _first_change_summary(turns_data: list[dict]) -> str:
    """P1-2: 取首条 change 的核心短名（去 stage_tag 前缀，截断 ≤30）。"""
    _STAGE_PREFIXES = ("[已实施] ", "[计划] ", "[探讨] ", "[已取消] ", "[评估中] ",
                       "已实施: ", "计划: ", "探讨: ", "已取消: ", "评估中: ",
                       "[待分类] ")
    for td in turns_data:
        for c in td.get("changes", []):
            if isinstance(c, dict):
                core = c.get("core_change", "") or c.get("change", "")
            else:
                core = str(c) if c else ""
            core = core.strip()
            for p in _STAGE_PREFIXES:
                if core.startswith(p):
                    core = core[len(p):]
                    break
            if core:
                return _truncate_hdl(core)
    return ""


def _build_hdl(turns_data: list[dict]) -> str:
    """规则：拼接首轮+最后轮 hdl，取首句（超长截断 ≤30 chars）。"""
    if not turns_data:
        return ""
    first_hdl = turns_data[0].get("hdl", "").strip()
    last_hdl = turns_data[-1].get("hdl", "").strip()
    if first_hdl and last_hdl and first_hdl != last_hdl:
        combined = f"{first_hdl} → {last_hdl}"
    else:
        combined = first_hdl or last_hdl
    # 取首句
    for sep in ("。", ". ", "；", "; "):
        if sep in combined:
            combined = combined.split(sep)[0] + ("。" if sep in ("。", "；") else "")
            break
    return _truncate_hdl(combined)


def _extract_user_requests(turns_data: list[dict]) -> list[str]:
    """规则：从各轮的 user_requests 字段提取，去重，最多 3 条。

    纯对话降级：无 user_request 时从 hdl 或 core_change 摘录。
    """
    seen: set[str] = set()
    results: list[str] = []

    # 优先走独立 user_requests 字段
    for td in turns_data:
        for ur in td.get("user_requests", []):
            if ur and ur not in seen:
                seen.add(ur)
                results.append(ur)
                if len(results) >= 3:
                    return results

    # 降级：从 hdl 或 changes 中摘录
    if not results:
        for td in turns_data:
            hdl = td.get("hdl", "").strip()
            if hdl and hdl not in seen:
                seen.add(hdl)
                results.append(hdl)
                if len(results) >= 3:
                    break
            for c in td.get("changes", []):
                if isinstance(c, dict):
                    core = c.get("core_change", "").strip()
                else:
                    core = str(c).strip() if c else ""
                if core and core not in seen:
                    seen.add(core)
                    results.append(core)
                    if len(results) >= 3:
                        return results

    return results


def _extract_open_items(turns_data: list[dict]) -> list[str]:
    """规则：从各轮的 todos 字段收集 open_items，去重。"""
    seen: set[str] = set()
    results: list[str] = []
    for td in turns_data:
        for todo in td.get("todos", []):
            if todo.strip() and todo.strip() not in seen:
                seen.add(todo.strip())
                results.append(todo.strip())
    return results


def _compute_status(turns_data: list[dict]) -> str:
    """规则：如果有 stage_tag=评估中 的 change → 'active'，否则 'completed'。"""
    for td in turns_data:
        for stag in td.get("tags", {}).values():
            if stag == "评估中":
                return "active"
    return "completed"


# ── 主入口 ──


def summarize_topic_chunk(
    turns_data: list[dict],
    title: str = "",
    max_chars: Optional[int] = None,
    max_rounds: int = 3,
    candidate_themes: Optional[list] = None,
) -> Optional[Dict[str, Any]]:
    """对话题块各轮 Fct 生成结构化摘要（v8: 注入预算 + 迭代提炼）。

    Args:
        turns_data: collect_turn_fcts() 返回的数据列表
        max_chars: 注入预算（字符）。None → 不检查预算（旧行为）。
            超过预算时进入迭代提炼：输入超 INPUT_BUDGET 则分批，
            每轮将已有摘要 + 剩余批次融合（4B 边融合边压缩），
            轮次全部覆盖（remaining 空）即完成；仍超预算 → hdl 兜底。
        max_rounds: 迭代上限（保护，非必跑满）。
        candidate_themes (v6.5.3): 切换时注入的候选主题列表
            （[{theme_id, title, overview}, ...]），供 4B 生成 strand 时
            判断归属（strand.theme_ref）；None → 无参考，纯 strand 生成。

    Returns:
        {
            "hdl": str,
            "changes": list[str],       # 代码从 Fct 提取
            "user_requests": list[str], # 规则
            "key_facts": list[str],     # 代码从 Fct 提取
            "open_items": list[str],    # 规则
            "status": "active" | "completed",
        }
        失败返回 None（LLM 异常等情况）
    """
    if not turns_data:
        return None

    # 规则处理（不依赖 4B）
    hdl = _build_hdl(turns_data)
    user_requests = _extract_user_requests(turns_data)
    open_items = _extract_open_items(turns_data)
    status = _compute_status(turns_data)

    # 代码提取 changes/key_facts（精确去重，不依赖 4B，做安全兜底）
    fallback_changes = _fallback_changes(turns_data)
    fallback_key_facts = _fallback_key_facts(turns_data)

    # ── v8: 输入分批 + 迭代提炼 ──
    batches = _split_into_batches(turns_data, Config.TOPIC_SUMMARY_INPUT_BUDGET)
    remaining = list(batches)

    summary: Optional[Dict[str, Any]] = None
    for _round in range(max_rounds):
        if summary is None:
            prompt = build_summarize_prompt(
                remaining[0], max_chars=max_chars,
                candidate_themes=candidate_themes)
        else:
            if not remaining:
                break  # 轮次已全部覆盖 → 完成
            prompt = _build_refine_prompt(summary, remaining[0], max_chars=max_chars)
        llm_result = call_llm_for_summary(prompt)
        if llm_result is None:
            break  # 4B 失败 → 用已有结果或 fallback
        # P1-1 (v6.4.3): 4B 返回退化输出（无 strands 且无 ooda_groups）→
        # 重试一次（温度抖动防御，strand 35 场景 4B 偶发返回空结构）
        if not llm_result.get("strands") and not llm_result.get("ooda_groups"):
            logger.info("[CA_TOPIC_SUM] Degenerate LLM output "
                        "(no strands/ooda_groups), retrying once")
            retry = call_llm_for_summary(prompt)
            if retry is not None:
                llm_result = retry
        summary = _assemble_summary(
            llm_result, turns_data, hdl, user_requests, open_items, status,
            fallback_changes, fallback_key_facts,
        )
        remaining.pop(0)
        if not remaining:
            break  # 全部覆盖 → 完成

    if summary is None:
        # 4B 全失败：用代码精确去重的数据，不 skip
        summary = _assemble_summary(
            None, turns_data, hdl, user_requests, open_items, status,
            fallback_changes, fallback_key_facts,
        )

    # ── v8: 终态预算检查 → hdl 兜底 ──
    if max_chars is not None and _estimate_inject_chars(summary) > max_chars:
        summary = _apply_hdl_fallback(summary, hdl)

    return summary


def _assemble_summary(
    llm_result: Optional[Dict[str, Any]],
    turns_data: list[dict],
    hdl: str,
    user_requests: list,
    open_items: list,
    status: str,
    fallback_changes: list,
    fallback_key_facts: list,
) -> Dict[str, Any]:
    """将 4B 结果（或 None=失败）组装为最终 summary dict。

    v6.4: summary 增加 strands 键。strand 来源优先级：
      1. 4B 输出的 strands（多 strand，宁多勿少）
      2. 4B 输出 ooda_groups 但无 strands → 单 strand 包装（hdl=块 hdl）
      3. 4B 失败（None）→ _fallback_single_strand（hdl=块 hdl, turns=全部轮次）
    changes = 各 strand ooda 四组扁平 concat（0.95 Jaccard 去重）；
    open_items = 各 strand "后续行动" concat（为空则保留规则提取）。
    """
    if llm_result is not None:
        # 4B 成功：用融合后的数据
        # P0-1 (v6.4.3): 4B 返回 changes=[]（空列表）时回退代码提取——
        # .get(key, default) 在 key 存在但值为 [] 时返回 []，导致
        # _fallback_ooda_groups 用空输入 → strand ooda 全空（strand 35 根因）。
        changes = llm_result.get("changes") or fallback_changes
        key_facts = llm_result.get("key_facts") or fallback_key_facts
        title_4b = llm_result.get("title", "")
        consumable = llm_result.get("consumable", bool(changes or key_facts))
        strands = llm_result.get("strands")
        strands_from_llm = isinstance(strands, list) and bool(strands)
        if not strands_from_llm:
            # 无 strands → 单 strand 包装：OODA 优先 4B ooda_groups，回退代码回溯
            ooda_groups = llm_result.get("ooda_groups")
            if not isinstance(ooda_groups, dict) or not ooda_groups:
                ooda_groups = _fallback_ooda_groups(changes, turns_data)
            strands = _fallback_single_strand(turns_data, hdl, ooda_groups)
    else:
        # 4B 失败：用代码精确去重的数据，不 skip
        changes = fallback_changes
        key_facts = fallback_key_facts
        title_4b = ""
        consumable = bool(changes or key_facts)
        strands = _fallback_single_strand(turns_data, hdl)
        strands_from_llm = False
        logger.info("[CA_TOPIC_SUM] 4B failed, using code-extracted fallback")

    # v6.4: 仅当 4B 明确输出 strands 时，changes/open_items 由 strands 扁平化覆盖
    #（旧格式只输出 ooda_groups 时，changes 保持 4B 顶层融合结果，不被扁平化截断）
    if strands_from_llm:
        flat_changes = _flatten_strand_ooda(strands)
        if flat_changes:
            changes = flat_changes
        strand_open_items = _flatten_strand_open_items(strands)
        if strand_open_items:
            open_items = strand_open_items

    # title: 优先 4B，失败用 hdl
    final_title = title_4b.strip() if title_4b and title_4b.strip() else hdl

    return {
        "hdl": hdl,
        "title": final_title,
        "strands": strands,
        "changes": changes,
        "ooda_groups": _ooda_from_strands(strands),
        "user_requests": user_requests,
        "key_facts": key_facts,
        "open_items": open_items,
        "status": status,
        "consumable": consumable,
    }


# ── v6.4: strand 扁平聚合辅助 ──


def _fallback_single_strand(
    turns_data: list[dict],
    hdl: str,
    ooda_groups: Optional[dict] = None,
) -> list[dict]:
    """单 strand 兜底：hdl=块 hdl，turns=全部轮次，ooda=传入或代码回溯。

    P1-2 (v6.4.3): hdl 超长（>30 chars）→ 降级为首条 change 摘要，
    避免把完整 Fct 句子当短名落库（strand 35 场景）。
    """
    if len(hdl or "") > _HDL_MAX_LEN:
        short = _first_change_summary(turns_data)
        if short:
            logger.info("[CA_TOPIC_SUM] fallback hdl %d chars → short '%s'",
                        len(hdl), short)
            hdl = short
    all_turns = [td.get("turn", 0) for td in turns_data if td.get("turn")]
    if ooda_groups is None:
        ooda_groups = _fallback_ooda_groups(_fallback_changes(turns_data), turns_data)
    return [{"hdl": hdl, "turns": all_turns, "ooda": ooda_groups}]


def _jaccard_dedup(items: list, threshold: float = 0.95) -> list[str]:
    """字符级 Jaccard ≥ threshold 视为重复，保留首个（v6.4 扁平聚合去重）。"""
    seen: list[str] = []
    for it in items:
        if not isinstance(it, str) or not it.strip():
            continue
        dup = False
        for old in seen:
            union = len(set(it) | set(old))
            if union and len(set(it) & set(old)) / union >= threshold:
                dup = True
                break
        if not dup:
            seen.append(it)
    return seen


def _flatten_strand_ooda(strands: list[dict]) -> list[str]:
    """各 strand ooda 四组扁平 concat（0.95 Jaccard 去重）。

    2026-08-01: 4B 偶发输出 strands 数组含 str 元素（非 dict）→
    st.get() AttributeError 中断 reprocess。加类型防御跳过畸形元素。
    """
    flat: list[str] = []
    for st in strands:
        if not isinstance(st, dict):
            continue
        ooda = st.get("ooda", {})
        if not isinstance(ooda, dict):
            continue
        for group in ("现象与问题", "背景与约束", "决策与方案", "后续行动"):
            for item in ooda.get(group, []):
                if isinstance(item, str) and item.strip():
                    flat.append(item)
    return _jaccard_dedup(flat)


def _flatten_strand_open_items(strands: list[dict]) -> list[str]:
    """各 strand "后续行动" concat（0.95 Jaccard 去重）。"""
    flat: list[str] = []
    for st in strands:
        if not isinstance(st, dict):
            continue
        ooda = st.get("ooda", {})
        if not isinstance(ooda, dict):
            continue
        for item in ooda.get("后续行动", []):
            if isinstance(item, str) and item.strip():
                flat.append(item)
    return _jaccard_dedup(flat)


def _ooda_from_strands(strands: list[dict]) -> dict:
    """从 strands 重建 ooda_groups（四组 concat，兼容下游 ooda_groups 消费）。"""
    result: dict[str, list[str]] = {
        "现象与问题": [], "背景与约束": [], "决策与方案": [], "后续行动": [], "其他": [],
    }
    for st in strands:
        if not isinstance(st, dict):
            continue
        ooda = st.get("ooda", {})
        if not isinstance(ooda, dict):
            continue
        for group in ("现象与问题", "背景与约束", "决策与方案", "后续行动"):
            for item in ooda.get(group, []):
                if isinstance(item, str) and item.strip():
                    result[group].append(item)
        # strand 内超出四组的键（如"其他"）归入 其他
        for group, items in ooda.items():
            if group not in result:
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, str) and item.strip():
                        result["其他"].append(item)
    return {k: v for k, v in result.items() if v}


# ── v8: 注入预算 + 迭代提炼辅助 ──


def _estimate_inject_chars(summary: dict) -> int:
    """估算摘要按注入格式的总字符数（用原始内容，不模拟注入层截断）。

    v8 语义：预算检查的是 4B 输出的原始大小——注入层的 [:200] 硬截断
    会丢信息，预算机制正是要避免依赖截断（让 4B 在生成时控制体积）。
    """
    lines = []
    title = str(summary.get("title") or summary.get("hdl") or "Topic")
    lines.append(f"## {title}")
    for c in (summary.get("changes") or [])[:8]:
        lines.append(f"- {str(c)}")
    for i, ur in enumerate((summary.get("user_requests") or [])[:3], 1):
        lines.append(f"{i}. {str(ur)}")
    for kf in (summary.get("key_facts") or [])[:5]:
        lines.append(f"- {str(kf)}")
    for oi in (summary.get("open_items") or [])[:5]:
        lines.append(f"- {str(oi)}")
    return len("\n".join(lines)) + 2


def _split_into_batches(turns_data: list[dict], input_budget: Optional[int]) -> list[list[dict]]:
    """按输入字符预算切分轮次批次（防御性：输入超限时分批提炼）。

    input_budget 极大或 None → 单批（一次全喂，现有行为）。
    """
    if not input_budget or input_budget <= 0:
        return [turns_data]
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for td in turns_data:
        try:
            item_len = len(json.dumps(td, ensure_ascii=False))
        except (TypeError, ValueError):
            item_len = 0
        if cur and cur_len + item_len > input_budget:
            batches.append(cur)
            cur = []
            cur_len = 0
        cur.append(td)
        cur_len += item_len
    if cur:
        batches.append(cur)
    return batches or [turns_data]


REFINE_SUMMARY_PROMPT = """Given an existing topic summary and additional turn records from the same topic, produce a merged, refined summary.

Merge the new turn records into the existing summary:
- Semantically deduplicate: if a new change/fact is already covered by the existing summary, do not repeat it
- Incorporate new information that is not yet covered by the existing summary
- Merge related items, order logically (implementation first, analysis later)
- Keep the same JSON structure as the existing summary

Rules:
- ⚠️ hdl 命名规范（strands 中的 hdl 同样适用）：每个 strand 的 hdl 必须基于该 strand 的 ooda 内容总结为中文短名（如「连接池与超时配置优化」），禁止使用代码符号名/英文标识符/文件名/函数名（如 "embedding_service"、"topic_find_ca"）
- title: keep the existing title unless the new turns introduce a clearly more important theme
- changes: merged, deduplicated list of all changes (existing + new), in logical order
- key_facts: confirmed findings only
- consumable: false when all changes are already covered in previous topics or the topic is purely meta/confirmatory with no new actions
- output size: {budget}

## Existing summary (JSON)
{summary_json}

## Additional turn records
{new_turns}

Respond ONLY with the JSON object, no extra text."""


def _build_refine_prompt(
    summary: dict,
    new_turns: list[dict],
    max_chars: Optional[int] = None,
) -> str:
    """构建融合轮 prompt：已有摘要 + 新批次轮次 → 预算内合并提炼。

    v6.4: 已有摘要携带 strands（保持多 strand 结构跨批次延续）。
    """
    # 只序列化纯数据字段（strands 若存在则携带；其余沿用旧字段）
    keys = ("title", "changes", "key_facts")
    payload = {k: summary.get(k) for k in keys}
    if summary.get("strands"):
        payload["strands"] = summary["strands"]
    return REFINE_SUMMARY_PROMPT.format(
        budget=_budget_note(max_chars),
        summary_json=json.dumps(payload, ensure_ascii=False),
        new_turns=_format_turns_for_prompt(new_turns),
    )


# ── hdl 兜底（v8）──

_MEANINGLESS_HDL = {
    "", "无", "无新内容", "无新增", "效果如何", "如何", "好", "ok", "OK",
    "是", "嗯", "是的", "了解", "好的", "继续",
}


def _is_hdl_meaningful(hdl: str) -> bool:
    """hdl 质量过滤：过短（<8 字符）或纯确认/提问词 → 无信息量，弃用。"""
    h = (hdl or "").strip()
    if len(h) < 8:
        return False
    if h in _MEANINGLESS_HDL:
        return False
    return True


def _apply_hdl_fallback(summary: dict, hdl: str) -> dict:
    """注入超预算的最终兜底：title 降级为 hdl 单行（质量过滤后）。

    v6.4 (Bug 2 修复)：只降级 title，不清空 changes/key_facts/ooda——
    旧实现把内容清空导致调用方 hollow 判定 `(not changes and not key_facts)`
    把有实质内容的话题误 skip。超预算由注入层截断处理，不在此清空内容。

    质量不过关 → 保留原摘要（宁可超预算也不注入无意义内容，
    空洞保护在调用方 __init__._run_topic_summarize 仍会拦截）。
    """
    if not _is_hdl_meaningful(hdl):
        logger.info("[CA_TOPIC_SUM] hdl fallback rejected by quality filter, keep original")
        return summary
    logger.info("[CA_TOPIC_SUM] inject over budget, title fallback: %s", hdl[:60])
    return {**summary, "title": hdl}


# ── 4B fallback ──


def _fallback_changes(turns_data: list[dict]) -> list[str]:
    """4B 失败时：简单去重提取 changes。"""
    seen: set[str] = set()
    results: list[str] = []
    for td in turns_data:
        for c in td.get("changes", []):
            if isinstance(c, dict):
                core = c.get("core_change", "").strip() or c.get("change", "").strip()
            else:
                core = str(c).strip() if c else ""
            if core and core not in seen:
                seen.add(core)
                results.append(core)
    return results


def _fallback_key_facts(turns_data: list[dict]) -> list[str]:
    """4B 失败时：从 hdl 摘录作为 key_facts 的降级。"""
    seen: set[str] = set()
    results: list[str] = []
    for td in turns_data:
        hdl = td.get("hdl", "").strip()
        if hdl and hdl not in seen:
            seen.add(hdl)
            results.append(f"话题：{hdl}")
    return results


def _fallback_ooda_groups(
    changes: list[str],
    turns_data: list[dict],
) -> dict[str, list[str]]:
    """4B 未返回 ooda_groups 时：回溯匹配原始 Fct 的 ooda 标签。

    对每条 4B 输出的 change，裁剪已知前缀后，在原始 Fct changes 中
    找最大共同子串匹配的条目，取 ooda 归类。
    无匹配或 ooda 空时归入"其他"。
    """
    # 4B 可能加的前缀
    _KNOWN_PREFIXES = ("已实施: ", "计划: ", "探讨: ", "评估中: ", "已取消: ",
                       "新增: ", "背景: ", "决策: ", "后续: ", "现象: ")

    # 收集所有原始 Fct change 及其 ooda 标签
    ooda_map: dict[str, str] = {}
    for td in turns_data:
        for core in td.get("changes", []):
            if not isinstance(core, str):
                continue
            ooda = td.get("ooda_tags", {}).get(core, "")
            if ooda:
                ooda_map[core] = ooda

    OODA_LABELS = ["现象与问题", "背景与约束", "决策与方案", "后续行动"]
    result: dict[str, list[str]] = {l: [] for l in OODA_LABELS}
    result["其他"] = []

    def _strip_prefix(s: str) -> str:
        for p in _KNOWN_PREFIXES:
            if s.startswith(p):
                return s[len(p):]
        return s

    for change in changes:
        if not isinstance(change, str) or not change.strip():
            continue
        cleaned = _strip_prefix(change)
        best_ooda = ""
        best_overlap = 0
        for core, ooda in ooda_map.items():
            # 最长公共子串近似：简单双向包含
            if cleaned in core or core in cleaned:
                overlap = len(cleaned) + len(core)
            else:
                # 逐字符交集
                overlap = len(set(cleaned) & set(core))
            if overlap > best_overlap:
                best_overlap = overlap
                best_ooda = ooda

        # 阈值：至少 30% 字符重叠才算匹配
        min_chars = max(len(cleaned), 5) * 0.3
        if best_overlap < min_chars:
            best_ooda = ""

        if best_ooda in result:
            result[best_ooda].append(change)
        else:
            result["其他"].append(change)

    result = {k: v for k, v in result.items() if v}
    return result


# ── 注入格式化 ──


def format_topic_carryover(summaries: list[dict], session_id: str) -> str:
    """将召回的话题摘要格式化为 <topic_carryover> 注入块。"""
    if not summaries:
        return ""

    blocks = []
    for s in summaries:
        lines = []
        # title优先，fallback到hdl
        title = s.get("title") or s.get("hdl", "") or "Topic"
        lines.append(f"## {title[:120]}")
        lines.append("")

        changes = s.get("changes", [])
        if changes:
            lines.append("--- 变更 ---")
            for c in changes[:8]:
                lines.append(f"- {c[:200]}")

        user_requests = s.get("user_requests", [])
        if user_requests:
            lines.append("--- 用户需求 ---")
            for i, ur in enumerate(user_requests[:3], 1):
                lines.append(f"{i}. {ur[:200]}")

        key_facts = s.get("key_facts", [])
        if key_facts:
            lines.append("--- 分析结论 ---")
            for kf in key_facts[:5]:
                lines.append(f"- {kf[:200]}")

        open_items = s.get("open_items", [])
        if open_items:
            lines.append("--- 待办 ---")
            for oi in open_items[:5]:
                lines.append(f"- {oi[:200]}")

        blocks.append("\n".join(lines))

    return (
        f"<topic_carryover from='{session_id}'>\n"
        + "\n\n".join(blocks)
        + "\n</topic_carryover>"
    )


# ═══════════════════════════════════════════════════════════
# v5.11 — wiki merge 4B
# ═══════════════════════════════════════════════════════════

WIKI_MERGE_PROMPT = """..."""  # kept for backward compat, unused in Jaccard pipeline

# ── v5.12: 判断 + 合并 一次 4B 调用 ──

WIKI_JUDGE_MERGE_PROMPT = """You are a precise wiki entry curator. Given an existing wiki entry and a new topic summary, first decide whether they are about the same topic, then merge if applicable.

## Input

### Existing wiki entry
Title: {existing_title}
Overview: {existing_overview}
Key changes: {existing_changes}
Key facts: {existing_facts}

### New topic summary
Title: {new_title}
Key changes: {new_changes}
Key facts: {new_facts}

## Instructions

### Step 1 — Judge: is this the same topic?

Answer YES if the new topic discusses the same subject matter, same piece of work, same decision, or same problem as the existing entry — even if from a different angle or session.

Answer NO if they are tangentially related at best (e.g. both about "server config" but one is about connection pool and the other about timeout tuning — they are different topics).

### Step 2 — If YES, merge

- **overview**: Update to reflect the combined scope (2-4 sentences)
- **changes**: Merge + deduplicate, reorder by logical flow, max 10
- **key_facts**: Merge + deduplicate, keep the more specific version, max 10
- **open_items**: Merge + deduplicate, remove resolved items, max 6

Set **has_new_info=true** if the new topic adds any substantive changes, facts, or open items that are not already covered in the existing entry.
Set **has_new_info=false** if all content in the new topic is already fully covered in the existing entry.

## Output format

Respond ONLY with parseable JSON:

```json
{{{{
  "same_topic": true,
  "has_new_info": true,
  "overview": "...",
  "changes": [...],
  "key_facts": [...],
  "open_items": [...]
}}}}
```

If same_topic=false, set has_new_info=false and leave content fields empty.
The title field is always kept from the existing entry — do not output it."""


def merge_wiki_summaries(
    existing: dict,
    new_item: dict,
) -> Dict[str, Any]:
    """用 4B 将现有 wiki entry 与新话题摘要合并为一个精炼条目。

    Args:
        existing: 现有 wiki entry 的 dict（title, changes, key_facts, open_items）
        new_item: 新话题的 dict（title, changes, key_facts, open_items）

    Returns:
        合并后的 dict（title=原 title, overview, changes, key_facts, open_items）
        失败返回 None（4B 异常或 fallback）。
    """
    prompt = WIKI_MERGE_PROMPT.format(
        existing_title=json.dumps(existing.get("title", ""), ensure_ascii=False),
        existing_changes=json.dumps(existing.get("changes", []), ensure_ascii=False),
        existing_facts=json.dumps(existing.get("key_facts", []), ensure_ascii=False),
        existing_open=json.dumps(existing.get("open_items", []), ensure_ascii=False),
        new_title=json.dumps(new_item.get("title", ""), ensure_ascii=False),
        new_changes=json.dumps(new_item.get("changes", []), ensure_ascii=False),
        new_facts=json.dumps(new_item.get("key_facts", []), ensure_ascii=False),
        new_open=json.dumps(new_item.get("open_items", []), ensure_ascii=False),
    )

    result = call_llm_for_summary(prompt)
    if result is None:
        # 4B 失败 → 简单拼接去重 fallback
        logger.warning("[CA_WIKI] merge_wiki_summaries 4B failed, using rule fallback")
        result = _wiki_merge_fallback(existing, new_item)

    # 保留原 title（4B 不输出 title），空则 fallback 到新标题
    result["title"] = existing.get("title", "") or new_item.get("title", "")
    result.setdefault("overview", result.get("title", ""))
    result.setdefault("open_items", [])
    return result


def _wiki_merge_fallback(existing: dict, new_item: dict) -> dict:
    """4B 失败时的规则降级：简单去重合并。"""
    def _dedup(a: list, b: list) -> list:
        seen = set(a)
        return a + [x for x in b if x not in seen]

    return {
        "title": existing.get("title", "") or new_item.get("title", ""),
        "overview": existing.get("overview", "") or new_item.get("title", ""),
        "changes": _dedup(existing.get("changes", []), new_item.get("changes", [])),
        "key_facts": _dedup(existing.get("key_facts", []), new_item.get("key_facts", [])),
        "open_items": _dedup(existing.get("open_items", []), new_item.get("open_items", [])),
    }


def judge_and_merge_wiki(
    existing: dict,
    new_item: dict,
) -> Dict[str, Any]:
    """一次 4B 调用：先判断是否同话题，再决定归并或跳过。

    Args:
        existing: 现有 wiki entry 的 dict（title, overview, changes, key_facts）
        new_item: 新话题的 dict（title, changes, key_facts）

    Returns:
        {
            "same_topic": bool,
            "has_new_info": bool,
            "overview": str,
            "changes": list,
            "key_facts": list,
            "open_items": list,
        }
    """
    prompt = WIKI_JUDGE_MERGE_PROMPT.format(
        existing_title=json.dumps(existing.get("title", ""), ensure_ascii=False),
        existing_overview=json.dumps(existing.get("overview", ""), ensure_ascii=False),
        existing_changes=json.dumps(existing.get("changes", []), ensure_ascii=False),
        existing_facts=json.dumps(existing.get("key_facts", []), ensure_ascii=False),
        new_title=json.dumps(new_item.get("title", ""), ensure_ascii=False),
        new_changes=json.dumps(new_item.get("changes", []), ensure_ascii=False),
        new_facts=json.dumps(new_item.get("key_facts", []), ensure_ascii=False),
    )

    result = call_llm_for_summary(prompt)
    if result is None:
        logger.warning("[CA_WIKI] judge_and_merge_wiki 4B failed, rule fallback: skip merge (same_topic=false)")
        return {
            "same_topic": False,
            "has_new_info": False,
            "overview": "",
            "changes": [],
            "key_facts": [],
            "open_items": [],
        }

    # 4B 成功，解析字段
    same_topic = result.get("same_topic")
    if not isinstance(same_topic, bool):
        same_topic = True  # 缺省保守
    has_new_info = result.get("has_new_info")
    if not isinstance(has_new_info, bool):
        has_new_info = True

    return {
        "same_topic": same_topic,
        "has_new_info": has_new_info,
        "overview": result.get("overview", existing.get("overview", "")),
        "changes": result.get("changes") if result.get("changes") else existing.get("changes", []),
        "key_facts": result.get("key_facts") if result.get("key_facts") else existing.get("key_facts", []),
        "open_items": result.get("open_items") if result.get("open_items") else existing.get("open_items", []),
    }
