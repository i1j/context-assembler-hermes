"""Unit tests for ca/stats.py — AssembleStats.

设计决策对照:
  → CR-002: stats.py 补全计数器未输出 (test_completion_counter_*)
  → R-004: F-stage metrics 记录 (test_assemble_stats_*)
  → tests/INDEX.md — 测试套件总览"""
import time
import pytest
from ca.stats import AssembleStats


class TestAssembleStats:
    def test_init(self):
        s = AssembleStats()
        assert s.upgrade_counts == {}
        assert s.error_counts == {}

    def test_time_phase(self):
        s = AssembleStats()
        with s.time_phase("test_phase"):
            time.sleep(0.01)
        assert s._phase_times.get("test_phase", 0) >= 0.009

    def test_time_phase_nested(self):
        s = AssembleStats()
        with s.time_phase("outer"):
            with s.time_phase("inner"):
                time.sleep(0.005)
        assert "outer" in s._phase_times
        assert "inner" in s._phase_times

    def test_time_phase_handles_exception(self):
        s = AssembleStats()
        with pytest.raises(ValueError):
            with s.time_phase("failing"):
                raise ValueError("boom")
        assert "failing" in s._phase_times

    def test_record_upgrade(self):
        s = AssembleStats()
        s.record_upgrade("topic", 3)
        assert s.upgrade_counts["topic"] == 3
        s.record_upgrade("topic", 2)
        assert s.upgrade_counts["topic"] == 5

    def test_record_error(self):
        s = AssembleStats()
        s.record_error("embed", "timeout")
        assert len(s.error_counts) == 1

    def test_finalize(self):
        s = AssembleStats()
        s.finalize(1000, 500)
        assert s.tokens_before == 1000
        assert s.tokens_after == 500

    def test_str_format(self):
        s = AssembleStats()
        s.record_upgrade("topic", 3)
        s.record_error("embed", "timeout")
        s.finalize(1000, 500)
        result = str(s)
        assert "topic" in result

    def test_multiple_errors(self):
        s = AssembleStats()
        s.record_error("a", "err1")
        s.record_error("b", "err2")
        assert len(s.error_counts) == 2
