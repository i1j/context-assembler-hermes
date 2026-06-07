"""
ca/post_process.py — JSON 容错解析与清洗 (v4.4.0 alpha)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_CORE_CHANGE_RE = re.compile(r"核心摘要[：:]\s*(.{1,200})", re.DOTALL)


def robust_json_parse(raw: str, max_repair_attempts: int = 3) -> Tuple[Dict[str, Any], str]:
    if not raw or not raw.strip():
        return {"core_change": "本轮无新内容"}, "empty"
    try:
        return json.loads(raw), "direct"
    except json.JSONDecodeError:
        pass
    text = raw.strip()
    for _ in range(max_repair_attempts):
        if not text.endswith("}"):
            text += "}"
        try:
            return json.loads(text), "bracket_repair"
        except json.JSONDecodeError:
            pass
    match = _CORE_CHANGE_RE.search(raw)
    if match:
        return {"core_change": match.group(1).strip()}, "regex_fallback"
    return {"core_change": "本轮无新内容"}, "regex_fallback"


def clean_increment(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("_truncated"):
        return {"core_change": "本轮无新内容"}
    cleaned: Dict[str, Any] = {}
    core = data.get("core_change", "").strip()
    if core and core not in ("无", "本轮无新内容"):
        cleaned["core_change"] = core
    for field in ["new_materials", "objective_facts", "consensus", "todo"]:
        items = data.get(field, [])
        if not isinstance(items, list):
            items = []
        items = [i for i in items if i and i.strip()][:3]
        if items:
            cleaned[field] = items
    if not cleaned:
        cleaned["core_change"] = "本轮无新内容"
    return cleaned

# ── L1 v2 解析器常量 ──

MEANINGLESS_CORE: Set[str] = {"无", "暂无", "无有效增量", "无新增", "none", "null", "",
                            "无变化", "无明显变化", "无核心变化", "无核心变更"}
WHITESPACE_PATTERN = re.compile(r'\s+')
CORE_CHANGE_PATTERN = re.compile(r'<core_change>(.*?)(?:</core_change>|\Z)', re.DOTALL | re.IGNORECASE)

# 4 类 Markdown 标题 → 5 类英 key 映射
SECTION_MAP = {
    "现象与问题": "new_materials",
    "背景与约束": "objective_facts",
    "决策与共识": "consensus",
    "后续行动": "actions",  # post_process 输出 actions，OODAParser 映射为 todo
}

# OODA 4 类预编译正则（供 parse_v1_markdown_xml 使用）
OODA_PATTERNS = {
    "phenomena": re.compile(
        r'^###\s+现象与问题[：:]?\s*$',
        re.MULTILINE
    ),
    "background": re.compile(
        r'^###\s+背景与约束[：:]?\s*$',
        re.MULTILINE
    ),
    "decisions": re.compile(
        r'^###\s+决策与共识[：:]?\s*$',
        re.MULTILINE
    ),
    "actions": re.compile(
        r'^###\s+后续行动[：:]?\s*$',
        re.MULTILINE
    ),
}

# Markdown 列表项正则
_LIST_ITEM_RE = re.compile(r'^[-*•]\s+(.+)$', re.MULTILINE)


def _build_empty_result() -> Tuple[Dict, None]:
    """返回退化空结果。"""
    return {"core_change": "本轮无新内容"}, None


def _safe_truncate(text: str, max_len: int = 100) -> str:
    """智能截断：优先在句号/问号/感叹号处截断，降级到逗号/分号，最后硬截断。

    确保 config.py 等词汇不会被腰斩。
    """
    if not text or len(text) <= max_len:
        return text

    # 最高优先级：句号、问号、感叹号（中英文）
    for pos in range(max_len - 1, -1, -1):
        if pos < len(text) and text[pos] in "。！？.!?":
            result = text[:pos + 1].strip()
            if result:
                return result
            break

    # 次高优先级：逗号、分号、冒号（中英文）
    for pos in range(max_len - 1, -1, -1):
        if pos < len(text) and text[pos] in "，；;,:：":
            result = text[:pos + 1].strip()
            if result:
                return result
            break

    # 最低：硬截断
    result = text[:max_len].strip()
    # 确保不以连接符结尾
    while result and result[-1] in ",，;；:：.":
        result = result[:-1].strip()
        if not result:
            break
    return result


def _json_to_v1_markdown(data: dict) -> str:
    """将旧格式 5 类英 key JSON 转换为新 4 类 Markdown 格式字符串。"""
    if not data or not isinstance(data, dict):
        return str(data) if data else ""

    FIELD_MAP = {
        "new_materials": ("现象与问题", False),
        "objective_facts": ("背景与约束", False),
        "consensus": ("决策与共识", False),
        "todo": ("后续行动", True),
    }

    parts = []
    for field, (title, is_todo) in FIELD_MAP.items():
        items = data.get(field, [])
        if isinstance(items, list) and items:
            parts.append(f"### {title}")
            for item in items[:3]:
                parts.append(f"- {str(item)[:50]}")
            parts.append("")

    core_change = data.get("core_change", "").strip()
    if core_change:
        # 过滤无意义 core_change
        if core_change not in MEANINGLESS_CORE and core_change != "本轮无新内容":
            parts.append(f"<core_change>\n{core_change}\n</core_change>")
            parts.append("")

    result = "\n".join(parts).strip()
    if not result:
        # 只有 core_change 无意义时也返回
        if core_change:
            return f"<core_change>\n{core_change}\n</core_change>"
        return ""
    return result


def parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]:
    """解析 LLM 输出的 4 类 Markdown + XML 格式，返回 (l1_dict, l0_text)。

    l1_dict 包含 5 类英 key（core_change, new_materials, objective_facts, consensus, todo）。
    l0_text 是 core_change 的首句，最多 100 字。
    """
    if not llm_output or not llm_output.strip():
        logger.warning("[CA-METRIC] ca.l1.parse_fallback_count: llm_output is empty")
        return _build_empty_result()

    # 1. 物理截断尾随噪音
    text = _safe_truncate(llm_output, max_len=2000)

    # 2. 提取 <core_change>...</core_change>
    core_text: Optional[str] = None
    match = CORE_CHANGE_PATTERN.search(text)
    if match:
        core_text = match.group(1).strip()
        if core_text and (core_text in MEANINGLESS_CORE or core_text == "本轮无新内容"):
            core_text = None
    else:
        logger.warning("[CA-METRIC] ca.l1.parse_fallback_count: no <core_change> tag found")
        # 语义短路：无法提取核心变更，返回空结果
        return _build_empty_result()

    # 3. 提取 Markdown 4 类标题下的列表项
    section_order = [
        ("现象与问题", "new_materials"),
        ("背景与约束", "objective_facts"),
        ("决策与共识", "consensus"),
        ("后续行动", "todo"),
    ]

    l1_dict: Dict[str, Any] = {"core_change": core_text or "本轮无新内容"}
    for eng_key in ["new_materials", "objective_facts", "consensus", "todo"]:
        l1_dict[eng_key] = []

    # 按顺序查找各章节
    lines = text.split("\n")
    current_section: Optional[str] = None
    current_items: List[str] = []
    section_items: Dict[str, List[str]] = {}

    for line in lines:
        stripped = line.strip()
        # 检测 ### 标题
        heading_match = re.match(r'^###\s+(.+)$', stripped)
        if heading_match:
            # 保存上一节
            if current_section is not None and current_items:
                section_items[current_section] = current_items[:3]
            # 开始新节
            title = heading_match.group(1).strip().rstrip("：:")
            mapped = None
            for cn_title, eng_key in section_order:
                if cn_title == title:
                    mapped = eng_key
                    break
            current_section = mapped
            current_items = []
            continue

        # 收集列表项
        if current_section is not None:
            list_match = _LIST_ITEM_RE.match(stripped)
            if list_match:
                item = list_match.group(1).strip()
                if item and item not in ("无", "無", "none"):
                    if len(item) > 50:
                        item = item[:50]
                    if len(current_items) < 3:
                        current_items.append(item)
            elif stripped and not stripped.startswith("<") and not stripped.startswith("</"):
                # 非空行（非 XML 标签）也尝试收集
                if stripped not in ("无", "無", "none") and current_section:
                    pass  # 列表项已通过上面的正则收集

    # 保存最后一节
    if current_section is not None and current_items:
        section_items[current_section] = current_items[:3]

    # 填入 l1_dict
    for eng_key in l1_dict:
        if eng_key == "core_change":
            continue
        if eng_key in section_items:
            l1_dict[eng_key] = section_items[eng_key][:3]

    # 4. l0_text = core_text 首句[:100]
    l0_text: Optional[str] = None
    if core_text:
        # 取首句
        first_sentence = core_text
        for sep in ["。", "！", "？", ".", "!", "?"]:
            if sep in core_text:
                parts = core_text.split(sep, 1)
                first_sentence = parts[0] + sep
                break
        l0_text = _safe_truncate(first_sentence, max_len=100)
        if l0_text in MEANINGLESS_CORE:
            l0_text = None

    if l0_text is None:
        logger.warning("[CA-METRIC] ca.l0.skipped_empty: l0_text is None/empty")

    return (l1_dict, l0_text)
