---
title: 双路检索 + RRF
slug: retrieval
category: architecture
version_introduced: v4.0
status: 已实装
decisions: [dual-retrieval, embedding-multi-backend]
depends_on: [store, embedding]
updated: 2026-07-26
source_files: ["ca/retrieval.py"]
---

## 问题

A-stage 需要从历史对话中找到与当前用户输入最相关的上下文。单一检索方式（纯 BM25 或纯向量）在复杂场景下覆盖不全。

## 设计方案

- **BM25 检索**：自实现 BM25Okapi（零外部依赖），关键词精确匹配
- **向量检索**：余弦相似度（numpy-free 自实现），语义匹配
- **RRF 融合**：双路得分排序融合，消除单路偏差
- **动态候选分配**：BM25_vs_vector 候选数灵活分配
- **向量降级**：嵌入不可用时自动退回纯 BM25
- **检索范围**：仅搜索当前 session 的 turn_stream（跨 session 由 OV 处理）
