---
title: TopicGradeManager 话题定级
slug: topic-grade-manager
category: decision
date: "2026-06"
version_introduced: v5.5
affects: [topic-management]
status: 已实装
source_files: ["topic_manager.py"]
---

## 触发条件
旧 `_compute_topic_groups` O(n²) 全量分割性能差，边界不稳定。需要增量话题检测。

## 选定
TopicGradeManager 在 pre_llm_call 中调用 detect(turn, ca_rows, user_msg)。3 种等级 ACT/REL/FAR。切换检测 via 相似度阈值。grade_on_switch 切换时打包旧话题 OV 提交。bg_review 跳过所有处理。
