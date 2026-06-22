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
| `compress()` | FAR 话题行删除 + 尾区保护，原地 `messages[:] = filtered` | 删行减 token + 设 abort 标志阻止 session rotation |
| `update_from_response()` | 记录 token | 仅监控，不触发动作 |
| `on_session_start()` | 记录 session_id | 插件实例由 hook 创建，CE 不重复初始化 |
| `on_session_end()` | 透传 plugin.on_session_end() | 资源清理 |

### 三个约束条件

`should_compress=True` 的设计假设 Hermes 的 compress_context 管线满足以下三条约束：

1. **无条件触发**：不论是否有 FAR 话题、对话长短，`should_compress` 都返回 True
2. **永不 session 轮转**：`compress()` 在 `_last_compress_aborted = True`、`_last_summary_error = "CA: in-place FAR deletion, no rotation"` 预设 abort 标志 → compress_context 跳过 archive_and_compact 和 session ID 变更
3. **post_llm_call 恢复全量内容**：`compress()` 保存 `_full_backup`（含被删 FAR 行的完整副本），`post_llm_call_v5` 从 `_full_backup` 恢复内容到 state.db，保证 state.db 存的是 LLM 实际看到的上下文

## 对缓存的影响

- `should_compress=True` → Hermes `compress_context()` 每轮触发，`compress()` 设 abort 标志阻止 rotation
- hook 路径（每轮 A-stage 配送）仍独立运行，`_saved_history_snapshot` 在无 FAR 删除时被 `_full_backup` 恢复路径覆盖
- CE 路径不修改 turn_stream，仅操作 in-memory messages 列表，不影响 F-stage 异步摘要和数据持久化

## 代码位置

- `plugins/ca_assembler/__init__.py` — `CAContextEngine` 类 (L245+), `_ce_engine` 单例 (L450+), `register()` (L171+)
- `plugin.yaml` — version 6.0.0
- `tests/unit/test_ce_shell.py` — 8 项协议测试

## 相关决策

- CE-000~CE-003: 旧 monkey-patch 方案（已废弃）
- TP-009: 三方向战略决策
- TP-009b: 方向二 — CE 接口历史列表替换干行
- P-004: Hook 注册 + 8 hooks
