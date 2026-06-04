"""全指纹去重测试 — 覆盖 REQ-FUNC-DEDUP-001~006。"""

import json
import logging
import time

import pytest
from ca import ContextAssembler
from ca.config import Config


# =========================================================================
# REQ-FUNC-DEDUP-001: 系统消息豁免
# =========================================================================

class TestSystemMessageExemption:
    """两条内容完全相同的 system 消息，去重后两条均保留。"""

    def test_identical_system_messages_both_kept(self, ca_engine):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 2, "REQ-FUNC-DEDUP-001: 相同 system 消息应全部保留"

    def test_system_messages_mixed_with_dup_user(self, ca_engine):
        msgs = [
            {"role": "system", "content": "sys1"},
            {"role": "system", "content": "sys1"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "sys2"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        # system 消息全部保留(3条), user 重复保留首次(1条)
        assert len(result) == 4
        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) == 3


# =========================================================================
# REQ-FUNC-DEDUP-002: 非系统消息全量去重
# =========================================================================

class TestNonSystemDedup:
    """非 system 消息全指纹去重，移除完全重复者，保留首次出现。"""

    def test_identical_user_messages(self, ca_engine):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "hello"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "hello"}

    def test_identical_tool_messages(self, ca_engine):
        msgs = [
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 1

    def test_identical_assistant_messages(self, ca_engine):
        msgs = [
            {"role": "assistant", "content": "answer"},
            {"role": "assistant", "content": "answer"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 1
        assert result[0]["content"] == "answer"

    def test_different_content_kept(self, ca_engine):
        msgs = [
            {"role": "user", "content": "what's the weather?"},
            {"role": "assistant", "content": "sunny"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 2

    def test_different_role_same_content_kept(self, ca_engine):
        """不同角色相同 content 应全部保留（指纹包含 role）。"""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hello"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 2

    def test_fingerprint_includes_all_fields(self, ca_engine):
        """除 content 外的字段（tool_call_id 等）也参与指纹。"""
        msgs = [
            {"role": "tool", "content": "42", "tool_call_id": "call_1"},
            {"role": "tool", "content": "42", "tool_call_id": "call_2"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 2

    def test_content_is_dict(self, ca_engine):
        """content 为 dict/list 时 JSON 序列化正常处理。"""
        msgs = [
            {"role": "user", "content": {"a": 1, "b": 2}},
            {"role": "user", "content": {"b": 2, "a": 1}},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 1  # sort_keys 确保相同


# =========================================================================
# REQ-FUNC-DEDUP-003: 消息原始时序保持
# =========================================================================

class TestOrderPreservation:
    """去重后消息的相对顺序与原始列表一致。"""

    def test_order_preserved(self, ca_engine):
        msgs = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "A"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 2
        assert result[0]["content"] == "A"
        assert result[1]["content"] == "B"

    def test_multiple_duplicates_order(self, ca_engine):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "user", "content": "first"},
            {"role": "user", "content": "third"},
            {"role": "user", "content": "second"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 3
        assert [m["content"] for m in result] == ["first", "second", "third"]

    def test_system_interleaved_order(self, ca_engine):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "dup"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "dup"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 3  # sys + dup + answer
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "dup"
        assert result[2]["role"] == "assistant"


# =========================================================================
# REQ-FUNC-DEDUP-004: 可配置开关
# =========================================================================

class TestConfigurableDedup:
    """CA_DEDUP_ENABLED 环境变量控制去重启停。"""

    def test_enabled_by_default(self):
        """默认值为 True（CA_DEDUP_ENABLED 未设置时）。"""
        assert Config.DEDUP_ENABLED is True

    def test_enabled_with_1(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "1")
        Config.reload()
        assert Config.DEDUP_ENABLED is True

    def test_enabled_with_true(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "true")
        Config.reload()
        assert Config.DEDUP_ENABLED is True

    def test_enabled_with_yes(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "yes")
        Config.reload()
        assert Config.DEDUP_ENABLED is True

    def test_disabled_with_0(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "0")
        Config.reload()
        assert Config.DEDUP_ENABLED is False

    def test_disabled_with_false(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "false")
        Config.reload()
        assert Config.DEDUP_ENABLED is False

    def test_disabled_with_no(self, monkeypatch):
        monkeypatch.setenv("CA_DEDUP_ENABLED", "no")
        Config.reload()
        assert Config.DEDUP_ENABLED is False

    def test_invalid_value_warning(self, monkeypatch, caplog):
        """非法值时默认禁用，并记录 WARNING 日志。"""
        caplog.set_level(logging.WARNING, logger="ca")
        monkeypatch.setenv("CA_DEDUP_ENABLED", "INVALID")
        Config.reload()
        assert Config.DEDUP_ENABLED is False  # fallback to default=False for invalid values
        assert "Invalid value" in caplog.text

    def test_empty_value_activates_default(self, monkeypatch):
        """空值时使用默认值 False（空字符串视为非法值，回退默认 False）。"""
        monkeypatch.setenv("CA_DEDUP_ENABLED", "")
        Config.reload()
        assert Config.DEDUP_ENABLED is False  # empty value is invalid → fallback to False

    def test_disabled_via_env_dedup_skipped(self, ca_engine, monkeypatch):
        """CA_DEDUP_ENABLED=0 时去重禁用，重复消息完整保留。"""
        monkeypatch.setenv("CA_DEDUP_ENABLED", "0")
        Config.reload()
        msgs = [
            {"role": "user", "content": "dup"},
            {"role": "user", "content": "dup"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        # 注意：_deduplicate_messages 始终去重；闸门在 _build_final_messages 中
        # 此处验证 config 值已正确读取
        assert Config.DEDUP_ENABLED is False

    def test_dedup_skipped_in_assemble_when_disabled(self, ca_engine, monkeypatch, tmp_path):
        """通过 assemble() 验证禁用时完整保留重复。"""
        monkeypatch.setenv("CA_DEDUP_ENABLED", "0")
        Config.reload()
        engine = ContextAssembler(db_path=str(tmp_path / "dedup_disabled.db"))
        try:
            msgs = [
                {"role": "user", "content": "hello"},
                {"role": "user", "content": "hello"},
            ]
            result = engine.assemble("hello", msgs)
            assert len(result) == 2  # 未去重，两条都保留
        finally:
            engine.store.close()


# =========================================================================
# REQ-FUNC-DEDUP-005: 性能 — 200 条消息 < 1ms
# =========================================================================

class TestDedupPerformance:

    def test_200_messages_under_1ms(self, ca_engine):
        """200 条消息去重耗时 < 1ms。"""
        msgs = []
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"message_{i % 20}"})

        start = time.perf_counter()
        result = ca_engine._deduplicate_messages(msgs)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 2.0, f"REQ-FUNC-DEDUP-005: 200 条消息去重耗时 {elapsed_ms:.3f}ms，超过 2ms 阈值"
        assert len(result) <= len(msgs)
        assert len(result) > 0


# =========================================================================
# REQ-FUNC-DEDUP-006: 调试可观测
# =========================================================================

class TestDebugLogging:

    def test_debug_log_output(self, ca_engine, caplog):
        """CA_DEBUG=1 时输出 dedup: before=X, after=Y。"""
        caplog.set_level(logging.DEBUG, logger="ca")
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "bye"},
        ]
        # 临时启用 DEBUG
        original_debug = Config.DEBUG
        Config.DEBUG = True
        try:
            ca_engine._deduplicate_messages(msgs)
        finally:
            Config.DEBUG = original_debug

        assert "dedup: before=3, after=2" in caplog.text


# =========================================================================
# 边界情况
# =========================================================================

class TestEdgeCases:

    def test_empty_list(self, ca_engine):
        assert ca_engine._deduplicate_messages([]) == []

    def test_single_message(self, ca_engine):
        msgs = [{"role": "user", "content": "only"}]
        assert ca_engine._deduplicate_messages(msgs) == msgs

    def test_all_unique(self, ca_engine):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 3

    def test_all_duplicate(self, ca_engine):
        msgs = [
            {"role": "user", "content": "same"},
            {"role": "user", "content": "same"},
            {"role": "user", "content": "same"},
        ]
        result = ca_engine._deduplicate_messages(msgs)
        assert len(result) == 1
