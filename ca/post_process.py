"""
ca/post_process.py — JSON 容错解析与清洗 (v4.4.0 alpha)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_CORE_CHANGE_RE = re.compile(r"核心摘要[：:]\s*(.{1,200})", re.DOTALL)



def clean_increment(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("_truncated"):
        return {"changes": [], "core_change": "本轮无新内容"}
    cleaned: Dict[str, Any] = {}
    # changes 列表：每条含 stage_tag（单状态）+ core_change（纯文本）
    changes = data.get("changes", [])
    valid_changes = []
    for c in changes:
        stag = c.get("stage_tag", "").strip()
        core = c.get("core_change", "").strip()
        if stag in VALID_STATES and core and core not in MEANINGLESS_CORE:
            valid_changes.append({"stage_tag": stag, "core_change": core})
    if valid_changes:
        cleaned["changes"] = valid_changes
        cleaned["core_change"] = "；".join(c["core_change"] for c in valid_changes)
    # 旧格式回退：单个 core_change + stage_tag 转成 changes
    elif data.get("core_change", "").strip():
        core = data["core_change"].strip()
        if core not in MEANINGLESS_CORE and core != "本轮无新内容":
            stag = data.get("stage_tag", "").strip()
            if stag in VALID_STATES:
                cleaned["changes"] = [{"stage_tag": stag, "core_change": core}]
            else:
                cleaned["changes"] = [{"stage_tag": "已实施", "core_change": core}]
            cleaned["core_change"] = core
    _PLACEHOLDERS = {"", "无", "無", "none", "-", "- 无", "—", "— 无", "暂无", "无有效内容",
                       "本轮无新增", "本轮无", "- 本轮无新增", "- 本轮无"}
    for field in ["new_materials", "objective_facts", "consensus", "todo"]:
        items = data.get(field, [])
        if not isinstance(items, list):
            items = []
        items = [i for i in items
                 if i and i.strip() and i.strip() not in _PLACEHOLDERS][:3]
        if items:
            cleaned[field] = items
    # 🔧 当 changes 已有有效条目时，去除旧 OODA 4 字段（避免新旧格式并存）
    _has_real_changes = cleaned.get("changes") and any(
        c.get("core_change", "").strip() not in _PLACEHOLDERS
        and c.get("core_change", "").strip() not in MEANINGLESS_CORE
        for c in cleaned["changes"]
    )
    if _has_real_changes:
        for field in ["new_materials", "objective_facts", "consensus", "todo"]:
            cleaned.pop(field, None)
    if not cleaned:
        cleaned["changes"] = []
        cleaned["core_change"] = "本轮无新内容"
    if "changes" not in cleaned:
        cleaned["changes"] = []
    if "core_change" not in cleaned:
        cleaned["core_change"] = "本轮无新内容"
    # 首轮/空历史兜底：有叙事段内容但无 changes → 自动派生（当 changes 为空且 core_change 为默认值时）
    if not cleaned.get("changes") and cleaned.get("core_change") == "本轮无新内容":
        _first_priority = cleaned.get("consensus", []) or cleaned.get("new_materials", [])
        if _first_priority and any(i not in _PLACEHOLDERS for i in _first_priority):
            _draft_core = next(i for i in _first_priority if i not in _PLACEHOLDERS)
            cleaned["changes"] = [{"stage_tag": "已实施", "core_change": _draft_core}]
            cleaned["core_change"] = _draft_core
    return cleaned

# ── L1 v2 解析器常量 ──

MEANINGLESS_CORE: Set[str] = {"无", "暂无", "无有效增量", "无新增", "none", "null", "",
                            "无变化", "无明显变化", "无核心变化", "无核心变更"}
# 匹配零对或多对 <stage_tag>【状态】</stage_tag><core_change>内容</core_change>
# 每对独立捕获，不依赖间距/换行
PAIR_PATTERN = re.compile(
    r'<stage_tag>\s*【([^】]+)】\s*</stage_tag>\s*<core_change>\s*(.*?)\s*</core_change>',
    re.DOTALL | re.IGNORECASE
)
VALID_STATES: Set[str] = {"已实施", "计划", "探讨", "已取消"}





# 4 类 Markdown 标题 → 5 类英 key 映射
SECTION_MAP = {
    "现象与问题": "new_materials",
    "背景与约束": "objective_facts",
    "决策与方案": "consensus",
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
        r'^###\s+决策与方案[：:]?\s*$',
        re.MULTILINE
    ),
    "actions": re.compile(
        r'^###\s+后续行动[：:]?\s*$',
        re.MULTILINE
    ),
}

# Markdown 列表项正则
_LIST_ITEM_RE = re.compile(r'^[-*•]\s+(.+)$', re.MULTILINE)


def _build_empty_result() -> Tuple[Dict, None, None]:
    """返回退化空结果。"""
    return {"changes": [], "core_change": "本轮无新内容"}, None, None


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
    """将 JSON 格式 Fct（含 changes 列表）转换为 Markdown + XML 格式字符串。"""
    if not data or not isinstance(data, dict):
        return str(data) if data else ""

    FIELD_MAP = {
        "new_materials": ("现象与问题", False),
        "objective_facts": ("背景与约束", False),
        "consensus": ("决策与方案", False),
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

    # 多对 <stage_tag>/<core_change>
    changes = data.get("changes", [])
    if changes:
        for c in changes:
            stag = c.get("stage_tag", "").strip()
            core = c.get("core_change", "").strip()
            if stag in VALID_STATES and core and core not in MEANINGLESS_CORE:
                parts.append(f"<stage_tag>\n【{stag}】\n</stage_tag>")
                parts.append(f"<core_change>\n{core}\n</core_change>")
                parts.append("")
    else:
        # 旧格式回退：单个 core_change
        core_change = data.get("core_change", "").strip()
        if core_change and core_change not in MEANINGLESS_CORE and core_change != "本轮无新内容":
            parts.append(f"<core_change>\n{core_change}\n</core_change>")
            parts.append("")

    result = "\n".join(parts).strip()
    if not result:
        core_change = data.get("core_change", "").strip()
        if core_change and core_change not in MEANINGLESS_CORE and core_change != "本轮无新内容":
            return f"<core_change>\n{core_change}\n</core_change>"
        return ""
    return result


def parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str], None]:
    """解析 LLM 输出的 4 类 Markdown + XML 格式，返回 (fct_dict, Hdl, None)。

    fct_dict 包含:
      - changes: list[dict] — 每项含 "stage_tag"(str) 和 "core_change"(str)
      - new_materials, objective_facts, consensus, todo: list[str]
      - core_change: str — 全部 core_change 的 "；" 拼接（向后兼容）

    Hdl 是首条 core_change 的首句，最多 100 字。
    core_state 始终为 None（已弃用，由 prompts 负责时态）。
    """
    if not llm_output or not llm_output.strip():
        logger.warning("[CA-METRIC] ca.fct.parse_fallback_count: llm_output is empty")
        return _build_empty_result()

    # 1. 物理截断尾随噪音
    text = _safe_truncate(llm_output, max_len=2000)

    # 2. 提取零对或多对 <stage_tag>【状态】</stage_tag><core_change>...</core_change>
    raw_pairs = PAIR_PATTERN.findall(text)
    changes: List[Dict[str, str]] = []
    for raw_state, raw_core in raw_pairs:
        state = raw_state.strip()
        core = raw_core.strip()
        if state in VALID_STATES and core and core not in MEANINGLESS_CORE:
            changes.append({"stage_tag": state, "core_change": core})

    if not changes:
        logger.warning("[CA-METRIC] ca.fct.parse_fallback_count: no valid <stage_tag>/<core_change> pairs found")

    # 3. 提取 Markdown 4 类标题下的列表项
    section_order = [
        ("现象与问题", "new_materials"),
        ("背景与约束", "objective_facts"),
        ("决策与方案", "consensus"),
        ("后续行动", "todo"),
    ]

    fct_dict: Dict[str, Any] = {
        "changes": changes,
        "core_change": "；".join(c["core_change"] for c in changes) if changes else "本轮无新内容",
    }
    for eng_key in ["new_materials", "objective_facts", "consensus", "todo"]:
        fct_dict[eng_key] = []

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

    # 填入 fct_dict
    for eng_key in fct_dict:
        if eng_key in ("changes", "core_change"):
            continue
        if eng_key in section_items:
            fct_dict[eng_key] = section_items[eng_key][:3]

    # 4. hdl_text = 首条 core_change 的首句（最多 100 字）
    hdl_text: Optional[str] = None
    if changes:
        first_core = changes[0]["core_change"]
        # 首句截断
        first_sentence = first_core
        for sep in ["。", "！", "？", ".", "!", "?", "\n"]:
            if sep in first_core:
                parts = first_core.split(sep, 1)
                first_sentence = (parts[0] + sep).rstrip('\n\r')
                break
        hdl_text = _safe_truncate(first_sentence, max_len=100)
        if hdl_text in MEANINGLESS_CORE:
            hdl_text = None

    if hdl_text is None:
        logger.warning("[CA-METRIC] ca.hdl.skipped_empty: hdl_text is None/empty")

    return (fct_dict, hdl_text, None)
