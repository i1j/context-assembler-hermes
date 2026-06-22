---
title: 增量缓存取代全量串联
slug: incremental-cache
category: decision
date: "2026-06-17"
version_introduced: v5.8
alternatives: ["全量每轮重建（O(n) 每轮）", "惰性缓存不过期（数据不一致）", "纯 delta 无基线（无话题感知）"]
chosen: "delta + 全量双模 + Fct-pending 防护"
affects: ["05-incremental-cache"]
status: 已实装
---

## 触发条件

A-stage 每轮对全部历史逐行判断 grade、提取 Fct，当对话很长（100+ turn）时导致性能瓶颈。

## 备选方案

1. **全量每轮重建**：O(n) 每轮，n=100 时每轮 100 次 grade 判断 → 不可接受
2. **惰性缓存不过期**：话题切换后缓存指向错误上下文 → 数据不一致
3. **增量 + 全量双模 + Fct-pending 防护（选定）**

## 选定

- `_A_stable_cache`：缓存完整上下文文本
- `_A_cache_turns`：覆盖的 turn 数
- `_A_cache_is_stale`：失效标记
- **调度**：话题切换→全量，stale→全量，有效→增量
- **增量 5 步流程**：截取定位点 → 新 turn 逐行 grade → 提取 Fct → 拼接 → 更新缓存
- **Fct-pending 防护**：Fct 未生成的 turn 跳过（保留 Elm）

## 之前 vs 之后

**之前**：每轮全量遍历全部历史
**之后**：90% 场景增量追加，仅话题切换/失效时全量
