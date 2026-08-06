---
title: 已拒绝方案汇总
slug: rejected-approaches
category: decision
date: "2026-07"
version_introduced: v0-v6
affects: [—]
status: 已拒绝
source_files: []
---

## 触发条件
CA 开发过程中评估并拒绝了多个技术方案。统一记录避免未来重复评估。

## 已拒绝方案

| 方案 | 评估版本 | 拒绝理由 |
|------|----------|----------|
| conv_encoding（Jaccard+三级替换+blob） | v5.0 | 性能差、有损、不透明 |
| turn_cache 四主键 | v5.0 | 主键过于复杂 |
| ToolBuffer 缓冲写入 | v5.0 | 增加不必要复杂度 |
| sqlite-vec 向量检索 | v4.x | 外部依赖二进制兼容性问题 |
| 水位压力话题分割 | v5.7 | 边界不稳定，已物理删除 |
| _compute_topic_groups 全量分割 | v5.7 | O(n²) 性能差 |
| PostgreSQL + pgvector | v4.2 | 网络延迟不可控 |
