"""
ca/lstage.py — L-stage 生命周期管理 (v5.0)

包含：
- LStageMixin   — 生命周期方法：reset, wait_for_pending 等

注：BackfillThread 已移除 — F-stage 代替了异步补全路径。
_fire_ov_submit 已移除 — CA_OV_SUBMIT_ENABLED 未启用，Config 项后续清理。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .config import Config
from .cache import CacheBuilder
from .stats import AssembleStats

logger = logging.getLogger(__name__)


class LStageMixin:
    """L-stage 混合类。由 ContextAssembler 通过多重继承引入。

    包含引擎生命周期方法：reset, wait_for_pending 等。
    """

    def reset(self):
        """重置引擎状态：清缓存、pending 任务、stats。"""
        self.wait_for_pending(5.0)
        self.cache.destroy()
        builder = CacheBuilder(self.store)
        self.cache = builder.build(self._session_id)
        with self._task_lock:
            self._pending_tasks.clear()
        self.stats = AssembleStats()
        self._turn_counter = self._restore_turn_index()

    def wait_for_pending(self, timeout=30.0):
        """等待所有 F-stage 线程完成。"""
        deadline = time.monotonic() + timeout
        with self._task_lock:
            tasks = list(self._pending_tasks.values())
        for t in tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=max(0.1, remaining))
