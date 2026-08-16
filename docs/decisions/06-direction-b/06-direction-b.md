---
title: 方向 B — DB 重建 conv_history
slug: direction-b
category: decision
date: "2026-06"
version_introduced: v6.0
affects: [a-stage, topic-management]
status: 已实装（代码+测试）｜生产已激活（select_context，2026-08-15 修订）
source_files: ["ca/a_stage.py"]
---

## 触发条件

v5 mutation 模式（`_simple_mutation_mode_v5`）破坏 Hermes 消息，需要 `_full_backup` 回退。增量缓存与话题切换联动脆弱。

## 备选方案

1. **方向 A（继续 mutation）** — 继续修复 mutation 缺陷
2. **方向 B（DB 重建）** — 从 turn_stream 重建，不碰 Hermes 消息
3. **弃用 CA 回 Hermes compress** — 退化

## 选定

方向 B：`_build_conv_history_v6` 全量从 DB 重建 conv_history。CE 管线 2026-06-28 停用。三区降级系统全面切换为 DB 重建模式。

## 影响

✅ 零消息污染，state.db 完整性保证 | ❌ 每次全量重建，turn>1000 后需关注性能

## 修订（2026-08-13 代码核对）
"已实装"仅指代码与测试落地，**生产从未激活**：`_build_conv_history_v6` 仅由 CE 壳
`compress()` 调用（`__init__.py:352`），而 CE 壳注册因签名错误从未成功、2026-08-13
起注册暂停（见 `decisions/20-ce-shell-registration.md` 修订）。当前生产 conv_history
由 Hermes 原生构建；A-stage 三区降级需在恢复 CE 壳并补齐 §6.3 前置条件后才会生效。

## 修订（2026-08-15：select_context 激活）
`register()` 恢复 1 参 CE 壳注册，`_build_conv_history_v6` 改由
`CAContextEngine.select_context()` 每轮驱动；`should_compress()` 恒 False。
方向 B 生产已激活，A-stage 三区降级随 select_context 每轮生效。
