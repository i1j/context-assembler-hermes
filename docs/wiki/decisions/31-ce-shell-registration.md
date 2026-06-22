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
| `should_compress()` | 返回 False | 组装由 hook 路径驱动，CE 不替代 hook |
| `compress()` | FAR 话题行删除 + 尾区保护 | 手动 /compress 路径，复用 v5.10 数据管道 |
| `update_from_response()` | 记录 token | 仅监控，不触发动作 |
| `on_session_start()` | 记录 session_id | 插件实例由 hook 创建，CE 不重复初始化 |
| `on_session_end()` | 透传 plugin.on_session_end() | 资源清理 |

## 对缓存的影响

- `should_compress=False` → Hermes `compress_context()` 永不因 CA 自动触发
- `compress()` 返回短表后，Hermes 做 `archive_and_compact`（同 session_id，不轮转）
  或 session 轮转 —— 由 `compression.in_place` 配置决定，CE 壳不决策
- hook 路径（每轮 A-stage 配送）的缓存命中率与 v5.10 一致 —— CE 壳未改动 hook 代码

## 代码位置

- `plugins/ca_assembler/__init__.py` — `CAContextEngine` 类 (L245+), `_ce_engine` 单例 (L450+), `register()` (L171+)
- `plugin.yaml` — version 6.0.0
- `tests/unit/test_ce_shell.py` — 8 项协议测试

## 相关决策

- CE-000~CE-003: 旧 monkey-patch 方案（已废弃）
- TP-009: 三方向战略决策
- TP-009b: 方向二 — CE 接口历史列表替换干行
- P-004: Hook 注册 + 8 hooks
