---
title: AssemblyCache 单例设计
slug: assembly-cache-singleton
category: decision
date: "2026-05"
version_introduced: v2.0
alternatives: ["无缓存每轮重建（性能差）", "多实例缓存（一致性差）"]
chosen: "单例 AssemblyCache + 冷却/索引/快照/安全副本/防竞态"
affects: []
status: 已实装
source_files: []  # 已被增量缓存取代，历史参考
---

## 触发条件

A-stage 需要缓存组装结果避免每轮全量重建。

## 选定

- 单例 AssemblyCache
- 冷却机制：缓存过期策略
- 索引：快速定位缓存项
- 快照：崩溃恢复
- 安全副本：读写隔离
- 防竞态：锁保护

旧决策节点：`D-001`, `D-002`, `D-003`, `D-004`, `D-005`, `D-006`

## 影响

✅ 缓存命中避免重复计算
❌ v5.8 已被 __A_stable_cache 增量缓存取代
