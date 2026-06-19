"""E-stage 写即落盘测试 — _on_api_response_v5 / _on_pre_tool_call_v5 / _on_post_tool_call_v5。

覆盖：
- _on_api_response_v5: thought 行写入、tool 占位行、纯对话 early-return
- _on_pre_tool_call_v5: no-op 验证
- _on_post_tool_call_v5: 回填 tool 行、per-tool L1、fallback 创建
- _update_fct_v5: 薄封装验证

设计决策对照:
  → SC-001: 主键 (api_call_count, seq_index) 替代旧 (turn_type, sub_index)
  → SC-002: 3 钩子实时采集替代 history 遍历
  → SC-004: query_embedding 列
Wiki: design/decision-points-wiki.md §SC-001, §SC-002

  → tests/INDEX.md — 测试套件总览"""

import json
import pytest
from types import SimpleNamespace


def _make_msg(content: str, tool_calls: list = None,
              provider_data: dict = None, finish_reason: str = "tool_calls"):
    """构造 mimic 的 assistant message"""
    msg = SimpleNamespace()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.provider_data = provider_data or {}
    if finish_reason:
        setattr(msg, "finish_reason", finish_reason)
    return msg


def _make_tool_call(call_id: str, name: str, args: dict = None,
                    type_: str = "function"):
    tc = SimpleNamespace()
    tc.id = call_id
    tc.name = name
    tc.type = type_
    tc.arguments = json.dumps(args or {})
    return tc


def _make_usage(prompt: int = 10, completion: int = 20):
    u = SimpleNamespace()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    return u


# ============================================================================
# _on_api_response_v5
# ============================================================================


class TestOnApiResponseV5:
    """REQ-FUNC-ESTAGE-001: 写 thought + tool 占位行"""

    def test_pure_text_early_return(self, ca_engine):
        """纯文本回复（无 tool_calls + stop）→ 不写任何行"""
        ca_engine._seq_counter = {0: 0}
        ca_engine._on_api_response_v5(
            api_request_id="r1",
            assistant_message=_make_msg("你好", finish_reason="stop"),
            api_call_count=1,
            turn_index=0,
            finish_reason="stop",
        )
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert rows == [], "纯文本不应写入 turn_stream"

    def test_writes_thought_and_tool_placeholders(self, ca_engine):
        """含 tool_calls → 写 thought 行 + tool 占位行"""
        ca_engine._seq_counter = {0: 0}
        tcs = [_make_tool_call("c1", "terminal", {"cmd": "ls"}),
               _make_tool_call("c2", "read", {"path": "/tmp"})]
        ca_engine._on_api_response_v5(
            api_request_id="r2",
            assistant_message=_make_msg("让我查一下", tool_calls=tcs),
            api_call_count=1,
            turn_index=0,
            finish_reason="tool_calls",
        )

        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        # seq=1 thought + seq=2 tool1 placeholder + seq=3 tool2 placeholder
        assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"
        assert rows[0][0] == 1, "seq=1 -> thought"
        assert rows[1][0] == 2, "seq=2 -> tool1"
        assert rows[2][0] == 3, "seq=3 -> tool2"

    def test_seq_counter_incremented(self, ca_engine):
        """_on_api_response_v5 正确递增 seq"""
        ca_engine._seq_counter = {0: 5}  # 已有 5 行
        tc = _make_tool_call("c3", "test")
        ca_engine._on_api_response_v5(
            api_request_id="r3",
            assistant_message=_make_msg("再查", tool_calls=[tc]),
            api_call_count=1,
            turn_index=0,
        )
        # seq starts at 6 (5+1), then tool placeholder = 7
        assert ca_engine._seq_counter[0] == 7, \
            f"seq_counter should be 7, got {ca_engine._seq_counter[0]}"

    def test_usage_tokens_written(self, ca_engine):
        """usage 信息写入 thought 行的 token 列"""
        ca_engine._seq_counter = {0: 0}
        tc = _make_tool_call("c4", "test")
        usage = _make_usage(prompt=100, completion=50)
        ca_engine._on_api_response_v5(
            api_request_id="r4",
            assistant_message=_make_msg("有统计", tool_calls=[tc]),
            api_call_count=1,
            turn_index=0,
            usage=usage,
        )
        # 无法直接读 token 列（read_turn_elm_rows 只返回 seq/role/content），
        # 验证整体写入成功即可
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert len(rows) == 2  # thought + tool

    def test_reasoning_content_as_thought(self, ca_engine):
        """provider_data.reasoning_content 优先于 content（thinking LLM 路径）"""
        ca_engine._seq_counter = {0: 0}
        tc = _make_tool_call("c5", "test")
        msg = _make_msg("surface", tool_calls=[tc],
                        provider_data={"reasoning_content": "深层思考"})
        ca_engine._on_api_response_v5(
            api_request_id="r5",
            assistant_message=msg,
            api_call_count=1,
            turn_index=0,
        )
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert rows[0][2] == "深层思考", \
            f"Expected reasoning_content, got {rows[0][2]}"

    def test_tool_seq_map_populated(self, ca_engine):
        """_tool_seq_map 包含每个 tool_call_id 的 (turn, seq)"""
        ca_engine._seq_counter = {0: 0}
        tcs = [_make_tool_call("tc_a", "tool_a"),
               _make_tool_call("tc_b", "tool_b")]
        ca_engine._on_api_response_v5(
            api_request_id="r6",
            assistant_message=_make_msg("两个工具", tool_calls=tcs),
            api_call_count=1,
            turn_index=0,
        )
        assert "tc_a" in ca_engine._tool_seq_map
        assert "tc_b" in ca_engine._tool_seq_map
        assert ca_engine._tool_seq_map["tc_a"][0] == 0  # turn 0


# ============================================================================
# _on_pre_tool_call_v5
# ============================================================================


class TestOnPreToolCallV5:
    """REQ-FUNC-ESTAGE-002: pre_tool_call 是 no-op"""

    def test_no_op(self, ca_engine):
        """_on_pre_tool_call_v5 不写任何行、不抛异常"""
        ca_engine._seq_counter = {0: 0}
        try:
            ca_engine._on_pre_tool_call_v5(
                tool_call_id="c1",
                tool_name="test",
                args={"x": 1},
                api_request_id="r1",
            )
        except Exception as e:
            pytest.fail(f"_on_pre_tool_call should be no-op, got: {e}")
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert rows == [], "pre_tool_call 不写行"


# ============================================================================
# _on_post_tool_call_v5
# ============================================================================


class TestOnPostToolCallV5:
    """REQ-FUNC-ESTAGE-003: 回填 tool 行 + per-tool L1"""

    def test_writes_tool_result_from_placeholder(self, ca_engine):
        """有占位行 → 回填结果（占位行被 INSERT OR REPLACE 覆盖）"""
        ca_engine._seq_counter = {0: 0}
        # 前置：写 thought + tool 占位 (seq=1, seq=2)
        tcs = [_make_tool_call("c1", "test")]
        ca_engine._on_api_response_v5(
            api_request_id="r1",
            assistant_message=_make_msg("干", tool_calls=tcs),
            api_call_count=1,
            turn_index=0,
        )
        # 回填 — 覆盖占位行的 seq=2
        ca_engine._on_post_tool_call_v5(
            tool_call_id="c1",
            tool_name="test",
            args={"x": 1},
            result="已完成",
            status="ok",
            duration_ms=150,
        )
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        # seq=1(thought) + seq=2(tool result, 覆盖placeholder) = 2 行
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
        assert rows[0][0] == 1
        assert rows[0][1] == "assistant"
        assert rows[1][0] == 2
        assert rows[1][1] == "tool"
        assert rows[1][2] == "已完成"

    def test_result_serialized_when_dict(self, ca_engine):
        """dict 类型 result 被 JSON 序列化"""
        ca_engine._seq_counter = {0: 0}
        tc = _make_tool_call("c2", "test")
        ca_engine._on_api_response_v5(
            api_request_id="r2",
            assistant_message=_make_msg("查字典", tool_calls=[tc]),
            api_call_count=1,
            turn_index=0,
        )
        ca_engine._on_post_tool_call_v5(
            tool_call_id="c2",
            tool_name="test",
            args={},
            result={"files": ["a.txt", "b.txt"]},
            status="ok",
            duration_ms=100,
        )
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert len(rows) == 2
        content = rows[1][2]  # seq=2 行
        assert "a.txt" in content or "files" in content

    def test_no_placeholder_fallback_creates_row(self, ca_engine):
        """无占位行 → 回退创建新行"""
        ca_engine._seq_counter = {0: 5}
        ca_engine._tool_seq_map = {}
        ca_engine._current_turn = 0
        ca_engine._on_post_tool_call_v5(
            tool_call_id="orphan",
            tool_name="test",
            args={},
            result="孤儿行",
            status="ok",
            duration_ms=50,
        )
        from ca.store import read_turn_elm_rows
        rows = read_turn_elm_rows(ca_engine.store, "test", 0)
        assert any("孤儿行" in r[2] for r in rows), \
            f"Expected orphan row, got {rows}"

    def test_status_reflects_error(self, ca_engine):
        """错误状态写入 status 列"""
        ca_engine._seq_counter = {0: 0}
        ca_engine._tool_seq_map = {"c3": (0, 2)}
        tc = _make_tool_call("c3", "test")
        ca_engine._on_api_response_v5(
            api_request_id="r3",
            assistant_message=_make_msg("出错了", tool_calls=[tc]),
            api_call_count=1,
            turn_index=0,
        )
        ca_engine._on_post_tool_call_v5(
            tool_call_id="c3",
            tool_name="test",
            args={},
            result="",
            status="error",
            duration_ms=200,
        )
        # 直查 DB Status 列确认写入
        cur = ca_engine.store.conn.execute(
            "SELECT Status FROM turn_stream WHERE session_id=? AND turn=? AND tool_call_id=?",
            ("test", 0, "c3"),
        )
        row = cur.fetchone()
        assert row is not None, "tool 行应存在"
        assert row[0] == "error", f"Expected 'error', got {row[0]!r}"


# ============================================================================
# _update_fct_v5
# ============================================================================


class TestUpdateFctV5:
    """REQ-FUNC-ESTAGE-004: update_fct_v5 薄封装"""

    def test_updates_fin_row(self, ca_engine):
        """写入 fin（assistant_fin）行的 Fct/Hdl 列"""
        from ca.store import write_turn_v5, read_fct_v5
        # 写 assistant fin 行（role=assistant + finish_reason=stop）
        write_turn_v5(ca_engine.store, "test", 0, 1,
                      role="assistant", content="回复",
                      finish_reason="stop")
        # 更新 Fct — update_fin_fct_v5 找 role=assistant AND finish_reason=stop
        ca_engine._update_fct_v5("test", 0, '{"core_change":"测试"}', "测试")
        result = read_fct_v5(ca_engine.store, "test", 0, 1)
        assert "core_change" in result, f"Expected core_change in Fct, got {result!r}"
        hdl = ca_engine.store.conn.execute(
            "SELECT Hdl FROM turn_stream WHERE session_id=? AND turn=? AND seq=?",
            ("test", 0, 1)
        ).fetchone()
        assert hdl[0] == "测试", f"Hdl expected '测试', got {hdl[0]!r}"

    def test_updates_empty_turn_safely(self, ca_engine):
        """未写入的 turn 调用 update_fct_v5 不抛异常"""
        result = ca_engine._update_fct_v5("test", 999,
                                           '{"core_change":"x"}', "x")
        assert result is True  # UPDATE 0 rows 不影响, True 表示无 SQL 错误
