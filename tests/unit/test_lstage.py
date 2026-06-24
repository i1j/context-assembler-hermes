"""LStageMixin 生命周期单元测试 — reset / wait_for_pending。

覆盖:
  - reset 清空 pending 任务
  - reset 后 cache 重建
  - wait_for_pending 超时行为
  - wait_for_pending 空任务列表

LStageMixin 由 ContextAssembler 通过多重继承引入，测试使用 ca_engine fixture。
"""
import threading
import time


class TestReset:
    """LStageMixin.reset() — 状态重置"""

    def test_clears_pending_tasks(self, ca_engine):
        """reset 清空 _pending_tasks"""
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        ca_engine._pending_tasks[99] = t
        ca_engine.reset()
        assert 99 not in ca_engine._pending_tasks

    def test_rebuilds_cache(self, ca_engine):
        """reset 重建 cache（新对象）"""
        old_cache = ca_engine.cache
        ca_engine.reset()
        assert ca_engine.cache is not old_cache

    def test_resets_stats(self, ca_engine):
        """reset 重建 stats（新对象）"""
        from ca.stats import AssembleStats
        old_stats = ca_engine.stats
        ca_engine.reset()
        assert ca_engine.stats is not old_stats
        assert isinstance(ca_engine.stats, AssembleStats)

    def test_multiple_resets_no_error(self, ca_engine):
        """连续多次 reset 不抛异常"""
        for _ in range(10):
            ca_engine.reset()

    def test_reset_without_pending_no_error(self, ca_engine):
        """无 pending 任务时 reset 不抛异常"""
        ca_engine.reset()

    def test_reset_updates_turn_counter(self, ca_engine):
        """reset 后 _turn_counter 与 DB 一致"""
        from ca.store import write_turn_v5
        write_turn_v5(ca_engine.store, "test", 5, 0, role="user", content="u5")
        ca_engine._session_id = "test"
        ca_engine.reset()
        assert ca_engine._turn_counter == 5


class TestWaitForPending:
    """LStageMixin.wait_for_pending() — 等待 F-stage 线程"""

    def test_empty_pending_returns_immediately(self, ca_engine):
        """无 pending 任务时立即返回"""
        ca_engine._pending_tasks.clear()
        ca_engine.wait_for_pending(timeout=5.0)

    def test_completed_thread_not_blocked(self, ca_engine):
        """已完成线程不阻塞 wait_for_pending"""
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        ca_engine._pending_tasks[1] = t
        ca_engine.wait_for_pending(timeout=1.0)

    def test_timeout_does_not_wait_forever(self, ca_engine):
        """超时后返回，不无限等待"""
        blocker = threading.Event()

        def slow_task():
            blocker.wait(30)

        t = threading.Thread(target=slow_task, daemon=True)
        t.start()
        ca_engine._pending_tasks[1] = t
        start = time.monotonic()
        ca_engine.wait_for_pending(timeout=0.01)
        elapsed = time.monotonic() - start
        blocker.set()  # cleanup
        assert elapsed < 5.0, f"wait_for_pending should timeout quickly, took {elapsed:.2f}s"

    def test_multiple_threads_all_waited(self, ca_engine):
        """多个 pending 线程全部等待完成"""
        completed = set()

        def make_task(task_id):
            def runner():
                time.sleep(0.01)
                completed.add(task_id)
            return threading.Thread(target=runner, daemon=True)

        ca_engine._pending_tasks = {}
        for i in range(5):
            t = make_task(i)
            t.start()
            ca_engine._pending_tasks[i] = t

        ca_engine.wait_for_pending(timeout=5.0)
        assert len(completed) == 5, f"Expected 5 completed, got {len(completed)}"
