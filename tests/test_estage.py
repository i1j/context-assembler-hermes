
"""E-stage 测试 — 写即落盘 hooks"""

import json
from types import SimpleNamespace
import pytest


class TestPostApiResponseV5:
    """_on_api_response_v5 — thought 写入 + tool 占位"""

    def _make_msg(self, content, tool_calls=None):
        """Create a simple object mimicking assistant_message."""
        msg = SimpleNamespace()
        msg.content = content
        msg.tool_calls = tool_calls or []
        return msg

    def _make_tool_call(self, call_id, name, args):
        tc = SimpleNamespace()
        tc.id = call_id
        tc.name = name
        tc.arguments = json.dumps(args)
        return tc

    def test_writes_thought_and_tool_placeholders(self):
        """assistant 含 tool_calls → 写 thought 行 + per-tool 占位行"""
        import importlib, sys
        from pathlib import Path
        from ca import ContextAssembler

        _f = Path(__file__).resolve().parent.parent / "__init__.py"
        _spec = importlib.util.spec_from_file_location("ca_assembler_plug_v5c", _f)
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["ca_assembler_plug_v5c"] = _mod
        _spec.loader.exec_module(_mod)

        engine = ContextAssembler(db_path=":memory:", session_id="test")
        plugin = _mod.CAContextAssemblerPlugin()
        plugin._engine = engine
        plugin._session_id = "test"
        engine._current_turn = 1
        engine._seq_counter = {1: 0}
        engine._tool_seq_map = {}

        msg = self._make_msg("让我查一下数据", [
            self._make_tool_call("call_1", "read_file", {"path": "/tmp"}),
        ])

        engine._on_api_response_v5(
            api_request_id="req1",
            assistant_message=msg,
            api_call_count=1,
            turn_index=1,
            finish_reason="tool_calls",
            usage=None,
        )

        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(engine.store, "test", 1)
        assert len(rows) >= 2
        assert rows[0][1] == "assistant"  # thought
        assert "让我查一下数据" in rows[0][2]

    def test_no_tool_calls_skips_writes(self):
        """纯文本回复（无 tool_calls）→ 不写任何行到 turn_stream。"""
        import importlib, sys
        from pathlib import Path
        from ca import ContextAssembler

        _f = Path(__file__).resolve().parent.parent / "__init__.py"
        _spec = importlib.util.spec_from_file_location("ca_assembler_plug_v5d", _f)
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["ca_assembler_plug_v5d"] = _mod
        _spec.loader.exec_module(_mod)

        engine = ContextAssembler(db_path=":memory:", session_id="test")
        plugin = _mod.CAContextAssemblerPlugin()
        plugin._engine = engine
        plugin._session_id = "test"
        engine._current_turn = 2
        engine._seq_counter = {2: 0}
        engine._tool_seq_map = {}

        msg = self._make_msg("这是纯文本回复")

        engine._on_api_response_v5(
            api_request_id="req2",
            assistant_message=msg,
            api_call_count=1,
            turn_index=2,
            finish_reason="stop",
            usage=None,
        )

        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(engine.store, "test", 2)
        assert len(rows) == 0  # 纯文本回复不写 turn_stream
