"""
ca/post_process.py — JSON 容错解析与清洗 (v4.4.0 alpha)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_CORE_CHANGE_RE = re.compile(r"核心摘要[：:]\s*(.{1,200})", re.DOTALL)


def robust_json_parse(raw: str, max_repair_attempts: int = 3) -> Tuple[Dict[str, Any], str]:
    if not raw or not raw.strip():
        return {"core_change": "无有效增量"}, "empty"
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
    return {"core_change": "无有效增量"}, "regex_fallback"


def clean_increment(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("_truncated"):
        return {"core_change": "无有效增量"}
    cleaned: Dict[str, Any] = {}
    core = data.get("core_change", "").strip()
    if core and core not in ("无", "无有效增量"):
        cleaned["core_change"] = core
    for field in ["new_materials", "objective_facts", "consensus", "todo"]:
        items = data.get(field, [])
        if not isinstance(items, list):
            items = []
        items = [i for i in items if i and i.strip()][:3]
        if items:
            cleaned[field] = items
    if not cleaned:
        cleaned["core_change"] = "无有效增量"
    return cleaned