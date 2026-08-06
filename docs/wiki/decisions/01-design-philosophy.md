---
title: 设计哲学：Elm/Fct/Hdl + 三阶段起源
slug: design-philosophy
category: decision
date: "2026-05"
version_introduced: v0.x
affects: [整个系统架构]
status: 已实装（基线）
source_files: ["ca/__init__.py"]
---

## 触发条件

从 Hermes compress engine（单条 Markdown 摘要）中解耦，需要独立的上下文管理方案。

## 备选方案

1. **原地修改 Hermes compress** — 与核心耦合太紧
2. **独立插件 + 三阶段管线** — CA 插件通过 Pipes 机制插入
3. **完全外部服务** — 网络延迟不可接受

## 选定

独立插件 + 三阶段管线。Elm/Fct/Hdl 三级编码，ACT/REL/FAR 话题定级驱动注入。

OV 节点：R-000（设计哲学），R-001（三阶段架构）

## 影响

✅ 解耦、独立迭代、独立降级 | ❌ 三阶段命名后被淘汰（C→E, L→F）
