---
title: 设计哲学：三阶段架构起源
slug: design-philosophy
category: decision
date: "2026-05"
version_introduced: v0.x
affects: ["整个系统架构"]
status: 已实装（基线）
---

## 触发条件

从 Hermes compress engine（单条 Markdown 摘要）中解耦，需要独立的上下文管理方案。

## 备选方案

1. **原地修改 Hermes compress** — 与 Hermes 核心耦合太紧，不利于独立迭代
2. **独立插件 + 三阶段管线** — CA 插件通过 Pipes 机制插入
3. **完全外部服务** — 网络延迟不可接受

## 选定

**独立插件 + 三阶段管线（C/A/L）**。CA 作为 Hermes 插件运行，通过 8 个 Hook 插入对话生命周期：
- **C-stage**：原始数据写入（→ 后演进为 E-stage）
- **A-stage**：上下文装配
- **L-stage**：摘要生成（→ 后演进的F-stage）

旧决策节点：`R-000`, `R-001`

## 之前 vs 之后

**之前**：Hermes compress 单条 Markdown 摘要，不可配置，不可调试
**之后**：三阶段管线，每个阶段独立开发、独立测试、独立降级

## 影响

✅ 解耦：CA 插件可独立开发和测试
✅ 可降级：每个阶段可以独立关闭或降级
❌ 三阶段命名后期被淘汰（C→E, L→F）
