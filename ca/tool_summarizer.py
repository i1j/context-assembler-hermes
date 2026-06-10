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

import ast
import json
import logging
import re
from collections import Counter
from pathlib import Path
from .post_process import _safe_truncate
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
                "result_summary": result_summary,
                "error": None,
                "implicit_knowledge": [],
                "next_action_hint": "",
                "_assemble_status": 0,
            }
            l0 = _safe_truncate(f"pytest ({', '.join(parts)}) — {el}s", 100)
            return l1, l0

        # exit_code 检测：优先靠 exit_code，不依赖文本正则
        exit_code = None
        for resp in tool_responses:
            try:
                raw = resp.get("content", "")
                if isinstance(raw, str) and raw.startswith("{"):
                    parsed = json.loads(raw)
                    ec = parsed.get("exit_code")
                    if ec is not None:
                        exit_code = ec
                        break
            except (json.JSONDecodeError, AttributeError):
                continue

        # 通用终端摘要 — L0 输出内容优先
        seen = set()
        key_lines = []
        for line in non_empty:
            stripped = line.strip()
            # 去重
            if stripped in seen:
                continue
            seen.add(stripped)
            # 过滤分隔符
            if stripped in ("---", "```", "==="):
                continue
            # 过滤 raw JSON dict 碎片 (Hermes terminal 返回的 JSON 外壳)
            if stripped.startswith('{"output"') or stripped.startswith('{"error"'):
                continue
            # 过滤内部注释
            if stripped.startswith("[Subdirectory context discovered"):
                continue
            key_lines.append(stripped)
            if len(key_lines) >= 5:
                break

        body = " | ".join(key_lines) if key_lines else ""
        # error 标记完全基于 exit_code，stdout 中的错误文本不算
        has_error = exit_code is not None and exit_code > 0
        # 结果优先：body 在前，cmd 降级到末尾
        # 无 key_lines 时展示 cmd（有输出才有信息价值）
        if key_lines:
            error_symbol = f"exit={exit_code} " if has_error else ""
            result_summary = f"{error_symbol}{body}"
            if cmd_short:
                result_summary += f" [{cmd_short}]"
        elif cmd_short:
            error_symbol = f"exit={exit_code} " if has_error else ""
            result_summary = f"{error_symbol}({total_effective} lines) [{cmd_short}]"
        else:
            result_summary = f"({total_effective} lines)"

        error_prefix = f"exit={exit_code}: " if has_error and exit_code else ""

        l1 = {
            "tool_name": "terminal",
            "tool_args": args,
            "result_summary": result_summary,
            "error": f"exit_code={exit_code}" if has_error else None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # L0 v4: 结果优先，省略命令文本，无 t: 前缀
        if key_lines:
            out_part = key_lines[0][:92]
            l0 = _safe_truncate(f"{error_prefix}{out_part}", 100)
        else:
            # 无有效输出时降级到 exit=N
            if exit_code is not None:
                l0 = f"exit={exit_code}"
            else:
                l0 = _safe_truncate(f"({total_effective} lines)", 100)
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

        # 检查结果 - 从 JSON 提取 bytes_written
        output = ""
        byte_count = None
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                try:
                    data = json.loads(output)
                    byte_count = data.get("bytes_written")
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        path_short = self._sanitize_path(path)
        if byte_count is not None:
            result_summary = f"已写入: {path_short} ({byte_count} 字节)"
        elif output:
            result_summary = f"已写入: {path_short}"
        else:
            result_summary = f"已写入: {path_short}"

        l1 = {
            "tool_name": "write_file",
            "tool_args": args,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"write_file: {path_short}", 100)
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

        path_short = self._sanitize_path(path)
        if output and "success" in output.lower():
            flag = " (replace_all)" if replace_all else ""
            result_summary = f"patch: {path_short}{flag}"
        else:
            result_summary = output or f"patch: {path_short}"

        l1 = {
            "tool_name": "patch",
            "tool_args": args,
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"patch: {path_short}", 100)
        return l1, l0

    def _summarize_read_file(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """read_file 结构化摘要：文件名 + 行数范围"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", "?")
        thought = tool_call_msg.get("content", "")

        path_short = self._sanitize_path(path)

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
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # L0: 文件名 + 行数，压缩路径前缀
        fname = Path(path).name if path else "<unknown>"
        parent = str(Path(path).parent)[-25:] if path else ""
        l0 = _safe_truncate(f"read_file: …{parent}/{fname} ({total_lines} lines)", 100)
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
                    all_files = [
                        f.get("path", str(f)) if isinstance(f, dict) else str(f)
                        for f in files
                    ]
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
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"search_files: {pattern[:40]} → {total_count} hits", 100)
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
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"skills_list: {total} skills", 100)
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
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"skill_view: {name_short}", 100)
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

        message_content = ""
        if isinstance(output, str) and output and output[0] == "{":
            try:
                data = json.loads(output)
                if data.get("success") is False:
                    error = data.get("error", "未知错误")
                elif "error" in data:
                    error = data["error"]
                else:
                    # success=True → 提取 message 字段
                    msg = data.get("message", "")
                    if msg:
                        message_content = msg
            except (json.JSONDecodeError, TypeError):
                pass

        if not error and "失败" in output:
            error = output[:120]

        key_str = ", ".join(f"{k}={v}" for k, v in key_fields.items())
        if error:
            result_summary = f"失败: {key_str} | err={error}"
        elif message_content:
            result_summary = f"成功: {key_str} — {message_content}"
        else:
            result_summary = f"OK: {key_str}"

        l1 = {
            "tool_name": "skill_manage",
            "tool_args": args,
            "result_summary": result_summary,
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        action = key_fields.get("action", "?")
        name = key_fields.get("name", "?")
        status = "error" if error else "ok"
        l0 = _safe_truncate(f"skill_manage: {action} {name} ({status})", 100)
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
            "result_summary": result_summary,
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        status = "error" if error else "ok"
        l0 = _safe_truncate(f"memory: {action} {target} ({status})", 100)
        if content_preview and len(l0) + len(content_preview) + 5 <= 100:
            l0 += f" — {content_preview}"
        return l1, l0

    def _summarize_todo(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """todo 结构化摘要：提取 summary 计数，避免 raw dict 泄漏。"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")

        output = ""
        for resp in tool_responses:
            c = resp.get("content", "")
            if c:
                output = str(c)
                break

        total = 0
        pending = 0
        in_progress = 0
        completed = 0
        cancelled = 0
        items = []
        if output:
            try:
                data = json.loads(output)
                sm = data.get("summary", data)
                total = sm.get("total", 0)
                pending = sm.get("pending", 0)
                in_progress = sm.get("in_progress", 0)
                completed = sm.get("completed", 0)
                cancelled = sm.get("cancelled", 0)
                items = data.get("todos", [])
            except (json.JSONDecodeError, TypeError):
                pass

        # 构建可读摘要
        parts = []
        if total:
            if pending:
                parts.append(f"{pending} pending")
            if in_progress:
                parts.append(f"{in_progress} in_progress")
            if completed:
                parts.append(f"{completed} completed")
            if cancelled:
                parts.append(f"{cancelled} cancelled")
            summary_text = f"todo: {total} 项"
            if parts:
                summary_text += f"（{', '.join(parts)}）"
        else:
            summary_text = f"todo: {len(items)} 项"

        result_summary = summary_text
        error = None

        l1 = {
            "tool_name": "todo",
            "tool_args": args,
            "result_summary": result_summary,
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }
        l0 = _safe_truncate(summary_text, 100)
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
                    raw = p0[key]
                    # 确保值为字符串，过滤 raw JSON dict/list
                    if isinstance(raw, (dict, list)):
                        raw = str(raw)
                    result_summary = self._sanitize_summary_text(raw)
                    break
            if not result_summary and vip.get("status"):
                result_summary = self._sanitize_summary_text(vip["status"])

        l1 = {
            "tool_name": tool_name,
            "tool_args": arguments,
            "result_summary": result_summary or "无返回数据",
            "error": error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        core = l1["result_summary"] or l1.get("error") or "无返回数据"
        l0 = _safe_truncate(f"{tool_name}: {core}", 100)

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

    @staticmethod
    def _sanitize_path(path_str: str, max_len: int = 120) -> str:
        """替换 HOME 路径为 ~ 防止绝对路径泄露。"""
        if not path_str:
            return ""
        s = str(path_str).replace("/home/i1j", "~")
        return s[:max_len]

    @staticmethod
    def _sanitize_summary_text(text: str, max_len: int = 200) -> str:
        """清理 result_summary：确保为字符串、剥离 raw JSON 外壳、缩短路径。"""
        if not text:
            return ""
        s = str(text)
        # 如果值本身是 dict/list 的字符串化结果（raw JSON），尝试提取有意义内容
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    # 提取常见文本字段
                    for key in ("result", "output", "summary", "message"):
                        val = parsed.get(key)
                        if val and isinstance(val, str):
                            s = val
                            break
                    else:
                        # 没有任何文本字段 → 压缩为简短描述
                        s = str(dict(list(parsed.items())[:3]))
            except (json.JSONDecodeError, TypeError):
                # Python repr(dict) fallback：单引号、True/False/None
                try:
                    parsed = ast.literal_eval(s)
                    if isinstance(parsed, dict):
                        for key in ("result", "output", "summary", "message"):
                            val = parsed.get(key)
                            if val and isinstance(val, str):
                                s = val
                                break
                        else:
                            s = str(dict(list(parsed.items())[:3]))
                except (ValueError, SyntaxError, TypeError):
                    pass
        elif s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    s = f"[{len(parsed)} items]"
            except (json.JSONDecodeError, TypeError):
                # Python repr(list) fallback：单引号、True/False/None
                try:
                    parsed = ast.literal_eval(s)
                    if isinstance(parsed, list):
                        s = f"[{len(parsed)} items]"
                except (ValueError, SyntaxError, TypeError):
                    pass
        s = s.replace("/home/i1j", "~")
        return s[:max_len]

    @staticmethod
    def generate_group_summary(thought: str,
                                tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """纯文本拼接工具组摘要，不调 LLM。

        Args:
            thought: assistant 的思考文本
            tool_results: 工具结果列表，每项含 tool_name / status / result_summary

        Returns:
            {"group_intent": str, "group_result": str,
             "tool_count": int, "state": "ok"|"error"|"blocked"|"cancelled"}
        """
        # 1. group_intent：截取 thought 首句（≤80 字符）；无 thought 时用工具名前缀
        intent = (thought or "").strip().split("\n")[0][:80]
        if not intent:
            unique_tools = list(dict.fromkeys(
                tr.get("tool_name", "?") for tr in (tool_results or [])
            ))
            tool_list = ", ".join(unique_tools[:3])
            if unique_tools:
                intent = f"调用 {tool_list}"
                if len(unique_tools) > 3:
                    intent += " 等工具"
            else:
                intent = "工具调用"

        # 2. group_result：汇总各工具 result_summary
        ok_count = 0
        error_count = 0
        tool_names = []
        for tr in (tool_results or []):
            name = tr.get("tool_name", "?")
            tool_names.append(name)
            st = tr.get("status", "ok")
            if st == "ok":
                ok_count += 1
            else:
                error_count += 1

        # 确定整体状态
        if error_count > 0 and ok_count == 0:
            state = "error"
        elif error_count > 0:
            state = "error"  # 有工具失败就标 error（保守策略）
        elif ok_count > 0:
            state = "ok"
        else:
            state = "ok"

        # 工具名去重后摘要
        unique_tools = list(dict.fromkeys(tool_names))
        tool_list = ", ".join(unique_tools[:5])
        if len(unique_tools) > 5:
            tool_list += f" 等 {len(unique_tools)} 种工具"

        result_parts = []
        for tr in (tool_results or []):
            rs = tr.get("result_summary", "")
            if rs:
                result_parts.append(rs)

        # 单工具组的 group_result 不重复工具细节——由工具行独占
        if len(tool_results) <= 1:
            group_result = f"调用 {len(tool_results)} 个工具"
        else:
            result_str = "；".join(result_parts[:3])
            if len(result_parts) > 3:
                result_str += "…"
            group_result = result_str if result_str else f"调用 {len(tool_results)} 个工具"

        return {
            "group_intent": intent,
            "group_result": group_result,
            "tool_count": len(tool_results),
            "state": state,
            "thought": (thought or "")[:200],
        }
