---
title: A-stage conv_history 装配（方向 B）
slug: a-stage
category: architecture
version_introduced: v6.0
status: 已实装（代码+测试）｜生产已激活（select_context，2026-08-15）
decisions: [direction-b, tail-protection, topic-grade-manager]
depends_on: [store, topic-management]
updated: 2026-08-16
source_files: ["ca/a_stage.py"]
---

> 修订（2026-08-15）：`_build_conv_history_v6` 由 CE 壳 `CAContextEngine.select_context()`
> 每轮调用；`register()` 使用 1 参签名恢复 CE 壳注册，`should_compress()` 恒 False。
> 8 个 hooks 继续负责 E-stage 写入 / F-stage 摘要 / 话题检测 / recall 注入；
> 当前生产状态：select_context 驱动 A-stage 重建，生产已激活。

## 问题

v5 使用 topic-aware 三级替换（`_simple_mutation_mode_v5`）在 Hermes 消息列表上原地修改（mutation），破坏原始消息，需 `_full_backup` 回退。增量缓存与话题切换联动脆弱。

## 方向 B：`_build_conv_history_v6`

全量从 turn_stream DB 重建 conv_history 列表，**不接触 Hermes 消息**。

### 三区降级规则

| 区域 / 等级 | user | assistant(fin) | thought | tool |
|-------------|------|----------------|---------|------|
| Tail（保护区） | Elm | Elm | Elm | Elm |
| ACT | Elm | Elm | Fct | Fct |
| REL | Fct | Fct | Hdl | Hdl |
| FAR | Hdl | Hdl | 删除 | 删除 |

### 行类型降级

```
user/fin (高优先级) > thought (中) > tool (低)
```

- bg_review 行不计入 turn 计数
- Tail 保护区：固定最后 2 个 user 轮（仅 1 轮时全保护），**不做 token 预算扫描/扩展**
  （`decisions/13`，2026-08-08 用户裁定；`CA_PROTECT_TAIL_TOKENS` 语义已由
  `TOPIC_PEAK_TOKEN` 承接，不参与 A-stage 尾区）
