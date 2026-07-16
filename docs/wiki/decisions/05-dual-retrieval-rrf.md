---
title: 双路检索 + RRF 排序
slug: dual-retrieval-rrf
category: decision
date: "2026-05"
version_introduced: v2.0
alternatives: ["仅 BM25（召回不足）", "仅向量（冷启动问题）"]
chosen: "BM25 + 向量双路 + RRF(K=60) 融合"
affects: []
status: 已实装（v5.x 后逐渐降级）
source_files: []  # 已从 CA 移除，历史参考
---

## 触发条件

A-stage 需要从历史中检索与当前轮相关的上下文。

## 备选方案

1. **仅 BM25**：召回不足，无法理解语义
2. **仅向量检索**：冷启动时无向量可用
3. **双路 + RRF（选定）**：

## 选定

- BM25（零依赖自实现，Okapi BM25，k1=1.5, b=0.75）
- 向量检索（嵌入服务多后端）
- RRF 排序（K=60）融合双路结果
- 动态分配（根据历史轮数和模型 context length 自适应）
- 嵌入服务降级（LLM 不可用时→退化 BM25 only）

旧决策节点：`C-014`, `C-014a`, `C-015`, `C-016`, `RE-001`, `RE-002`

## 影响

✅ 双路互补：BM25 精确匹配 + 向量语义理解
✅ RRF 无需训练
❌ v5.0 后不再需要 BM25 索引
