---
title: CE Shell 注册（ContextEngine ABC）
slug: ce-shell-registration
category: decision
date: "2026-06"
version_introduced: v5.10
affects: [plugin]
status: 已实装（v6.0 停用）→ 2026-08-13 注册暂停（修订）
source_files: ["plugins/ca_assembler/__init__.py"]
---

## 触发条件
CA 需要作为 Hermes ContextEngine 注册才能激活 `compress()` 管线。v5.10 前无 ABC 实现，CE 管线不通。

## 选定
CAContextEngine 实现 ContextEngine ABC。`should_compress()=True` + `compress()` 从
turn_stream DB 全量重建 conv_history（`_build_conv_history_v6`，方向 B），不再修改
Hermes 传入的 messages。Abort flag 阻止 session rotation（archive/rotation 全部跳过）。
CE-000~CE-003 决策链。v6.0 方向 B 后 compress() 语义为全量重建（非仅 FAR 行删除），
并视为**替代内置 compressor 的占位**——CA 不触发 Hermes compress_context 数据修改流程。

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
