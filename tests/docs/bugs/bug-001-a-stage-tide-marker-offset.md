# Bug 卡片 #1: A-stage [~/N] 标记索引偏移

## 现象
- `test_online.py::test_assemble_contains_tide_markers`: 断言 `[~/0]` 存在但输出中只有 `[~/1]` 开始
- `test_online.py::test_head_middle_tail_layering`: 期望 ≥3 个带 `[~/N]` 标记的 head 消息，实际只有 2 个（缺少第一轮的 `[~/0]`）

## 失败消息
```
assert '[~/0]' in 'You are a helpful assistant. [~/1] {"core_change": ...}'
AssertionError: Missing [~/0] marker
```

## 影响范围
- A-stage 中第一轮的 L1 未被正确注入 `[~/0]` 标记
- head/middle/tail 分层逻辑中 head 少一个条目

## 根因推测
标记索引可能从 1 开始计数而非 0，或者 `_hard_truncation` 或 CacheBuilder 在 turn_counter=0 时跳过了标记生成。

## 复现
```bash
cd ~/projects/context-assembler
source venv/bin/activate
python -m pytest tests/test_online.py::TestAStageAssembly -v --tb=long
```

## 涉及模块
- `ca/__init__.py` — assemble() 中 [~/N] 标记逻辑
- `ca/cache.py` — CacheBuilder 中 turn 索引

## 优先级
高 — 影响 A-stage 输出的正确性
