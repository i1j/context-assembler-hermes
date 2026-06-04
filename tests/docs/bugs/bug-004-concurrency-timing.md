# Bug 卡片 #4: test_concurrency — 多会话隔离在并发场景下超时

## 现象
- `test_concurrency.py::test_multi_session_isolation`: 两个独立的 `ContextAssembler` 实例，各自的 `wait_for_pending(5)` 超时前未完成写入
```
assert len(r1) == 1
AssertionError: assert 0 == 1
```

## 影响范围
多实例场景下的 C-stage 异步可靠性

## 根因推测
两个独立的 ContextAssembler 实例共享同一线程池或后台线程调度器导致竞争。或 `wait_for_pending` 的实现只等待了自己的线程但线程启动有延迟。

## 复现
```bash
cd ~/projects/context-assembler
source venv/bin/activate
# 注意：此测试在串行模式下可能通过
python -m pytest tests/test_concurrency.py::test_multi_session_isolation -v --tb=long -n 0
# 但在 xdist 并行模式下失败更频繁
python -m pytest tests/test_concurrency.py::test_multi_session_isolation -v --tb=long -n 4
```

## 涉及模块
- `ca/__init__.py` — process_turn_async / wait_for_pending
- `tests/test_concurrency.py`

## 优先级
低 — 只在并行环境下触发，串行独跑可能通过
