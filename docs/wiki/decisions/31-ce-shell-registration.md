---
source_files: ["__init__.py", "ca/a_stage.py"]
---

# CE-004: ContextEngine 壳注册

| 字段 | 值 |
|------|-----|
| 决策 ID | CE-004 |
| 版本 | v6.0 (2026-06-28 清理 CE 管线) |
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
CE 的 `on_session_start` 记录 session_id。

### v6 方向 B（2026-06-28）：CE 管线彻底停用

v6 方向 B 下，conv_history 由 `_build_conv_history_v6` 从 CA 自有 `turn_stream` DB 重建，
不再需要 Hermes CE 管线的参与。`should_compress()` 返回 False 完全停用 CE 压缩链路。

| 方法 | v6.0 状态 | v6.0 行为 | 原因 |
|------|----------|----------|------|
| `should_compress()` | ⚠ 已修改 | **返回 False** | 停用 Hermes compress_context，消除通过 should_compress → compress → abort → flush 链路间接写 state.db 的可能 |
| `compress()` | ⚠ 已修改 | **no-op**（return messages） | v6 方向 B 不需要 CE 管线介入消息列表 |
| `update_from_response()` | 不变 | 记录 token | 仅监控，不触发动作 |
| `on_session_start()` | 不变 | 记录 session_id | 插件实例由 hook 创建，CE 不重复初始化 |
| `on_session_end()` | 不变 | 透传 plugin.on_session_end() | 资源清理 |

### 执行顺序（当前）

```
1. pre_llm_call hook (v6)     → turn_stream 写用户消息 (seq=0) + 话题检测
2. LLM 调用
3. post_api_request hook (v6) → turn_stream 写 thought + tool 占位
4. post_tool_call hook (v6)   → turn_stream 回填 tool 行 + per-tool Fct
5. post_llm_call hook (v6)    → turn_stream 写 asst_fin + 触发 F-stage
```

CE 管线（should_compress → compress_context → compress → conversation_history_after_compression → flush）已完全停用。
Hermes 不再通过 CE 管线接触 CA 数据，`_flush_messages_to_session_db` 的 identity 去重不再依赖 CA 操作。

### 历史：旧设计（v6.0 初期，已清理）

旧设计中 `should_compress() → True` 配合 `_last_compress_aborted` flag 的 abort 机制，
会导致以下双写漏洞：

1. **should_compress()** → True，同时设 `_last_compress_aborted=True`（abort 标志）
2. **compress(messages)** → `_build_conv_history_v6` 构建 new_conv → 原地拷贝回 messages dict + 可能的 `messages.append()`（新 dict）
3. **Hermes compress_context** → 看到 abort 标志 → 跳过 archive_and_compact 和 session rotation → 但 compress() 已修改了 messages
4. **conversation_history_after_compression** → 返回 None（因 abort 路径未设 `_last_compaction_in_place`)
5. 后续 **`_persist_session(messages, conversation_history=None)`** → `history_ids=set()` → 步骤 2 中 `append()` 的新 dict 不被任何 `flushed_ids` 覆盖 → 写入 state.db 作为重复行

此漏洞于 2026-06-28 通过 `should_compress() → False` + `compress() → no-op` 彻底清理。

### 三个约束条件（历史参考）

旧 `should_compress=True` 的设计依赖以下三条未能同时满足的约束：

1. ~~无条件触发~~：已取消，返回 False
2. ~~永不 session 轮转~~：不再需要，compress_context 不触发
3. ~~identity 保持 → state.db 自动保护~~：不再需要，CA 不接触 Hermes 消息列表

## 守卫机制（历史参考）

旧 `compress()` 入口的 `_last_compress_msg_len` 守卫已被 `compress() → no-op` 替代。

## 代码位置

- `plugins/ca_assembler/__init__.py` — `CAContextEngine` 类, `_ce_engine` 单例, `register()`
- `plugin.yaml` — version 6.0.0
- `tests/unit/test_ce_shell.py` — 8 项协议测试

## 相关决策

- CE-000~CE-003: 旧 monkey-patch 方案（已废弃）
- TP-009: 三方向战略决策
- TP-009b: 方向二 — CE 接口历史列表替换干行
- P-004: Hook 注册 + 8 hooks
