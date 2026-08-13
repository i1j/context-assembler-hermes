---
title: A-stage conv_history 装配（方向 B）
slug: a-stage
category: architecture
version_introduced: v6.0
status: 已实装（代码+测试）｜生产未激活（2026-08-13 核对）
decisions: [direction-b, tail-protection, topic-grade-manager]
depends_on: [store, topic-management]
updated: 2026-08-13
source_files: ["ca/a_stage.py"]
---

> 修订（2026-08-13 代码核对）：**"已实装"仅指代码与测试落地，生产从未运行**——
> `_build_conv_history_v6` 在代码中**仅**由 CE 壳 `CAContextEngine.compress()` 调用
> （`__init__.py:352`），8 个 hooks 从不调用它；而 CE 壳注册自 2026-08-13 起暂停
> （原 2 参注册必抛 TypeError + Hermes 08-01 起失败回滚全部 hooks，详见
> `docs/migration-research-dsh.md` §1.5/§6）。因此当前生产状态：A-stage 重建未激活，
> conv_history 由 Hermes 原生构建；E-stage/F-stage/话题/recall 由 hooks 驱动照常运行。
> 恢复激活需先补齐：条件式 should_compress、pre_llm_call 模式守卫、前检压缩与
> seq 0 写入的轮序处理（§6.3）。

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
- Tail 保护区：最后 `CA_PROTECT_TAIL_TOKENS`（默认 20000）token 对应的 user 轮
