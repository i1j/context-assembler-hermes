"""
ca/tool_summarizer.py — 工具轮摘要规则引擎 (v5.10)

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


def _is_delimiter_heavy(line: str, threshold: float = 0.5) -> bool:
    """纯分隔符行检测：>50% 非空格字符为 =-=* 的行视为无语义"""
    stripped = line.strip()
    if not stripped:
        return True
    non_space = [c for c in stripped if not c.isspace()]
    if not non_space:
        return True
    delim_count = sum(1 for c in non_space if c in "=-_*")
    return delim_count / len(non_space) > threshold


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
                "tool_args": {"command": cmd_short} if cmd_short else {},
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

        # 通用终端摘要 — Hdl 输出内容优先
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
            # P1: 有 key_lines 时不附带 cmd_short — 命令已隐含在 thought 中，单位 Token 互信息最大化为零
            result_summary = f"{error_symbol}{body}"
        elif cmd_short:
            error_symbol = f"exit={exit_code} " if has_error else ""
            result_summary = f"{error_symbol}({total_effective} lines) [{cmd_short}]"
        else:
            result_summary = f"({total_effective} lines)"

        error_prefix = f"exit={exit_code}: " if has_error and exit_code else ""

        l1 = {
            "tool_name": "terminal",
            "tool_args": {"command": cmd_short} if cmd_short else {},
            "result_summary": result_summary,
            "error": f"exit_code={exit_code}" if has_error else None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # Hdl v4+: 命令前缀 + 关键输出，便于话题回顾
        cmd_part = cmd_short[:40] if cmd_short else ""
        if key_lines:
            out_part = key_lines[0][:50]
            if cmd_part:
                l0 = _safe_truncate(f"{error_prefix}{cmd_part}: {out_part}", 100)
            else:
                l0 = _safe_truncate(f"{error_prefix}{out_part}", 92)
        else:
            if exit_code is not None:
                if cmd_part:
                    l0 = _safe_truncate(f"{cmd_part} (exit={exit_code})", 100)
                else:
                    l0 = f"exit={exit_code}"
            else:
                l0 = _safe_truncate(f"({total_effective} lines)", 100)
        return l1, l0

    def _summarize_execute_code(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """execute_code 结构化摘要：解析 Hermes JSON 响应，提取 status + 关键输出行。

        当前问题(6/24)：result_summary 原文存储 Hermes 原始 JSON 串（含 tool_calls_made/
        duration_seconds 噪声）。新逻辑剥离 JSON 外壳，只保留 status + 前 3 个有意义的
        输出行。数据验证：139 行中 98% 可解析为 JSON，97% 含关键输出行。
        """
        args = tool_call_msg.get("function", {}).get("arguments", {})
        thought = tool_call_msg.get("content", "")

        # ---- 解析 Hermes execute_code 响应 JSON ----
        status = "?"
        output_text = ""
        error_msg = None
        tool_calls_count = 0

        for resp in tool_responses:
            c = resp.get("content", "")
            if isinstance(c, str) and c.startswith("{"):
                try:
                    parsed = json.loads(c)
                    status = parsed.get("status", status)
                    output_text = parsed.get("output", "")
                    error_msg = parsed.get("error")
                    tool_calls_count = parsed.get("tool_calls_made", 0)
                    break
                except (json.JSONDecodeError, TypeError):
                    continue

        # ---- 提取有意义的输出行 ----
        non_empty = [l for l in output_text.split("\n") if l.strip()]
        seen = set()
        key_lines = []
        pytest_line = None

        # 优先检测 pytest 摘要行
        for line in non_empty:
            stripped = line.strip()
            if re.search(r'\d+\s+passed', stripped) and re.search(r'\d+\.\d+s', stripped):
                pytest_line = stripped
                break

        if pytest_line:
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
            has_error = status in ("error", "timeout", "interrupted")
            l1 = {
                "tool_name": "execute_code",
                "tool_args": self._clean_tool_args("execute_code", args),
                "result_summary": result_summary,
                "error": error_msg if has_error else None,
                "implicit_knowledge": [],
                "next_action_hint": "",
                "_assemble_status": 0,
            }
            l0 = _safe_truncate(f"pytest ({', '.join(parts)}) — {el}s", 100)
            return l1, l0

        for line in non_empty:
            stripped = line.strip()
            if stripped in seen:
                continue
            seen.add(stripped)
            # 过滤分隔符和 JSON 外壳碎片
            if stripped in ("---", "```", "==="):
                continue
            if stripped.startswith('{"') or stripped.endswith("}"):
                continue
            # 2 行以内 JSON 片 -> 剩余输出无信号
            if stripped.startswith("{\"status\"") or stripped.startswith("\"output\""):
                continue
            # 分隔符占 >50% 非空格字符的行（=== ==== 等）：无语义信息
            if _is_delimiter_heavy(stripped):
                continue
            key_lines.append(stripped)
            if len(key_lines) >= 3:
                break

        # ---- 构建 result_summary ----
        has_error = status in ("error", "timeout", "interrupted")
        tc_part = f" [{tool_calls_count} tc]" if tool_calls_count > 0 else ""

        if key_lines:
            body = " | ".join(key_lines)
        elif error_msg:
            # 有 error 无有效输出行：只保留错误信息
            err_short = error_msg.split("\n")[0].strip()[:120]
            body = f"({err_short})"
        else:
            body = f"({len(non_empty)} lines)"

        if has_error:
            if error_msg:
                err_type = error_msg.split("\n")[0].strip()[:80]
                result_summary = f"{status}: {err_type}"
            else:
                result_summary = f"{status}: {body}"
        else:
            result_summary = f"{body}{tc_part}"

        # ---- 构建 Fct dict ----
        l1 = {
            "tool_name": "execute_code",
            "tool_args": self._clean_tool_args("execute_code", args),
            "result_summary": result_summary,
            "error": error_msg if has_error else None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # ---- 构建 Hdl ----
        if has_error:
            if error_msg:
                err_short = error_msg.split("\n")[0].strip()[:50]
                l0 = _safe_truncate(f"exc@{status}: {err_short}", 100)
            elif key_lines:
                out_part = key_lines[0][:50]
                l0 = _safe_truncate(f"exc@{status}: {out_part}", 100)
            else:
                l0 = _safe_truncate(f"exc@{status}", 100)
        elif key_lines:
            out_part = key_lines[0][:50]
            l0 = _safe_truncate(f"exc: {out_part}", 92)
        else:
            l0 = _safe_truncate(f"exc: ({len(non_empty)} lines)", 100)

        return l1, l0

    def _summarize_write_file(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """write_file 结构化摘要：文件路径 + 动作结果，不含内容"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", args.get("file_path", ""))
        thought = tool_call_msg.get("content", "")

        # 检查结果 - 从 JSON 提取 bytes_written
        data = self._parse_json_response(tool_responses)
        byte_count = data.get("bytes_written") if data else None

        path_short = self._sanitize_path(path)
        if byte_count is not None:
            result_summary = f"已写入: {path_short} ({byte_count} 字节)"
        else:
            result_summary = f"已写入: {path_short}"

        # 注：写文件 always succ — 响应里如果不是 JSON 或有错误，知道文件名就够诊了
        # 另外两个分支(elif output / else) 内容完全一致，已合并

        l1 = {
            "tool_name": "write_file",
            "tool_args": self._clean_tool_args("write_file", args),
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

        # 检查结果 — 解析 JSON 提取 files_modified 数和 success 状态
        data = self._parse_json_response(tool_responses)
        success = data.get("success") if data else None
        files_modified = data.get("files_modified") if data else None

        path_short = self._sanitize_path(path)
        if success is False:
            result_summary = f"patch failed: {path_short}"
        elif files_modified:
            fm_count = len(files_modified)
            flag = " (replace_all)" if replace_all else ""
            result_summary = f"patch: {path_short} ({fm_count} files){flag}"
        else:
            flag = " (replace_all)" if replace_all else ""
            result_summary = f"patch: {path_short}{flag}"

        l1 = {
            "tool_name": "patch",
            "tool_args": self._clean_tool_args("patch", args),
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"patch: {path_short}", 100)
        return l1, l0

    def _summarize_read_file(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """read_file Fct=Elm 全量：取文件原文，去掉冗余（path/offset/limit 已在 tool_args）"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        path = args.get("path", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", "?")
        path_short = self._sanitize_path(path)

        # 从 tool 响应中提取 文件原文 + total_lines
        result_content = ""
        total_lines = "?"
        error = None
        for resp in tool_responses:
            c = resp.get("content", "")
            if not isinstance(c, str):
                continue
            try:
                parsed = json.loads(c)
                if not isinstance(parsed, dict):
                    continue
                # 逐级判断响应类型
                if parsed.get("error"):
                    error = parsed["error"]
                    continue  # error 不一定是最终状态，继续循环可能有正常结果
                if parsed.get("status") == "unchanged" or parsed.get("dedup"):
                    # dedup 命中：无新内容，保留 path 信息
                    result_content = parsed.get("message", "File unchanged (dedup)")
                    if "total_lines" in parsed:
                        total_lines = parsed["total_lines"]
                    break
                # 正常读取：提取文件原文
                if "content" in parsed:
                    result_content = parsed["content"]
                if "total_lines" in parsed:
                    total_lines = parsed["total_lines"]
                break
            except (json.JSONDecodeError, TypeError):
                continue

        # result_summary = Elm 头尾截断摘要（文件原文太长时保留开头+结尾 ~200 字）
        if error:
            result_summary = error
            l1_error = error
        else:
            raw = result_content if result_content else "(empty response)"
            # head_tail_truncate：保留文件开头(imports/签名) + 结尾(最后函数/类)
            # 比对 _sanitize_summary_text 更适合代码文件的信息密度
            s = str(raw).replace("/home/i1j", "~")
            result_summary = ToolSummarizer._head_tail_truncate(s, head_ratio=0.6, max_len=200)
            l1_error = None

        l1 = {
            "tool_name": "read_file",
            "tool_args": self._clean_tool_args("read_file", args),
            "result_summary": result_summary,
            "total_lines": total_lines,
            "error": l1_error,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        # Hdl：话题回顾用——路径 + 范围 + 行数
        if error:
            l0 = _safe_truncate(f"read_file: {path_short} — {error[:80]}", 100)
        elif total_lines != "?":
            l0 = _safe_truncate(f"read_file: {path_short} (lines {offset}-{limit}, {total_lines}行)", 100)
        else:
            l0 = _safe_truncate(f"read_file: {path_short} (lines {offset}-{limit})", 100)
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
                        self._sanitize_path(f.get("path", str(f))) if isinstance(f, dict) else self._sanitize_path(str(f))
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
            "tool_args": self._clean_tool_args("search_files", args),
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
            "tool_args": self._clean_tool_args("skills_list", args),
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }

        l0 = _safe_truncate(f"skills_list: {total} skills", 100)
        return l1, l0

    def _summarize_skill_view(self, tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]:
        """结构化 Markdown 提取：frontmatter + 高价值章节全量 + 其余章节索引"""
        args = tool_call_msg.get("function", {}).get("arguments", {})
        skill_name = args.get("name", "?")

        # Parse tool response
        body = description = ""
        tags = []
        linked_files = {}
        status = "available"
        for resp in tool_responses:
            c = resp.get("content", "")
            if not c:
                continue
            try:
                data = json.loads(str(c))
                if isinstance(data, dict):
                    body = data.get("body") or ""
                    description = (data.get("description") or "").strip()
                    tags = data.get("tags") or []
                    linked_files = data.get("linked_files") or {}
                    status = data.get("status", "available")
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        # Error / dedup
        if not body:
            desc_short = description[:60] if description else skill_name
            l0 = _safe_truncate(f"skill_view: {skill_name} — {desc_short}", 100)
            l1 = {
                "tool_name": "skill_view",
                "tool_args": self._clean_tool_args("skill_view", args),
                "result_summary": description or "[空]",
                "error": None,
                "implicit_knowledge": [],
                "next_action_hint": "",
                "_assemble_status": 0,
            }
            return l1, l0

        lines = body.split("\n")
        total_lines = len(lines)

        # ---- Parse frontmatter ----
        body_offset = 0
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    body_offset = i + 1
                    break
        body_lines = lines[body_offset:]

        # ---- Section parsing ----
        PITFALL_KW = ["pitfall", "坑", "warning", "注意", "caveat", "踩坑", "⚠", "警告", "错误", "常见问题"]
        HOWTO_KW = ["usage", "use", "step", "how to", "使用", "步骤", "操作", "用法", "方法", "流程"]
        ALL_HIGH_KW = PITFALL_KW + HOWTO_KW

        sections = []  # [(title, start_ln, line_count, code_count)]
        cur_title = "## (文件头部)"
        cur_start = body_offset
        cur_lines = []
        cur_cc = 0
        in_code = False

        for i, line in enumerate(body_lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                cur_lines.append(line)
                continue
            if in_code:
                cur_lines.append(line)
                continue
            if stripped.startswith("## ") or stripped.startswith("### "):
                sections.append((cur_title, cur_start, len(cur_lines), cur_cc))
                cur_title = stripped
                cur_start = body_offset + i
                cur_lines = [line]
                cur_cc = 0
            else:
                cur_lines.append(line)
        sections.append((cur_title, cur_start, len(cur_lines), cur_cc))

        # ---- Build output ----
        fm_tag_str = ", ".join(tags) if tags else ""

        parts = []
        parts.append(f"[{skill_name}]")
        if description:
            parts.append(f" 描述: {description}")
        if fm_tag_str:
            parts.append(f" 标签: {fm_tag_str}")
        parts.append(f" 状态: {status}")
        if linked_files:
            refs = linked_files.get("references") or []
            tmpl = linked_files.get("templates") or []
            scripts = linked_files.get("scripts") or []
            parts.append(f" 引用: {len(refs)}篇, 模板: {len(tmpl)}, 脚本: {len(scripts)}")
        parts.append(f" | 共{total_lines}行")
        parts.append("")

        high_parts = []
        low_index = []
        for title, start_ln, lc, cc in sections:
            if lc == 0:
                continue
            tl = title.lower()
            is_high = any(kw in tl for kw in ALL_HIGH_KW)
            full_text = "\n".join(lines[start_ln:start_ln + lc])

            if is_high:
                high_parts.append(title)
                high_parts.append(full_text)
                high_parts.append("")
            else:
                extra = f" [代码块×{cc}]" if cc else ""
                low_index.append(f"  {title} ({lc}行{extra})")

        if high_parts:
            parts.append("── 完整保留 ──")
            parts.extend(high_parts)

        if low_index:
            parts.append("── 省略章节（可回查Elm）──")
            parts.extend(low_index)

        result_summary = "\n".join(parts).strip()

        desc_short = _safe_truncate(description, 60) if description else skill_name
        l0 = _safe_truncate(f"skill_view: {skill_name} — {desc_short} ({total_lines}行)", 100)

        l1 = {
            "tool_name": "skill_view",
            "tool_args": self._clean_tool_args("skill_view", args),
            "result_summary": result_summary,
            "error": None,
            "implicit_knowledge": [],
            "next_action_hint": "",
            "_assemble_status": 0,
        }
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
        message_content = ""

        data = self._parse_json_response(tool_responses)
        if data:
            if data.get("success") is False:
                error = data.get("error", "未知错误")
            elif "error" in data:
                error = data["error"]
            else:
                msg = data.get("message", "")
                if msg:
                    message_content = msg

        if not error:
            # fallback: 无法解析 JSON 或 JSON 中无 error 时检查原始响应文本
            for resp in tool_responses:
                c = resp.get("content", "")
                if isinstance(c, str) and "失败" in c:
                    error = c[:120]
                    break

        key_str = ", ".join(f"{k}={v}" for k, v in key_fields.items())
        if error:
            result_summary = f"失败: {key_str} | err={error}"
        elif message_content:
            result_summary = f"成功: {key_str} — {message_content}"
        else:
            result_summary = f"OK: {key_str}"

        l1 = {
            "tool_name": "skill_manage",
            "tool_args": self._clean_tool_args("skill_manage", args),
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
        data = self._parse_json_response(tool_responses)
        if data and data.get("success") is False:
            error = data.get("error", "未知错误")

        key_str = ", ".join(f"{k}={v}" for k, v in key_fields.items())
        if error:
            result_summary = f"失败: {key_str} | err={error}"
        elif content_preview:
            result_summary = f"{action} {target}: {content_preview}"
        else:
            result_summary = f"{action} {target} (no output)"

        l1 = {
            "tool_name": "memory",
            "tool_args": self._clean_tool_args("memory", args),
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
        """todo Fct=全量列表：保留每项 content+status；Hdl=计数+首个提示"""
        args = tool_call_msg.get("function", {}).get("arguments", {})

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

        # Fct: 全量任务列表（content+status），保留全部互信息
        task_list = [{"content": t.get("content"), "status": t.get("status")}
                     for t in items if t.get("content")]
        result_summary = json.dumps(task_list, ensure_ascii=False) if task_list else "[]"

        # Hdl: 计数 + 首个任务作线索
        text_parts = []
        if pending:
            text_parts.append(f"{pending} pending")
        if in_progress:
            text_parts.append(f"{in_progress} in_progress")
        if completed:
            text_parts.append(f"{completed} completed")
        if cancelled:
            text_parts.append(f"{cancelled} cancelled")
        summary_text = f"todo: {total} 项"
        if text_parts:
            summary_text += f"（{', '.join(text_parts)}）"
        # 附加首个任务作线索
        first_content = ""
        for t in items:
            c = t.get("content", "")
            if c:
                first_content = c[:40]
                break
        if first_content:
            summary_text += f" — {first_content}"

        l1 = {
            "tool_name": "todo",
            "tool_args": self._clean_tool_args("todo", args),
            "result_summary": result_summary,
            "error": None,
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
        p1 = self._extract_fields(combined_results, priority["p1"], full=True, truncate=True)

        result_summary = ""
        error = vip.get("error")
        if error:
            result_summary = "失败"
        else:
            # 按配置顺序选择 P0 字段（配置即唯一真源，不用硬编码覆盖）
            for key in priority["p0"]:
                if key in p0 and p0[key]:
                    raw = p0[key]
                    # 确保值为字符串，过滤 raw JSON dict/list
                    if isinstance(raw, (dict, list)):
                        raw = str(raw)
                    result_summary = self._sanitize_summary_text(raw)
                    break
            if not result_summary:
                for key in priority["p1"]:  # 按配置顺序选择 P1 字段
                    if key in p1 and p1[key]:
                        raw = p1[key]
                        if isinstance(raw, (dict, list)):
                            raw = str(raw)
                        result_summary = self._sanitize_summary_text(raw)
                        break
            if not result_summary and vip.get("status"):
                result_summary = self._sanitize_summary_text(vip["status"])

        # 最终安全截断：不管 result_summary 来自哪条路径，超过 300 字就截掉
        if result_summary and len(result_summary) > 300:
            result_summary = _safe_truncate(result_summary, 300)

        l1 = {
            "tool_name": tool_name,
            "tool_args": self._clean_tool_args(tool_name, arguments),
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
    def _parse_json_response(responses: List[Dict]) -> Optional[Dict]:
        """从 tool_responses 中解析第一个可用的 JSON dict 响应。返回 None 表示无解析结果。"""
        for resp in responses:
            c = resp.get("content", "")
            if isinstance(c, str) and c.startswith("{"):
                try:
                    data = json.loads(c)
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, TypeError):
                    continue
        return None

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

    # 无信息量的过渡词/句（作前缀剥离用，不含句号也匹配）
    _TRANSITION_PREFIXES = sorted([
        "开始。", "开始审视。", "查代码。", "明白了。",
        "开始", "开始审视", "查代码", "明白了",
    ], key=len, reverse=True)  # 长串优先匹配

    @staticmethod
    def generate_group_summary(thought: str,
                                tool_results: List[Dict[str, Any]] = None) -> str:
        """从 thought 提取工具组摘要（软目标 150 字，超过 150 时在句尾截断）。

        tool_results 参数已弃用——仅保留签名兼容历史调用。
        返回纯文本（非 JSON），直接写入 Fct 供 A-stage 注入。

        Returns:
            纯文本摘要；如果 thought 为空或仅为过渡词则返回 ""
        """
        text = (thought or "").strip()
        if not text:
            return ""

        # 剥离无信息量的过渡词前缀，露出真正的 thought
        for prefix in ToolSummarizer._TRANSITION_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if not text:
            return ""

        # 按句尾分割后逐句累加，超过 150 字时在该句尾截断
        sentences = re.split(r'(?<=[。！？.!?])\s*', text)
        result = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(result) + len(s) > 150 and result:
                # 超过 150 字，包含当前句后返回
                return (result + " " + s).strip()
            if result:
                result += " "
            result += s
        if result:
            if len(result) > 150:
                return _safe_truncate(result, 150)
            return result
        return _safe_truncate(text, 150)

    @staticmethod
    def _clean_tool_args(tool_name: str, args: dict) -> dict:
        """去除 tool_args 中负载型字段（完整代码体、文件内容体），保留语义关键字段。

        terminal     → 去 command
        execute_code → 去 code
        write_file   → 去 content
        patch        → 去 old_string, new_string
        MCP 变体     → 只要有 code 就去掉
        其他工具     → 原样保留（args 体积极小，如 read_file 的 path）
        """
        HEAVY_FIELDS = {
            "execute_code": {"code"},
            "terminal": {"command"},
            "write_file": {"content", "file_content"},
            "patch": {"old_string", "new_string"},
            "skill_manage": {"old_string", "new_string", "content", "file_content"},
            "memory": {"content", "old_text", "old_string"},
        }
        drop = set(HEAVY_FIELDS.get(tool_name, []))
        # 兜底：任何工具只要有 code 字段就去掉（覆盖 MCP 变体等）
        if "code" in args:
            drop.add("code")
        if not drop:
            return args
        return {k: v for k, v in args.items() if k not in drop}
