---
title: 插件职责分离
slug: plugin-responsibility
category: decision
date: "2026-05"
version_introduced: v4.0
alternatives: ["所有逻辑写在 __init__.py（不可维护）", "逻辑外迁到独立服务（延迟）"]
chosen: "8 Hook 插件 + 断路器 + bg_review 跳过 + ToolBuffer"
affects: ["07-bg-review-sync-write"]
status: 已实装
source_files: ["ca/__init__.py", "ca/e_stage.py", "ca/lstage.py"]
---

## 触发条件

CA 作为一个 Hermes 插件，需确定哪些职责属于 CA、哪些属于外部系统。

## 选定

- **Hook 职责分离**：8 个 Hook 覆盖对话生命周期
- **断路器**：CA 失败不影响 Hermes 核心
- **bg_review 跳过**：后台审查轮不走完整路径
- **ToolBuffer**：工具结果缓冲（v5.0 已移除）

旧决策节点：`P-001`, `P-002`, `P-003`, `P-004`, `P-005`, `P-006`
