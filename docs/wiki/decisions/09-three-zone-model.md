---
title: A-stage 三区模型
slug: three-zone-model
category: decision
date: "2026-05"
version_introduced: v2.0
alternatives: ["全部保留（超出 context length）", "全部截断（丢失上下文）"]
chosen: "active（尾部保留）/ retrieval（检索区）/ fallback（降级区）"
affects: ["04-a-stage-role-match"]
status: 已实装（v5.5 后被 topic-aware 角色队列匹配取代）
source_files: []  # 已被 TopicGrade 取代，历史参考
---

## 触发条件

A-stage 装配时，历史上下文需要分级处理。

## 选定

- **active 区**：尾部最近几轮，保留 Elm 原文
- **retrieval 区**：检索到相关历史，替换为摘要
- **fallback 区**：未检索到，截断或丢弃
- `_is_valid_summary` fail-close：摘要无效时不替换

旧决策节点：`C-011`, `C-011a`

## 影响

✅ 分级处理避免了 context length 超限
❌ v5.5 被 topic-aware 角色队列匹配取代
