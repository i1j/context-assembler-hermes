# Bug 卡片 #2: C-stage 异步写入未在超时内完成

## 现象
- `test_online.py::TestCStageWrite::test_process_turn_writes_to_db` — 调用 process_turn 后 `store.read_session` 返回空
- `test_online.py::TestCStageWrite::test_cstage_three_turns` — 同根因链式失败
- `test_online.py::TestCStageWrite::test_db_json_parseable` — 同根因链式失败

## 失败消息
```
assert len(records) >= 1
AssertionError: assert 0 >= 1
```

## 影响范围
C-stage 异步写入在在线测试场景中未在默认超时内完成

## 根因推测
`process_turn()` 或 `wait_for_pending()` 在插件包装场景中的行为与直接 `ContextAssembler` 不同。可能是插件层 `_engine` 引用的问题，或后台线程未启动。

## 复现
```bash
cd ~/projects/context-assembler
source venv/bin/activate
python -m pytest tests/test_online.py::TestCStageWrite -v --tb=long
```

## 涉及模块
- `plugins/context_engine/ca_assembler/__init__.py` — 插件层 C-stage
- `test_online.py` — seeded_engine fixture

## 优先级
中 — 影响插件集成验证
