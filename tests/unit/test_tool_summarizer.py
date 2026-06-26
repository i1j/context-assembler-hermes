"""Unit tests for ca/tool_summarizer.py — ToolSummarizer & generate_group_summary.

设计决策对照:
  → C-004: 工具轮不调 LLM — 规则引擎摘要 (test_tool_summarizer_*)
Wiki: decision-points-wiki.md §C-004

  → tests/INDEX.md — 测试套件总览"""
import json
import pytest

from ca.tool_summarizer import ToolSummarizer


def _tc(args: dict) -> dict:
    """Build a tool_call message dict with arguments as parsed dict (handler-ready)."""
    name = args.pop("_name", "test")
    return {
        "role": "assistant",
        "content": "",
        "function": {"name": name, "arguments": args},
    }


def _tr(content: str = "ok", status: str = "ok") -> list:
    return [{"content": content, "status": status}]


# ── Constructor ──

class TestInit:
    def test_default_profile(self):
        s = ToolSummarizer()
        assert hasattr(s, "priority")

    def test_custom_profile_invalid_falls_back(self, tmp_path):
        """Invalid profile name falls back to default."""
        s = ToolSummarizer(profile="nonexistent_profile")
        assert hasattr(s, "priority")


# ── Utility methods ──

class TestUtilities:
    def test_sanitize_path(self):
        s = ToolSummarizer()
        result = s._sanitize_path("/home/user/long/path/to/file.txt")
        assert isinstance(result, str) and len(result) > 0

    def test_head_tail_truncate(self):
        s = ToolSummarizer()
        text = "a" * 300
        result = s._head_tail_truncate(text, max_len=100)
        assert len(result) <= 120

    def test_extract_fields(self):
        s = ToolSummarizer()
        data = [{"name": "a", "value": 1}, {"name": "b", "value": 2}]
        result = s._extract_fields(data, ["name"])
        assert len(result) > 0


# ── _summarize_execute_code (Hermes execute_code handler) ──

def _ec_resp(status: str, output: str = "", error: str = None,
             tool_calls_made: int = 0, duration: float = 0.5) -> list:
    """Build execute_code tool_responses list from status/output/error."""
    body = {"status": status, "output": output,
            "tool_calls_made": tool_calls_made, "duration_seconds": duration}
    if error:
        body["error"] = error
    return [{"content": json.dumps(body)}]


class TestExecuteCodeHandler:
    def test_basic_output(self):
        """成功响应：提取前 3 个非空输出行"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "print('hello')"}},
              "content": "跑个测试"}
        resp = _ec_resp("success", "line1\nline2\nline3\nline4")
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "line1" in l0
        assert "line2" in l1["result_summary"]
        assert "duration_seconds" not in l1["result_summary"]
        assert "tool_calls_made" not in l1["result_summary"]
        assert isinstance(l1["tool_args"], dict)
        assert "code" not in l1["tool_args"]

    def test_error_with_traceback(self):
        """错误响应：保留 status + traceback 首行"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "1/0"}},
              "content": "查一下"}
        traceback = "Traceback (most recent call last):\n  File \"script.py\", line 1\nZeroDivisionError: division by zero"
        resp = _ec_resp("error", output="", error=traceback)
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "error:" in l1["result_summary"] or "Traceback" in l1["result_summary"]
        assert "Traceback" in l0 or "error" in l0

    def test_pytest_detection(self):
        """pytest 输出 → 'pytest: N passed — Xs'"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "pytest"}},
              "content": "跑测试"}
        output = "some setup\n...\n3 passed, 1 failed in 2.34s\n"
        resp = _ec_resp("success", output)
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert l1["result_summary"] == "pytest: 3 passed, 1 failed — 2.34s"
        assert "pytest" in l0

    def test_pytest_passed_only(self):
        """仅有 passed 无 failed"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "pytest -x"}},
              "content": "跑简洁测试"}
        output = "....................\n19 passed in 0.58s\n"
        resp = _ec_resp("success", output)
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "pytest" in l1["result_summary"]
        assert "19 passed" in l1["result_summary"]

    def test_empty_output(self):
        """空输出 → 回退到行数"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "print('')"}},
              "content": "测空输出"}
        resp = _ec_resp("success", "")
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "0 lines" in l1["result_summary"] or "0" in l1["result_summary"]
        assert l0 is not None

    def test_code_stripped_from_args(self):
        """tool_args 不含 code 字段"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code",
                           "arguments": {"code": "import os\nprint('hi')\n",
                                         "timeout": 30}},
              "content": "跑代码"}
        resp = _ec_resp("success", "hi")
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "code" not in l1["tool_args"]
        assert l1["tool_args"].get("timeout") == 30

    def test_timeout_output(self):
        """超时响应：status=timeout，保留超时信息"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "import time; time.sleep(999)"}},
              "content": "跑慢代码"}
        resp = _ec_resp("timeout", "partial output\n⏰ Script timed out", error="Script timed out after 30s")
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "timeout" in l1["result_summary"] or "timed out" in l1["result_summary"] or "partial" in l1["result_summary"]

    def test_error_without_error_field(self):
        """error status 但无 error 字段：回退到 status + body"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "bad"}},
              "content": "出错查"}
        resp = [{"content": json.dumps({"status": "error", "output": "something went wrong",
                                        "tool_calls_made": 0, "duration_seconds": 0.1})}]
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "error" in l1["result_summary"]

    def test_tool_calls_count_in_summary(self):
        """tool_calls_made > 0 → result_summary 含 [N tc]"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "from hermes_tools import terminal; ..."}},
              "content": "用工具"}
        resp = _ec_resp("success", "result: ok\n", tool_calls_made=3)
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "[3 tc]" in l1["result_summary"]
        assert "duration_seconds" not in l1["result_summary"]

    def test_no_tool_calls_no_suffix(self):
        """tool_calls_made = 0 → 无 [0 tc] 后缀"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "print(1)"}},
              "content": "简单调用"}
        resp = _ec_resp("success", "1\n", tool_calls_made=0)
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert "[0 tc]" not in l1["result_summary"]

    def test_non_json_response_fallback(self):
        """响应非 JSON → 安全降级"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "echo"}},
              "content": ""}
        resp = [{"content": "plain text", "status": "error"}]
        l1, l0 = s._summarize_execute_code(tc, resp)
        assert l0 is not None

    def test_multiple_tool_responses(self):
        """多个 tool_responses → 只解析第一个 JSON"""
        s = ToolSummarizer()
        tc = {"function": {"name": "execute_code", "arguments": {"code": "print(42)"}},
              "content": "多响应"}
        resp = [
            {"content": json.dumps({"status": "success", "output": "42\n", "tool_calls_made": 0, "duration_seconds": 0.1})},
            {"content": json.dumps({"status": "success", "output": "extra\n", "tool_calls_made": 0, "duration_seconds": 0.2})},
        ]
        l1, l0 = s._summarize_execute_code(tc, resp)
        # 只取第一个的 "42"
        assert "42" in l1["result_summary"]
        assert "extra" not in l1["result_summary"]

    def test_handler_dispatch(self):
        """summarize() 正确分派到 _summarize_execute_code"""
        s = ToolSummarizer()
        tc_msg = {"role": "assistant",
                  "function": {"name": "execute_code", "arguments": '{"code": "print(1)"}'},
                  "content": "跑代码"}
        resp = _ec_resp("success", "1\n")
        l1, l0 = s.summarize(tc_msg, resp)
        assert isinstance(l1, dict)
        assert l1["tool_name"] == "execute_code"
        assert "1" in l1["result_summary"]


class TestTerminalHandler:
    def test_basic_output(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "grep", "command": "grep foo /tmp/x", "path": "/tmp/x"})
        l1, l0 = s._summarize_terminal(tc, _tr("line1\nline2"), tool_label="t")
        assert "line1" in l0

    def test_empty_response(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "ls", "path": "/"})
        l1, l0 = s._summarize_terminal(tc, _tr(""), tool_label="t")
        assert l0 is not None

    def test_error_status(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "rm", "path": "/x"})
        l1, l0 = s._summarize_terminal(tc, [{"content": "permission denied", "status": "error"}], tool_label="t")
        assert l0 is not None


# ── _summarize_read_file ──

class TestReadFileHandler:
    def test_basic(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "read", "file_path": "/home/test/file.py"})
        resp = [{"content": json.dumps({"content": "abc\ndef", "total_lines": 3}), "status": "ok"}]
        l1, l0 = s._summarize_read_file(tc, resp)
        assert "file" in l0.lower()

    def test_error(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "read", "file_path": "/nonexistent"})
        l1, l0 = s._summarize_read_file(tc, [{"content": "not found", "status": "error"}])
        assert l0 is not None


# ── _summarize_write_file ──

class TestWriteFileHandler:
    def test_basic(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "write", "file_path": "/home/test/out.txt"})
        l1, l0 = s._summarize_write_file(tc, _tr("done"))
        assert "out.txt" in l0

    def test_with_content(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "write", "file_path": "/home/test/out.txt", "content": "hello"})
        l1, l0 = s._summarize_write_file(tc, _tr("done"))
        assert "out.txt" in l0


# ── _summarize_search_files ──

class TestSearchFilesHandler:
    def test_no_results(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "search", "pattern": "*.py", "path": "/tmp"})
        resp = [{"content": json.dumps({"files": [], "matches": [], "stats": {}}), "status": "ok"}]
        l1, l0 = s._summarize_search_files(tc, resp)
        assert l0 is not None

    def test_with_results(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "search", "pattern": "*.py", "path": "/tmp"})
        resp = [{"content": json.dumps({
            "files": ["a.py", "b.py"],
            "matches": [{"file": "a.py", "line": 1, "text": "import os"}],
            "stats": {"total_matches": 3, "files_with_matches": 2}}), "status": "ok"}]
        l1, l0 = s._summarize_search_files(tc, resp)
        assert l0 is not None


# ── _summarize_patch ──

class TestPatchHandler:
    def test_basic(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "patch", "file_path": "/home/test/x.py", "content": "diff..."})
        l1, l0 = s._summarize_patch(tc, _tr("applied"))
        assert "x.py" in l0 or "patch" in l0


# ── _summarize_memory ──

class TestMemoryHandler:
    def test_basic(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "memory", "name": "test-fact"})
        l1, l0 = s._summarize_memory(tc, [{"content": "saved", "status": "ok"}])
        assert l0 is not None

    def test_no_args(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "memory"})
        l1, l0 = s._summarize_memory(tc, [{"content": "saved", "status": "ok"}])
        assert l0 is not None


# ── _summarize_todo ──

class TestTodoHandler:
    def test_basic(self):
        s = ToolSummarizer()
        tc = _tc({"_name": "todo_write", "todos": [{"content": "task1", "status": "pending"}]})
        l1, l0 = s._summarize_todo(tc, [{"content": "done", "status": "ok"}])
        assert l0 is not None


# ── summarize() dispatch ──

class TestSummarize:
    def test_dispatches_to_handler(self):
        s = ToolSummarizer()
        tc_msg = {"role": "assistant", "function": {"name": "read", "arguments": '{"file_path": "/tmp/x.py"}'}}
        resp = [{"content": json.dumps({"content": "hi", "total_lines": 1}), "status": "ok"}]
        l1, l0 = s.summarize(tc_msg, resp)
        assert l0 is not None
        assert isinstance(l1, dict)

    def test_unknown_tool_uses_terminal(self):
        s = ToolSummarizer()
        tc_msg = {"role": "assistant", "function": {"name": "unknown_tool_123", "arguments": '{"arg": "val"}'}}
        l1, l0 = s.summarize(tc_msg, _tr("done"))
        assert l0 is not None

    def test_json_string_args_adaptation(self):
        """args as JSON string gets auto-converted to dict."""
        s = ToolSummarizer()
        tc_msg = {"role": "assistant", "function": {"name": "read", "arguments": '{"file_path": "/tmp/x.py"}'}}
        resp = [{"content": json.dumps({"content": "abc", "total_lines": 1}), "status": "ok"}]
        l1, l0 = s.summarize(tc_msg, resp)
        assert l0 is not None

    def test_empty_function_args(self):
        s = ToolSummarizer()
        tc_msg = {"role": "assistant", "function": {"name": "read", "arguments": "{}"}}
        resp = [{"content": "error", "status": "error"}]
        l1, l0 = s.summarize(tc_msg, resp)
        assert l0 is not None


# ── generate_group_summary (static) ──

class TestGenerateGroupSummary:
    def test_basic(self):
        result = ToolSummarizer.generate_group_summary(thought="需要查询文件")
        assert isinstance(result, str) and len(result) > 0

    def test_no_thought(self):
        assert ToolSummarizer.generate_group_summary(thought="") == ""

    def test_whitespace_thought(self):
        assert ToolSummarizer.generate_group_summary(thought="   ") == ""

    def test_truncated(self):
        result = ToolSummarizer.generate_group_summary(thought="a" * 200)
        assert len(result) <= 160  # 软目标 150 字
