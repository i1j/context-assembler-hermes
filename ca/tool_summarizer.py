"""
ca/tool_summarizer.py — 工具轮摘要规则引擎 (v4.4.0 alpha)

功能：
- 按工具名匹配字段优先级配置。
- 支持 YAML 配置文件，失败回退 JSON 或硬编码默认。
- 单工具调用摘要，空工具名防御，未知工具日志提升。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    yaml = None
    _YAML_AVAILABLE = False

DEFAULT_PRIORITY: Dict[str, List[str]] = {
    "vip": ["tool_name", "error", "status", "command", "instruction"],
    "p0": ["content", "thought", "result", "summary", "message", "conclusion", "output"],
    "p1": ["args", "parameters", "input", "metadata", "context"],
    "p2": ["timestamp", "id", "trace_id", "request_id", "session_id"],
}

_PROFILE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


class ToolSummarizer:
    def __init__(self, profile: Optional[str] = None):
        self.priority = self._load_priority(profile)

    def _load_priority(self, profile: Optional[str]) -> Dict[str, Dict[str, List[str]]]:
        profile_name = profile or Config.TOOL_FIELD_PRIORITY_PROFILE
        if profile_name:
            if not _PROFILE_NAME_PATTERN.match(profile_name):
                logger.warning("Invalid profile name '%s', falling back to default", profile_name)
            else:
                profile_path = Path(__file__).parent / f"tool_field_priority_{profile_name}.yaml"
                if not profile_path.exists():
                    profile_path = Path(__file__).parent / f"tool_field_priority_{profile_name}.json"
                if profile_path.exists():
                    try:
                        config = self._load_config_file(profile_path)
                        validated = self._validate_all_tools(config)
                        logger.info("Loaded tool field priority profile: %s", profile_path)
                        return validated
                    except Exception as e:
                        logger.warning("Failed to load profile %s, using default: %s", profile_path, e)

        default_path = Path(__file__).parent / "tool_field_priority.yaml"
        if not default_path.exists():
            default_path = Path(__file__).parent / "tool_field_priority.json"
        if default_path.exists():
            try:
                config = self._load_config_file(default_path)
                validated = self._validate_all_tools(config)
                logger.debug("Loaded default tool field priority")
                return validated
            except Exception as e:
                logger.warning("Failed to load default priority, using hardcoded: %s", e)
        return {"default": DEFAULT_PRIORITY}

    def _load_config_file(self, path: Path) -> Dict:
        if path.suffix in ('.yaml', '.yml') and _YAML_AVAILABLE:
            with open(path, 'r') as f:
                return yaml.safe_load(f)
        else:
            with open(path, 'r') as f:
                return json.load(f)

    def _validate_all_tools(self, config: Dict) -> Dict[str, Dict[str, List[str]]]:
        validated = {}
        for tool_name, prio in config.items():
            if isinstance(prio, dict):
                validated[tool_name] = self._merge_with_default(prio)
                self._validate_priority_config(validated[tool_name])
            else:
                logger.warning("Invalid config for tool '%s', skipping", tool_name)
        if "default" not in validated:
            validated["default"] = DEFAULT_PRIORITY
        return validated

    def _merge_with_default(self, config: Dict) -> Dict[str, List[str]]:
        def ensure_list(value, default):
            if isinstance(value, list):
                return value
            logger.warning("Invalid config value %s, using default", value)
            return default.copy()
        return {
            "vip": ensure_list(config.get("vip"), DEFAULT_PRIORITY["vip"]),
            "p0": ensure_list(config.get("p0"), DEFAULT_PRIORITY["p0"]),
            "p1": ensure_list(config.get("p1"), DEFAULT_PRIORITY["p1"]),
            "p2": ensure_list(config.get("p2"), DEFAULT_PRIORITY["p2"]),
        }

    def _validate_priority_config(self, config: Dict):
        for level, fields in config.items():
            if not isinstance(fields, list):
                raise ValueError(f"Priority level {level} must be a list")
            if len(fields) > 20:
                logger.warning("Priority level %s has %d fields (too many)", level, len(fields))

    def _get_priority_for_tool(self, tool_name: str) -> Dict[str, List[str]]:
        normalized = tool_name.strip() if tool_name else "unknown_tool"
        if normalized in self.priority:
            return self.priority[normalized]
        if normalized != "unknown_tool":
            logger.info("No specific config for tool '%s', using default", normalized)
        return self.priority.get("default", DEFAULT_PRIORITY)

    def summarize(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        tool_name = (tool_call_msg.get("function", {}).get("name", "")).strip()
        if not tool_name:
            tool_name = "unknown_tool"

        arguments = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")
        priority = self._get_priority_for_tool(tool_name)

        combined_results = []
        for resp in tool_responses:
            content = resp.get("content")
            if content is None:
                combined_results.append({"result": ""})
                continue
            if isinstance(content, (dict, list)):
                combined_results.append({"result": content} if isinstance(content, list) else content)
            elif isinstance(content, str):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        combined_results.append(data)
                    else:
                        combined_results.append({"result": data})
                except (json.JSONDecodeError, TypeError):
                    combined_results.append({"result": content[:200]})
            else:
                combined_results.append({"result": str(content)[:200]})

        vip = self._extract_fields(combined_results, priority["vip"], full=True)
        p0 = self._extract_fields(combined_results, priority["p0"], full=True, truncate=True)
        p1 = self._extract_fields(combined_results, priority["p1"], full=False, name_only=True)

        result_summary = ""
        error = vip.get("error")
        if error:
            result_summary = "失败"
        else:
            for key in ["result", "summary", "message", "conclusion", "output"]:
                if key in p0 and p0[key]:
                    result_summary = p0[key]
                    break
            if not result_summary and vip.get("status"):
                result_summary = vip["status"]

        l1 = {
            "tool_name": tool_name,
            "tool_args": arguments,
            "thought_process": thought,
            "result_summary": result_summary or "无返回数据",
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        core = l1["result_summary"] or l1.get("error") or "无返回数据"
        l0 = f"{tool_name}: {core}"[:100]
        if thought and len(thought) <= 30 and len(l0) + len(thought) + 11 <= 100:
            l0 += f" (thinking: {thought})"

        return l1, l0

    def _extract_fields(self, data_list, field_names, full=True, truncate=False, name_only=False):
        result: Dict[str, Any] = {}
        for data in data_list:
            for field in field_names:
                if field in data:
                    value = data[field]
                    if name_only:
                        result[field] = f"<{field}>"
                    elif full:
                        if truncate and isinstance(value, str):
                            value = self._head_tail_truncate(value)
                        result[field] = value
        return result

    @staticmethod
    def _head_tail_truncate(text: str, head_ratio: float = 0.6, max_len: int = 120) -> str:
        if len(text) <= max_len:
            return text
        head_len = int(max_len * head_ratio)
        tail_len = max_len - head_len - 1
        if tail_len < 0:
            return text[:max_len] + "…"
        return text[:head_len] + "…" + text[-tail_len:]
