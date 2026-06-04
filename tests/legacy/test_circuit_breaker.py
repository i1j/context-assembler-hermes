import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.context_engine.ca_assembler import (
    _record_failure,
    _record_success,
    _read_state,
    _write_state,
    is_available,
)


def _with_home(tmp_path, fn):
    """在临时 HERMES_HOME 下执行操作，保证状态文件隔离。"""
    old_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = str(tmp_path)
        return fn()
    finally:
        if old_home is not None:
            os.environ["HERMES_HOME"] = old_home
        else:
            del os.environ["HERMES_HOME"]


def test_breaker_opens_after_three_failures(tmp_path):
    def do_test():
        _write_state({"failures": 0})
        for _ in range(3):
            _record_failure()
        assert not is_available()

    _with_home(tmp_path, do_test)


def test_breaker_resets_after_cooldown(tmp_path):
    def do_test():
        _write_state({"failures": 3, "retry_after": 0})
        time.sleep(0.05)

        assert is_available()

    _with_home(tmp_path, do_test)


def test_success_resets_failures(tmp_path):
    def do_test():
        _write_state({"failures": 2})
        _record_success()
        state = _read_state()
        assert state.get("failures", 0) == 0

    _with_home(tmp_path, do_test)


def test_failure_count_persists(tmp_path):
    def do_test():
        _write_state({})
        _record_failure()
        _record_failure()
        state = _read_state()
        assert state.get("failures", 0) == 2

    _with_home(tmp_path, do_test)
