"""
test_tool_buffer.py — PR2 Buffer 层测试

测试范围：
- ToolGroupBuffer 数据类
- _on_api_response / _on_pre_tool_call / _on_post_tool_call handler
- flush_tool_buffer 写入 + 清理
- Buffer 悬挂清理（destroy / reset）
- 多 API 同轮排序
- 容错 auto-create
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch
from typing import Any, Dict, List, Optional

import pytest

# ── Mock NormalizedResponse ──


class MockAssistantMessage:
    """模拟 Hermes NormalizedResponse 的 assistant_message。"""
    def __init__(self, content: str = "", tool_calls: Optional[List[Dict]] = None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockToolCall:
    """模拟 Hermes ToolCall。"""
    def __init__(self, id: str, name: str, arguments: str = "{}"):
        self.id = id
        self.name = name
        self.arguments = arguments
        self.type = "function"


# ── Fixtures ──


@pytest.fixture
def engine_with_buffer(ca_engine):
    """返回 engine，确保 buffer 为空且破坏 mock 已启动。"""
    assert len(ca_engine._tool_buffer) == 0
    yield ca_engine
    # teardown: 确保 buffer 已清理
    ca_engine._tool_buffer.clear()


# ── Tests ──


class TestToolGroupBuffer:
    """ToolGroupBuffer 数据类基础测试。"""

    def test_init_empty_results(self):
        """results 默认为空 dict"""
        from ca import ToolGroupBuffer
        buf = ToolGroupBuffer(thought="test", tool_defs=[], api_call_count=1, turn_index=1)
        assert buf.results == {}
        assert buf.thought == "test"
        assert buf.api_call_count == 1
        assert buf.turn_index == 1

    def test_init_with_results(self):
        """传入 results 保留"""
        from ca import ToolGroupBuffer
        buf = ToolGroupBuffer(thought="t", tool_defs=[], api_call_count=1, turn_index=1,
                              results={"c1": {"tool_name": "read_file"}})
        assert buf.results["c1"]["tool_name"] == "read_file"


class TestOnApiResponse:
    """_on_api_response handler 测试。"""

    def test_creates_buffer_entry(self, engine_with_buffer):
        """调用后 _tool_buffer 新增一个条目"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(
            content="我来查文件",
            tool_calls=[
                MockToolCall(id="c1", name="read_file", arguments='{"path":"/a"}'),
            ],
        )
        engine._on_api_response(
            api_request_id="req_001",
            assistant_message=msg,
            api_call_count=1,
            turn_index=1,
            finish_reason="tool_calls",
        )
        assert "req_001" in engine._tool_buffer
        buf = engine._tool_buffer["req_001"]
        assert buf.thought == "我来查文件"
        assert len(buf.tool_defs) == 1
        assert buf.tool_defs[0]["id"] == "c1"
        assert buf.api_call_count == 1
        assert buf.turn_index == 1

    def test_multiple_tools(self, engine_with_buffer):
        """多工具正确解析"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(
            content="查文件",
            tool_calls=[
                MockToolCall(id="c1", name="read_file"),
                MockToolCall(id="c2", name="search_files"),
                MockToolCall(id="c3", name="read_file"),
            ],
        )
        engine._on_api_response(
            api_request_id="req_002",
            assistant_message=msg,
            api_call_count=1,
            turn_index=1,
            finish_reason="tool_calls",
        )
        assert len(engine._tool_buffer["req_002"].tool_defs) == 3

    def test_pure_dialogue_skips_buffer(self, engine_with_buffer):
        """纯对话（无 tool_calls, finish_reason='stop'）不创建 buffer"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(content="你好")
        engine._on_api_response(
            api_request_id="req_003",
            assistant_message=msg,
            api_call_count=1,
            turn_index=1,
            finish_reason="stop",
        )
        assert "req_003" not in engine._tool_buffer


class TestOnPreToolCall:
    """_on_pre_tool_call handler 测试。"""

    def test_registers_placeholder(self, engine_with_buffer):
        """在已有 buffer 中注册 tool 占位"""
        engine = engine_with_buffer
        # 先创建 buffer
        msg = MockAssistantMessage(content="查", tool_calls=[MockToolCall(id="c1", name="read_file")])
        engine._on_api_response(
            api_request_id="req_p1", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        # pre_tool_call
        engine._on_pre_tool_call(
            tool_call_id="c1", tool_name="read_file",
            args={"path": "/a"}, api_request_id="req_p1",
        )
        buf = engine._tool_buffer["req_p1"]
        assert "c1" in buf.results
        assert buf.results["c1"]["tool_name"] == "read_file"
        assert buf.results["c1"]["status"] == "pending"

    def test_auto_create_missing_buffer(self, engine_with_buffer):
        """buffer 不存在时 auto-create sentinel"""
        engine = engine_with_buffer
        engine._on_pre_tool_call(
            tool_call_id="c_orphan", tool_name="read_file",
            args={}, api_request_id="req_orphan",
        )
        # 应创建 sentinel buffer
        assert "req_orphan" in engine._tool_buffer
        buf = engine._tool_buffer["req_orphan"]
        assert buf.api_call_count == 999999  # sentinel
        assert "c_orphan" in buf.results
        assert buf.results["c_orphan"]["status"] == "pending"


class TestOnPostToolCall:
    """_on_post_tool_call handler 测试。"""

    def test_fills_result(self, engine_with_buffer):
        """工具执行结果填入 buffer"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(content="查", tool_calls=[MockToolCall(id="c1", name="read_file")])
        engine._on_api_response(
            api_request_id="req_post1", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        # pre_tool_call
        engine._on_pre_tool_call(
            tool_call_id="c1", tool_name="read_file",
            args={"path": "/a"}, api_request_id="req_post1",
        )
        # post_tool_call
        engine._on_post_tool_call(
            tool_call_id="c1", tool_name="read_file",
            args={"path": "/a"}, result="files content",
            status="ok", duration_ms=150, api_request_id="req_post1",
        )
        buf = engine._tool_buffer["req_post1"]
        assert buf.results["c1"]["content"] == "files content"
        assert buf.results["c1"]["status"] == "ok"
        assert buf.results["c1"]["duration_ms"] == 150

    def test_error_status_preserved(self, engine_with_buffer):
        """原始 error status 保留"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(content="查", tool_calls=[MockToolCall(id="c1", name="read_file")])
        engine._on_api_response(
            api_request_id="req_err", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        engine._on_post_tool_call(
            tool_call_id="c1", tool_name="read_file",
            result="", status="error", duration_ms=50,
            error_type="FileNotFoundError", error_message="not found",
            api_request_id="req_err",
        )
        buf = engine._tool_buffer["req_err"]
        assert buf.results["c1"]["status"] == "error"
        assert buf.results["c1"]["error_type"] == "FileNotFoundError"

    def test_auto_create_missing_buffer(self, engine_with_buffer):
        """post_tool_call 时 buffer 不存在 → auto-create"""
        engine = engine_with_buffer
        engine._on_post_tool_call(
            tool_call_id="c_orphan_post", tool_name="search_files",
            result='{"files": ["x.py"]}',
            status="ok", duration_ms=80, api_request_id="req_orphan_post",
        )
        assert "req_orphan_post" in engine._tool_buffer
        buf = engine._tool_buffer["req_orphan_post"]
        assert buf.api_call_count == 999999  # sentinel


class TestFlushToolBuffer:
    """flush_tool_buffer 完整流程测试。"""

    def test_flush_empty_buffer(self, engine_with_buffer):
        """空 buffer flush 返回 0"""
        engine = engine_with_buffer
        n = engine.flush_tool_buffer(user_message="")
        assert n == 0

    def test_flush_single_group(self, engine_with_buffer):
        """单组 API 写入正确行数"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(
            content="查文件",
            tool_calls=[
                MockToolCall(id="c1", name="read_file", arguments='{"path":"/a"}'),
                MockToolCall(id="c2", name="search_files", arguments='{"pattern":"*.py"}'),
            ],
        )
        engine._on_api_response(
            api_request_id="req_flush1", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        engine._on_pre_tool_call(tool_call_id="c1", tool_name="read_file",
                                  args={"path": "/a"}, api_request_id="req_flush1")
        engine._on_pre_tool_call(tool_call_id="c2", tool_name="search_files",
                                  args={"pattern": "*.py"}, api_request_id="req_flush1")
        engine._on_post_tool_call(tool_call_id="c1", tool_name="read_file",
                                   result="aaa", status="ok", duration_ms=150,
                                   api_request_id="req_flush1")
        engine._on_post_tool_call(tool_call_id="c2", tool_name="search_files",
                                   result="x.py", status="ok", duration_ms=80,
                                   api_request_id="req_flush1")

        n = engine.flush_tool_buffer(user_message="查文件")
        # 1(user) + 1(assistant{tc}) + 2(tool) = 4 rows
        assert n == 4, f"Expected 4 rows, got {n}"
        # buffer 应已清空
        assert len(engine._tool_buffer) == 0

        # 验证 DB 内容
        records = engine.store.read_session("test")
        assert len(records) >= 4
        # user 行
        user_rows = [r for r in records if r["api_call_count"] == 0]
        assert len(user_rows) >= 1
        # assistant{tc} 行
        asst_rows = [r for r in records if r["api_call_count"] == 1 and r["turn_type"] == "dialogue"]
        assert len(asst_rows) >= 1
        # tool 行
        tool_rows = [r for r in records if r["turn_type"] == "tool"]
        assert len(tool_rows) >= 2

    def test_flush_multiple_api_groups(self, engine_with_buffer):
        """多 API 同轮，按 api_call_count 排序写入"""
        engine = engine_with_buffer
        # API 组 1 (api_call_count=1, 2 tools)
        msg1 = MockAssistantMessage(content="查", tool_calls=[
            MockToolCall(id="c1", name="read_file"), MockToolCall(id="c2", name="search_files"),
        ])
        engine._on_api_response(
            api_request_id="req_m1", assistant_message=msg1,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        # API 组 2 (api_call_count=2, 1 tool)
        msg2 = MockAssistantMessage(content="再查", tool_calls=[
            MockToolCall(id="c3", name="read_file"),
        ])
        engine._on_api_response(
            api_request_id="req_m2", assistant_message=msg2,
            api_call_count=2, turn_index=1, finish_reason="tool_calls",
        )
        # 填充结果
        for req_id, tc_id in [("req_m1", "c1"), ("req_m1", "c2"), ("req_m2", "c3")]:
            engine._on_pre_tool_call(tool_call_id=tc_id, tool_name="tool",
                                      api_request_id=req_id)
            engine._on_post_tool_call(tool_call_id=tc_id, tool_name="tool",
                                       result="ok", status="ok", duration_ms=10,
                                       api_request_id=req_id)

        n = engine.flush_tool_buffer(user_message="查")
        # 1(user) + 2(assistant{tc}) + 3(tool) = 6 rows
        assert n == 6, f"Expected 6 rows, got {n}"

        # 验证排序
        records = engine.store.read_session("test")
        api_order = [(r["api_call_count"], r["turn_type"]) for r in records]
        # 按 api: user(0) → api1 assistant → api1 tool×2 → api2 assistant → api2 tool
        assert api_order[0] == (0, "dialogue"), f"Expected user first: {api_order}"
        assert (1, "dialogue") in api_order
        assert (2, "dialogue") in api_order
        assert len([r for r in records if r["turn_type"] == "tool"]) == 3

    def test_flush_clears_buffer(self, engine_with_buffer):
        """flush 后 buffer 为空"""
        engine = engine_with_buffer
        msg = MockAssistantMessage(content="x", tool_calls=[MockToolCall(id="c1", name="tool")])
        engine._on_api_response(
            api_request_id="req_clear", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        engine._on_post_tool_call(tool_call_id="c1", tool_name="tool",
                                   result="ok", status="ok", duration_ms=1,
                                   api_request_id="req_clear")
        engine.flush_tool_buffer(user_message="x")
        assert len(engine._tool_buffer) == 0


class TestBufferCleanup:
    """Buffer 悬挂清理测试。"""

    def test_destroy_flushes_hanging_buffer(self, ca_engine):
        """destroy 时 flush 悬挂 buffer"""
        engine = ca_engine
        msg = MockAssistantMessage(content="x", tool_calls=[MockToolCall(id="c1", name="tool")])
        engine._on_api_response(
            api_request_id="req_hang", assistant_message=msg,
            api_call_count=1, turn_index=1, finish_reason="tool_calls",
        )
        assert len(engine._tool_buffer) == 1
        # destroy 应 flush buffer
        engine.destroy()
        # destroy 后不应有遗留
        assert len(engine._tool_buffer) == 0
