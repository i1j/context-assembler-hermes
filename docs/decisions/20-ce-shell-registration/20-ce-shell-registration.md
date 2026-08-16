---
title: CE Shell 注册（ContextEngine ABC）
slug: ce-shell-registration
category: decision
date: "2026-06"
version_introduced: v5.10
affects: [plugin]
status: 已实装（1 参注册 + select_context 驱动）｜2026-08-15 生产已激活
source_files: ["plugins/ca_assembler/__init__.py"]
---

## 触发条件
CA 需要作为 Hermes ContextEngine 注册才能激活 `compress()` 管线。v5.10 前无 ABC 实现，CE 管线不通。

## 选定
CAContextEngine 实现 ContextEngine ABC。`select_context()` 每轮从 turn_stream DB 全量重建
conv_history（`_build_conv_history_v6`，方向 B），`should_compress()` 恒 False，
`compress()` 仅作为手动 /compress 回退路径。CE-000~CE-003 决策链。CA 不触发 Hermes
compress_context 数据修改流程，作为**替代内置 compressor 的占位**。

## 修订（2026-08-13 代码核对）

- **注册实现长期错误**：代码调 `ctx.register_context_engine("ca_assembler", _ce_engine)`
  （2 参）vs Hermes 签名 `register_context_engine(self, engine)`（1 参）→ 必抛 TypeError，
  引擎**从未成功注册**（`hermes_cli/plugins.py:1898` 签名自 2026-04-06 起未变）。
- **后果升级（Hermes commit `22af80bcf`，2026-08-01）**：register() 抛异常会
  `_dispose_registrations` + `_remove_plugin_subscriptions`（plugins.py:4345-4361）
  → 8 个 hooks 一并回滚 → **整个插件加载失败、CA 停摆**（errors.log 08-13 连续
  "Failed to load plugin 'ca_assembler'"；agent.log 最后 CA 活动 08-12）。
  原"参数警告无害、hooks 不被回滚"的判定已失效。
- **修复**：`register()` 注释掉注册行（2026-08-13）。恢复路径见
  `docs/migration-research-dsh.md` §1.5/§6.3：改 1 参可激活 A-stage（需先补
  条件式 should_compress、pre_llm_call 模式守卫、前检压缩与 seq 0 的轮序处理）；
  纯占位则同时把 should_compress 改 False。

## 修订（2026-08-15：select_context 路线）

- **恢复 1 参注册**：`register()` 改为 `ctx.register_context_engine(_ce_engine)`
  （Hermes `hermes_cli/plugins.py:1898`），不再使用旧 2 参写法。
- **A-stage 改由 `select_context()` 驱动**：每轮从 turn_stream DB 重建 conv_history，
  与压缩语义解耦；`should_compress()` 恒 False，`compress()` 仅保留手动回退路径。
- 8 个 hooks 注册与分发保持不变；生产激活需 tester profile `context.engine: ca_assembler`。
