<!-- source_files: ["__init__.py", "ca/a_stage.py"] -->

# CE-004: ContextEngine 壳注册

| 字段 | 值 |
|------|-----|
| 决策 ID | CE-004 |
| 版本 | v6.0 |
| 日期 | 2026-06-22 |
| 父节点 | P-001 (Plugin 层职责分离) |

## 背景

CA 自 v5.10 起完全基于 Hermes 插件 hooks (pre_llm_call / post_llm_call / 3 工具钩子) 驱动。
TP-009 方向二提出通过 `ContextEngine` ABC 接口获得消息列表操控权（删行/插行/重组），
但旧 CE-000~CE-003（monkey-patch 接管 `_compress_context`) 已废弃。

## 决策

`register()` 新增 `ctx.register_context_engine("ca_assembler", _ce_engine)`。
`CAContextEngine` 单例实现 `agent.context_engine.ContextEngine` ABC，
作为 singleton 与现有 `_engines` 注册表协同工作：
CE 的 `on_session_start` 记录 session_id，`compress()` 通过 session_id
查找对应的 `CAContextAssemblerPlugin` 实例。

### 关键设计

| 方法 | 行为 | 原因 |
|------|------|------|
| `should_compress()` | 返回 True | 每轮触发 compress_context → compress() 做 FAR 行删除，但有 abort 约束阻止 archive/rotation |
| `compress()` | FAR 话题的 thought/tool 行对删除 + 尾区保护，原地 `messages[:] = filtered`；入口保存 `_full_backup` | 删行减 token + 设 abort 标志阻止 session rotation |
| `update_from_response()` | 记录 token | 仅监控，不触发动作 |
| `on_session_start()` | 记录 session_id | 插件实例由 hook 创建，CE 不重复初始化 |
| `on_session_end()` | 透传 plugin.on_session_end() | 资源清理 |

### 执行顺序

Hermes `turn_context.py` 在每轮 LLM 调用前的管线：

1. **should_compress()** → True，同时设 `_last_compress_aborted=True`（abort 标志）
2. **compress(messages)** → `_full_backup = 全量快照` → 交 A-stage 统一处理（删 FAR thought/tool 行对 + content 替换）
3. **Hermes compress_context** → 看到 abort 标志 → 跳过 archive_and_compact → 返回缩短版 messages
4. **pre_llm_call hook** → A-stage `_simple_mutation_mode_v5` 在缩短版上做 content 替换，保存 `_saved_history_snapshot`
5. **LLM 调用**
6. **post_llm_call hook** → `_full_backup` 恢复 content（不恢复已删行）→ `process_turn_f_stage`

> preflight 压缩循环（`turn_context.py:314-334`）最多执行 3 轮压缩。首轮删 FAR 行后，次轮因 `_last_compress_msg_len` 守卫提前返回。`_full_backup` 会被次轮覆盖，但因首轮已删完 FAR 行，覆盖后的备份结构与首轮一致。

### 三个约束条件

`should_compress=True` 的设计假设 Hermes 的 compress_context 管线满足以下三条约束：

1. **无条件触发**：不论是否有 FAR 话题、对话长短，`should_compress` 都返回 True
2. **永不 session 轮转**：`compress()` 在 `_last_compress_aborted = True`、`_last_summary_error = "CA: in-place FAR deletion, no rotation"` 预设 abort 标志 → compress_context 跳过 archive_and_compact 和 session ID 变更
3. **post_llm_call 恢复 content**：`compress()` 入口保存 `_full_backup`（含被删 FAR 行的原始 content），`post_llm_call_v5` 从 `_full_backup` 恢复 content 到 state.db（已删行本身不恢复）

## 对缓存的影响

- `should_compress=True` → Hermes `compress_context()` 每轮触发，`compress()` 设 abort 标志阻止 rotation
- hook 路径（A-stage）仍独立运行，`_saved_history_snapshot` 在 CE 激活时与 `_full_backup` 结构相同（均为压缩后的消息列表），post_llm_call 优先使用 `_full_backup` 恢复
- CE 路径不修改 turn_stream，仅操作 in-memory messages 列表，不影响 F-stage 异步摘要和数据持久化

## 守卫机制

`compress()` 入口的 `_last_compress_msg_len` 守卫防止重复压缩：

```python
last_len = getattr(self, '_last_compress_msg_len', 0)
if not force and len(messages) <= last_len:
    return messages
```

首轮压缩后设置 `_last_compress_msg_len = len(缩短版)`。次轮调用时消息未增长则提前返回。

## 代码位置

- `plugins/ca_assembler/__init__.py` — `CAContextEngine` 类, `_ce_engine` 单例, `register()`
- `plugin.yaml` — version 6.0.0
- `tests/unit/test_ce_shell.py` — 8 项协议测试

## 相关决策

- CE-000~CE-003: 旧 monkey-patch 方案（已废弃）
- TP-009: 三方向战略决策
- TP-009b: 方向二 — CE 接口历史列表替换干行
- P-004: Hook 注册 + 8 hooks
