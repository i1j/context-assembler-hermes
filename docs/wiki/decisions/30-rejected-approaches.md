---
title: 已拒绝方案汇总
slug: rejected-approaches
category: decision
date: "2026-06-18"
version_introduced: v5.0~v5.8
alternatives: []
chosen: "16 个已拒绝方案归档"
affects: ["14-rejected-approaches"]
status: 已拒绝
---

## 触发条件

CA 开发过程中评估并拒绝了 16 个技术方案。统一归档以避免未来重复评估。

## 方案列表

| 方案 | 版本 | 拒绝理由 |
|------|------|----------|
| conv_encoding | v5.0 | 性能差、有损、不透明 |
| turn_cache 四主键 | v5.0 | 复杂度过高 |
| ToolBuffer | v5.0 | 增加复杂度 |
| 水位压力分割 | v5.7 | 边界不稳定，已删除 |
| _compute_topic_groups | v5.7 | O(n²) 性能差 |
| 全量每轮重分割 | v5.5 | O(n²) |
| sqlite-vec | v4.x | 外部依赖兼容性 |
| BM25 检索 | v5.0 | 不再需要 |
| 每轮全量重建缓存 | v5.8 | 性能差 |
| 同步 LLM 摘要 | v5.2 | 阻塞用户路径 |
| bg_review LLM 摘要 | v5.5 | 浪费时间 |
| CE pass-through | v5.0 | 空壳占用资源 |
| 惰性缓存 | v5.8 | 数据不一致 |
| L2/L1/L0 保留 | v5.5 | 用户反复纠正 |
| 固定半径 grade | v5.5 | 不灵活 |
| 时间距离定级 | v5.5 | 不反映语义 |
