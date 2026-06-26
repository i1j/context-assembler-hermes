"""
ca/stats.py — 组装统计收集 (v4.4.0 alpha)

重命名为 AssembleStats，增加工具轮统计字段。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple


class AssembleStats:
    def __init__(self):
        self._start = time.monotonic()
        self._phase_stack: List[Tuple[str, float]] = []
        self._phase_pending: Optional[str] = None
        self._phase_pending_start: float = 0.0
        self._phase_times: Dict[str, float] = {}
        self.total_time_ms: float = 0.0
        self.tokens_before: int = 0
        self.tokens_after: int = 0
        self.savings_pct: float = 0.0
        self.error_counts: Dict[str, str] = {}
        self.phase_timing: Dict[str, float] = {}
        # 工具轮统计
        self.tool_backfill_success: int = 0
        self.tool_backfill_failure: int = 0
        # 话题统计（v4.6.0）
        self.topic_count: int = 0
        self.topic_retrieved_count: int = 0
        # Fct 统计（v2 refactor）
        self.truncated_fallback: int = 0
        self.parse_fallback_count: int = 0
        self.skipped_empty: int = 0
        self.fct_latency_ms: float = 0.0
        # 线程安全：F-stage 多线程并发更新计数器
        self._lock = threading.Lock()

    def time_phase(self, name: str):
        class _PhaseTimer:
            def __init__(self, outer: AssembleStats, phase_name: str):
                self._outer = outer
                self._phase_name = phase_name

            def __enter__(self):
                if self._outer._phase_pending is not None:
                    self._outer._phase_stack.append(
                        (self._outer._phase_pending, self._outer._phase_pending_start)
                    )
                self._outer._phase_pending = self._phase_name
                self._outer._phase_pending_start = time.monotonic()
                return self

            def __exit__(self, *args):
                elapsed = (time.monotonic() - self._outer._phase_pending_start) * 1000
                self._outer._phase_times[self._phase_name] = elapsed
                if self._outer._phase_stack:
                    self._outer._phase_pending, self._outer._phase_pending_start = \
                        self._outer._phase_stack.pop()
                else:
                    self._outer._phase_pending = None
        return _PhaseTimer(self, name)

    def record_error(self, source: str, message: str) -> None:
        self.error_counts[source] = message

    def finalize(self, tokens_before: int, tokens_after: int) -> None:
        self.tokens_before = tokens_before
        self.tokens_after = tokens_after
        if tokens_before > 0:
            self.savings_pct = (tokens_before - tokens_after) / tokens_before * 100
        self.total_time_ms = (time.monotonic() - self._start) * 1000
        if self._phase_times:
            self.phase_timing.update(self._phase_times)

    def __str__(self):
        parts = [f"t={self.total_time_ms:.0f}ms"]
        if self.phase_timing:
            pts = ",".join(f"{k}={v:.0f}ms" for k, v in sorted(self.phase_timing.items()))
            parts.append(pts)
        if self.savings_pct:
            parts.append(f"saved={self.savings_pct:.0f}%")
        if self.tool_backfill_success or self.tool_backfill_failure:
            parts.append(f"backfill_ok={self.tool_backfill_success}/fail={self.tool_backfill_failure}")
        if self.topic_count:
            parts.append(f"topics={self.topic_count}")
        if self.topic_retrieved_count:
            parts.append(f"topic_ret={self.topic_retrieved_count}")
        if self.truncated_fallback:
            parts.append(f"fct_trunc={self.truncated_fallback}")
        if self.parse_fallback_count:
            parts.append(f"fct_parse_fb={self.parse_fallback_count}")
        if self.skipped_empty:
            parts.append(f"hdl_skip={self.skipped_empty}")
        if self.fct_latency_ms:
            parts.append(f"fct_lat={self.fct_latency_ms:.0f}ms")
        if self.error_counts:
            parts.append(f"errors={list(self.error_counts.keys())}")
        return " | ".join(parts)
