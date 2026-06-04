# Bug 卡片 #3: CAContextAssemblerPlugin 钩子行为与测试期望不符

## 现象（3 个子故障）

### 3a. `test_plugin.py::TestPreLlmCall::test_syncs_guard`
- `_on_pre_llm_call` 行为与 mock 期望不匹配

### 3b. `test_plugin.py::TestPostLlmCall::test_invokes_process_turn`
- `_on_post_llm_call` 未调用 `plugin._engine.process_turn`
```
Expected 'process_turn' to be called once. Called 0 times.
```

### 3c. `test_plugin.py::TestPostLlmCall::test_failure_sets_error_flag`
- 异常未设置 `_engine_errored` 标记
```
assert plugin._engine_errored is True
AssertionError: assert False is True
```

## 影响范围
插件层的 `pre_llm_call` / `post_llm_call` 钩子逻辑验证失败

## 根因推测
- 3b: `_on_post_llm_call` 内部可能通过其他路径处理 LLM 响应（如直接调用 `_engine.process_turn_async`），而非 `process_turn`
- 3c: `_on_post_llm_call` 的异常处理未设置 `self._engine_errored = True`

## 复现
```bash
cd ~/projects/context-assembler
source venv/bin/activate
python -m pytest tests/test_plugin.py -v --tb=long
```

## 涉及模块
- `plugins/context_engine/ca_assembler/__init__.py` — 钩子方法

## 优先级
中 — 影响插件层单元测试覆盖
