---
source_files: ["topic_manager.py"ca/grade.py"]
title: TopicGradeManager 取代旧话题系统
slug: topic-grade-manager
category: decision
date: "2026-06-16"
version_introduced: v5.5
alternatives: ["保留旧 TP 系统（v4.6 话题拣选，不可维护）", "完全消除话题分割（退化为全局上下文）"]
chosen: "TopicGradeManager 增量分割 + 形心半径定级 + Grade 枚举"
affects: ["08-topic-segmentation", "09-topic-grade-switch", "04-a-stage-role-match"]
status: 已实装
---

## 触发条件

旧话题系统（TP-001~TP-005）建立在 v4.x 三区模型上，无法直接对接 v5.x 的 topic-aware grade 替换。

## 备选方案

1. **保留旧 TP**：维护两套话题系统 → 不可维护
2. **完全消除**：退化为全局上下文 → 信息过载
3. **TopicGradeManager（选定）**：增量分割 + 形心定级 + Grade 枚举

## 选定

```python
class TopicGradeManager:
    detect(turn, ca_rows, user_msg)  → 检测话题是否切换
    grade_on_switch(q_emb, user_msg) → 按形心半径定级 ACT/REL/FAR
    get_turn_grade(turn_num)         → 返回 TopicGrade 枚举
    reset()                          → 清空所有状态
```

- Grade 枚举：`Grade.ELM="Elm", Grade.FCT="Fct", Grade.HDL="Hdl"`（字符串值）
- TopicGrade 枚举：`TopicGrade.ACT="Act", TopicGrade.REL="Rel", TopicGrade.FAR="Far"`
- 旧 `_compute_topic_groups`/`_jaccard_tokens`/`_is_bg_turn` 物理删除（v5.7）
- 话题配置：`CA_TOPIC_JACCARD_ENTRY`（0.02）、`CA_TOPIC_JACCARD_CHAIN`（0.04）、`CA_TOPIC_RADIUS_WEIGHT`（2.0）

## 之前 vs 之后

**之前**：旧 TP（全量分割 + 三级定级 + TopicRetriever + topic_boost），O(n²) 复杂度
**之后**：TopicGradeManager 增量 + 形心定级，O(1) 每轮
