"""
ca/tool_summarizer.py — 工具轮摘要规则引擎 (v4.4.0 alpha)

功能：
- 按工具名匹配字段优先级配置。
- 支持 YAML 配置文件，失败回退 JSON 或硬编码默认。
- 单工具调用摘要，空工具名防御，未知工具日志提升。
- 按工具类型的结构化摘要分发（`_summarize_<tool_name>` 方法模式），
  当前支持 terminal/write_file/patch/read_file/search_files/skill_manage/memory/
  execute_code/skills_list/skill_view。
  关键参数优先展示，避免被 bulk content (old_string/new_string) 淹没。
  terminal 内置 pytest 输出检测 → 结构化测试结果摘要。
  未匹配的工具走通用字段提取逻辑不变。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
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

    def _summarize_terminal(self, tool_call_msg: Dict, tool_responses: List[Dict], tool_label: str = "t") -> Tuple[Dict, str]:
        """terminal 结构化摘要：保留命令摘要 + 关键行。检测 pytest 等结构输出"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        command = args.get("command", "")
        thought = tool_call_msg.get("content", "")

        # 拼接完整输出
        output_lines = []
        for resp in tool_responses:
            c = resp.get("content", "")
            if isinstance(c, str):
                output_lines.extend(c.split("\n"))
            elif isinstance(c, dict):
                output_lines.extend(str(c).split("\n"))

        non_empty = [l for l in output_lines if l.strip()]
        total_effective = len(non_empty)
        cmd_short = command[:100].replace("\n", "\\n") if command else ""

        # 检测 pytest 输出
        pytest_line = None
        for line in non_empty:
            stripped = line.strip()
            # "N passed, M failed, ... in X.YYs"
            if re.search(r'\d+\s+passed', stripped) and re.search(r'\d+\.\d+s', stripped):
                pytest_line = stripped
                break

        if pytest_line:
            # 提取测试结果摘要
            passed = re.search(r'(\d+)\s+passed', pytest_line)
            failed = re.search(r'(\d+)\s+failed', pytest_line)
            skipped = re.search(r'(\d+)\s+skipped', pytest_line)
            elapsed = re.search(r'in\s+([\d.]+)s', pytest_line)
            parts = []
            if passed: parts.append(f"{passed.group(1)} passed")
            if failed: parts.append(f"{failed.group(1)} failed")
            if skipped: parts.append(f"{skipped.group(1)} skipped")
            el = elapsed.group(1) if elapsed else "?"
            result_summary = f"pytest: {', '.join(parts)} — {el}s"
            l1 = {
                "tool_name": "terminal",
                "tool_args": args,
                "thought_process": thought,
                "result_summary": result_summary,
                "error": None,
                "implicit_knowledge": [],
                "next_action_hint": "",
                "_assemble_status": 0,
            }
            l0 = f"terminal: pytest ({', '.join(parts)}) — {el}s"[:100]
            return l1, l0

        # 检测终端错误输出
        _ERROR_RE = re.compile(
            r'(Traceback|Error:|Exception:|ModuleNotFound|ImportError|'
            r'NotFound|No such file|Permission denied|syntax error|'
            r'OperationalError|RuntimeError|ValueError|TypeError|'
            r'failed|FAILED)',
            re.IGNORECASE
        )
        has_error = any(_ERROR_RE.search(line) for line in non_empty)
        error_flag = "[ERROR] " if has_error else ""

        # 通用终端摘要 — L0 输出内容优先
        seen = set()
        key_lines = []
        for line in non_empty:
            stripped = line.strip()
            if stripped in seen:
                continue
            seen.add(stripped)
            if stripped in ("---", "```", "==="):
                continue
            key_lines.append(stripped)
            if len(key_lines) >= 5:
                break

        body = " | ".join(key_lines) if key_lines else f"({total_effective} lines)"
        result_summary = f"[{cmd_short}] {body}" if cmd_short else body
        if len(result_summary) > 500:
            result_summary = result_summary[:497] + "…"
        if error_flag:
            result_summary = f"[ERROR] {result_summary}"

        l1 = {
            "tool_name": "terminal",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": error_flag.strip() or None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # L0: 有输出内容则优先展示首个关键行，否则回退到行数
        if key_lines and tool_label in ("t", "exc"):
            cmd_part = cmd_short[:30].replace("\\n", " ")
            out_part = key_lines[0][:50]
            l0 = f"{error_flag}{tool_label}:{cmd_part} → {out_part}"[:100]
        else:
            l0 = f"{error_flag}{tool_label}: {cmd_short[:60]} ({total_effective} lines)"[:100]
        return l1, l0

    def _summarize_execute_code(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """execute_code 结构化摘要：代码首行摘要 + 输出关键行，同 terminal"""
        l1, l0 = self._summarize_terminal(tool_call_msg, tool_responses, tool_label="exc")
        l1["tool_name"] = "execute_code"
        return l1, l0

    def _summarize_write_file(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """write_file 结构化摘要：文件路径 + 动作结果，不含内容"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", args.get("file_path", ""))
        thought = tool_call_msg.get("content", "")

        # 检查结果
        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)[:200]
                break

        path_short = str(path)[:120] if path else "<unknown>"
        result_summary = output if output else f"write_file: {path_short}"

        l1 = {
            "tool_name": "write_file",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = f"write_file: {path_short}"[:100]
        return l1, l0

    def _summarize_patch(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """patch 结构化摘要：目标文件 + 替换数"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", "")
        replace_all = args.get("replace_all", False)
        thought = tool_call_msg.get("content", "")

        # 检查结果
        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)[:200]
                break

        path_short = str(path)[:120] if path else "<unknown>"
        if output and "success" in output.lower():
            flag = " (replace_all)" if replace_all else ""
            result_summary = f"patch: {path_short}{flag}"
        else:
            result_summary = output or f"patch: {path_short}"

        l1 = {
            "tool_name": "patch",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = f"patch: {path_short}"[:100]
        return l1, l0

    def _summarize_read_file(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """read_file 结构化摘要：文件名 + 行数范围"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", "?")
        thought = tool_call_msg.get("content", "")

        path_short = str(path)[:120] if path else "<unknown>"

        # 检查结果中是否包含行数信息
        total_lines = "?"
        for resp in tool_responses:
            c = resp.get("content", "")
            if isinstance(c, str) and c.startswith("Line "):
                # Hermes read_file 格式: "Line 1|...\nLine 2|..."
                lines = c.split("\n")
                total_lines = len(lines)
                break
            elif isinstance(c, str):
                # 优先从 JSON 响应中提取 total_lines 字段（read_file 标准格式）
                try:
                    parsed = json.loads(c)
                    if isinstance(parsed, dict) and "total_lines" in parsed:
                        total_lines = parsed["total_lines"]
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
                # fallback: 纯文本行数分割（JSON 解码失败时）
                lines = [l for l in c.split("\n") if l.strip()]
                if lines:
                    total_lines = len(lines)

        result_summary = f"read_file: {path_short} (lines {offset}-{limit}, loaded {total_lines})"

        l1 = {
            "tool_name": "read_file",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # L0: 文件名 + 行数，压缩路径前缀
        fname = Path(path).name if path else "<unknown>"
        parent = str(Path(path).parent)[-25:] if path else ""
        l0 = f"read_file: …{parent}/{fname} ({total_lines} lines)"[:100]
        return l1, l0

    def _summarize_search_files(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """search_files 结构化摘要：查询条件 + 结果数量 + 前几个文件名"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        thought = tool_call_msg.get("content", "")

        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        # 解析 JSON 结果
        matches = []
        all_files = []
        total_count = 0
        if output:
            try:
                data = json.loads(output)
                total_count = data.get("total_count", 0)
                files = data.get("files") or data.get("matches", [])
                if isinstance(files, list):
                    all_files = [str(f) for f in files]
                    matches = all_files[:3]
            except (json.JSONDecodeError, TypeError):
                pass

        result_summary = f"search_files: {total_count} matches"
        if all_files and total_count > 3:
            # 添加目录分组：提取顶层目录统计
            dirs = Counter()
            for fp in all_files:
                p = Path(fp)
                # 取第一个有父级的目录段
                parent = p.parent.name if p.parent else "."
                if not parent or parent == "/":
                    parent = "."
                dirs[parent] += 1
            groups = [f"{d}:{c}" for d, c in dirs.most_common(4)]
            if groups:
                result_summary += f" [{', '.join(groups)}]"
        elif matches:
            result_summary += f" ({', '.join(matches)})"
        if len(result_summary) > 500:
            result_summary = result_summary[:497] + "…"

        l1 = {
            "tool_name": "search_files",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = f"search_files: {pattern[:40]} → {total_count} hits"[:100]
        return l1, l0

    def _summarize_skills_list(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """skills_list 结构化摘要：技能名列表"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")

        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        skills = []
        if output:
            try:
                data = json.loads(output)
                items = data.get("skills", data.get("data", data.get("items", [])))
                if isinstance(items, list):
                    skills = [s.get("name", str(s))[:40] for s in items[:5]]
            except (json.JSONDecodeError, TypeError):
                pass

        total = len(skills)
        name_str = ", ".join(skills) if skills else "?"
        result_summary = f"skills_list: {total} items ({name_str})"

        l1 = {
            "tool_name": "skills_list",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = f"skills_list: {total} skills"[:100]
        return l1, l0

    def _summarize_skill_view(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """skill_view 结构化摘要：技能名 + 文件大小/结构概览"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        name = args.get("name", "?")
        file_path = args.get("file_path", "")
        thought = tool_call_msg.get("content", "")

        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        total_lines = "?"
        if output:
            total_lines = len(output.split("\n"))

        name_short = str(name)[:60]
        fp_short = f" ({file_path})" if file_path else ""

        result_summary = f"skill_view: {name_short}{fp_short} — {total_lines} lines"

        l1 = {
            "tool_name": "skill_view",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = f"skill_view: {name_short}"[:100]
        return l1, l0

    @staticmethod
    def _pick_key_fields(args: Dict, keys: List[str]) -> Dict[str, Any]:
        """从参数中提取关键字段，过滤掉 bulk content 字段"""
        result = {}
        for k in keys:
            v = args.get(k)
            if v is None:
                continue
            sv = str(v)
            if len(sv) > 60:
                result[k] = sv[:57] + "…"
            else:
                result[k] = sv
        return result

    def _summarize_skill_manage(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """skill_manage 结构化摘要：action + name + file_path，省略 old_string/new_string 等内容字段"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")

        key_fields = self._pick_key_fields(args, ["action", "name", "file_path", "category"])
        error = None

        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        if isinstance(output, str) and output and output[0] == "{":
            try:
                data = json.loads(output)
                if data.get("success") is False:
                    error = data.get("error", "未知错误")
                elif "error" in data:
                    error = data["error"]
            except (json.JSONDecodeError, TypeError):
                pass

        if not error and "失败" in output:
            error = output[:120]

        key_str = ", ".join(f"{k}={v}" for k, v in key_fields.items())
        if error:
            result_summary = f"失败: {key_str} | err={error}"
        else:
            result_summary = output[:300] if output else f"OK: {key_str}"

        l1 = {
            "tool_name": "skill_manage",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        action = key_fields.get("action", "?")
        name = key_fields.get("name", "?")
        status = "error" if error else "ok"
        l0 = f"skill_manage: {action} {name} ({status})"[:100]
        return l1, l0

    def _summarize_memory(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """memory 结构化摘要：action + 目标 + 结果"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")

        action = args.get("action", "?")
        target = args.get("target", "?")
        key_fields = self._pick_key_fields(args, ["action", "target", "old_text"])
        content_preview = str(args.get("content", ""))[:80]

        error = None
        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        if isinstance(output, str) and output and output[0] == "{":
            try:
                data = json.loads(output)
                if data.get("success") is False:
                    error = data.get("error", "未知错误")
            except (json.JSONDecodeError, TypeError):
                pass

        key_str = ", ".join(f"{k}={v}" for k, v in key_fields.items())
        if error:
            result_summary = f"失败: {key_str} | err={error}"
        elif content_preview:
            result_summary = f"{action} {target}: {content_preview}"
        else:
            result_summary = output[:300] or f"{action} {target} (no output)"

        l1 = {
            "tool_name": "memory",
            "tool_args": args,
            "thought_process": thought,
            "result_summary": result_summary,
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        status = "error" if error else "ok"
        l0 = f"memory: {action} {target} ({status})"[:100]
        if content_preview and len(l0) + len(content_preview) + 5 <= 100:
            l0 += f" — {content_preview}"
        return l1, l0

    def summarize(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        tool_name = (tool_call_msg.get("function", {}).get("name", "")).strip()
        if not tool_name:
            tool_name = "unknown_tool"

        # 适配层：arguments 是 JSON string（OpenAI API 标准），handler 需要 parsed dict
        func = tool_call_msg.get("function", {})
        raw_args = func.get("arguments", {})
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    tool_call_msg = {
                        **tool_call_msg,
                        "function": {**func, "arguments": parsed}
                    }
            except (json.JSONDecodeError, TypeError):
                pass

        # 工具类型分发：有专用 handler 的走结构化摘要，否则走通用逻辑
        sanitized = tool_name.replace(".", "_").replace("-", "_")
        handler = getattr(self, f"_summarize_{sanitized}", None)
        if handler is not None:
            try:
                return handler(tool_call_msg, tool_responses)
            except Exception as e:
                logger.warning("Tool-specific summarizer for '%s' failed: %s, falling back", tool_name, e)

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
