---
title: TopicGradeManager 取代旧话题系统
slug: topic-grade-manager
category: decision
date: "2026-06-16"
version_introduced: v5.5
alternatives: ["保留旧 TP 系统（v4.6 话题拣选，不可维护）", "完全消除话题分割（退化为全局上下文）"]
chosen: "TopicGradeManager（495行）增量分割 + 形心半径定级 + Grade 枚举"
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
    _segment_topics(turn)  → 检测话题边界
    get_turn_grade(turn)   → 按形心半径定级 ELM(≤25) / FCT(≤100) / HDL(>100)
    reset()                → 话题切换时重置
```

- Grade 枚举：`Grade.ELM=2, Grade.FCT=1, Grade.HDL=0`
- 旧 `_compute_topic_groups`/`_jaccard_tokens`/`_is_bg_turn`/`_scan_forced_split_phrases` 物理删除（v5.7）
- 话题配置：`CA_TOPIC_SEGMENT_ENABLED`, `CA_TOPIC_SIMILARITY_THRESHOLD`

## 之前 vs 之后

**之前**：旧 TP（全量分割 + 三级定级 + TopicRetriever + topic_boost），O(n²) 复杂度
**之后**：TopicGradeManager 增量 + 形心定级，O(1) 每轮
