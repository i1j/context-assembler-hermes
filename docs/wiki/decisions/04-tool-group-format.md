---
title: 工具组组装格式
slug: tool-group-format
category: decision
date: "2026-05"
version_introduced: v1.2
alternatives: ["纯文本拼接（丢失结构）", "带标签 JSON（v4.4 方案，过度结构化）"]
chosen: "带标签 JSON（v4.4）→ 纯文本替换或带标签追加（v4.5+）"
affects: []
status: 已实装
---

## 触发条件

工具调用在上下文中如何格式化，需在结构化和可读性之间平衡。

## 备选方案

1. **v4.4**：带标签 JSON `[~/{turn}/{sub}] {L1_json}`
2. **v4.5+**：纯文本替换或带标签追加模式

## 选定

- v4.4：`[~/{turn}/{sub}] {L1_json}` 带标签结构
- v4.5+：支持纯文本替换或带标签追加模式并行
- 工具行默认无标签

旧决策节点：`C-018`
