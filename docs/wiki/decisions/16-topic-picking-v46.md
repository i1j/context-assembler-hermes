---
title: 话题拣选（v4.6.0）
slug: topic-picking-v46
category: decision
date: "2026-06-08"
version_introduced: v4.6.0
alternatives: ["无话题分割（全局上下文处理）", "纯 LLM 判断话题边界（不可控）"]
chosen: "话题分割 → 三级定级 → TopicRetriever → topic_boost → 移除预选"
affects: ["08-topic-segmentation", "09-topic-grade-switch"]
status: 已实装（v5.5 后被 TopicGradeManager 取代）
---

## 触发条件

长对话中话题切换需要系统自动感知。

## 选定

- **话题分割**：TP-001
- **三级定级**：TP-002（旧版本级方案）
- **TopicRetriever**：TP-003 按话题检索
- **topic_boost**：TP-004 话题优先级提升
- **移除预选**：TP-005 不预先选择话题

旧决策节点：`TP-001`, `TP-002`, `TP-003`, `TP-004`, `TP-005`

## 影响

✅ 首次实现话题感知
❌ v5.5 被 TopicGradeManager 完全取代
