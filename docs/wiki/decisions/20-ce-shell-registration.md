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
CAContextEngine 实现 ContextEngine ABC。`should_compress()=True` + `compress()` FAR 行过滤。Abort flag 阻止 session rotation。CE-000~CE-003 决策链。v6.0 方向 B 后 compress() 退化为仅 FAR 行删除，不再修改消息。
