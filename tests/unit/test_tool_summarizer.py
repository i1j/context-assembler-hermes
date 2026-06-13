"""Unit tests for ca/tool_summarizer.py — ToolSummarizer & generate_group_summary."""
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


# ── _summarize_terminal (generic handler) ──

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
        assert len(result) <= 110
