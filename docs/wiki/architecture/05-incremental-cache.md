---
title: 增量缓存
slug: incremental-cache
category: architecture
version_introduced: v5.8
status: 已实装
decisions: ["incremental-cache", "topic-grade-manager"]
depends_on: ["a-stage-role-match", "tail-protection", "storage-model"]
updated: 2026-06-18
---

## 问题

A-stage 每轮都从头组装所有历史 turn 的上下文。当会话很长（数百轮）时，全量重组导致：
- 性能瓶颈：每轮对全部历史逐行判断 grade、提取 Fct
- 重复工作：大部分历史上下文在上次装配后没有变化

## 决策

### 备选方案

1. **全量每轮重建** — 简单但性能差，O(n) 每轮
2. **惰性缓存不过期** — 可能导致数据不一致
3. **纯 delta 无全量基线** — 无法处理话题切换
4. **delta + 全量双模 + Fct-pending 防护（选定）**

### 选定方案

使用 `_A_stable_cache` 缓存上次 A-stage 输出，分三种调度模式：

```
                 ┌──────────────┐
                 │ 新 turn 到达  │
                 └──────┬───────┘
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
      ┌──────────────┐    ┌──────────────┐
      │ 话题切换了吗？ │    │ 缓存 stal    │
      │              │    │ e?           │
      └──────┬───────┘    └──────┬───────┘
             │ YES                │ YES
             ▼                    ▼
     ┌──────────────┐    ┌──────────────┐
     │ 全量重建      │    │ 全量重建      │
     └──────┬───────┘    └──────┬───────┘
             │ NO                 │ NO
             ▼                    ▼
     ┌────────────────────────────────┐
     │ 增量替换：只拼接新 turn 的 Fct  │
     └────────────────────────────────┘
```

**三个实例变量**：
- `_A_stable_cache: str` — 上次 A-stage 输出的完整上下文文本
- `_A_cache_turns: int` — 缓存覆盖的 turn 数（最后覆盖的 turn 序号）
- `_A_cache_is_stale: bool` — 缓存是否失效

**增量流程**（5 步）：
1. 判断缓存有效性（话题切换 → stale, stale → stale, 有效 → 有效）
2. 有效时：从 `_A_stable_cache` 截取定位点后的尾部
3. 只对新 turn（`_A_cache_turns` 以后）逐行执行 grade 判定 + Fct 替换
4. 将新 turn 的装配结果追加到截取的缓存尾部
5. 更新 `_A_stable_cache` 和 `_A_cache_turns`

**Fct-pending 防护**：如果增量范围内的某个 turn 的 Fct 尚未生成（`_assembly_skipped`），跳过该 turn 保留 Elm。

**回退条件**：
| 条件 | 行为 |
|------|------|
| 话题切换 | 全量重建 |
| `_A_cache_is_stale=True` | 全量重建 |
| 无增量空间（历史太长）+ 缓存命中 | 增量 |
| Fct pending 过多 | 增量（保留 pending turn 的 Elm） |

## 数据验证

```python
# 观察缓存命中 vs 全量重建
# 在日志中搜索 "A-stage cache" 或 "incremental"
grep "incremental\|full rebuild\|cache hit" ca_assembler.log | tail -20
```

## 优点

- 减少重複 LLM Fct 提取和 grade 判定
- 话题切换时自动全量重建保证一致性
- Fct-pending 防护避免未生成摘要的 turn 丢失

## 约束 / 已知问题

- 缓存仅覆盖 A-stage 输出文本，不缓存 grade 判定结果
- 极端长会话（1000+ turn）仍可能因增量空间不足退化
- 当前未持久化到磁盘，session 重启丢失缓存
