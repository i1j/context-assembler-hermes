---
title: CE Shell 注册（ContextEngine ABC）
slug: ce-shell-registration
category: decision
date: "2026-06"
version_introduced: v5.10
affects: [plugin]
status: 已实装（v6.0 停用）
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
