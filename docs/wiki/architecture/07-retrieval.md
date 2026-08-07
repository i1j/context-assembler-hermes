---
title: 双路检索 + RRF
slug: retrieval
category: architecture
version_introduced: v4.0
status: 死代码（未接入主链路）
decisions: [dual-retrieval, embedding-multi-backend]
depends_on: [store, embedding]
updated: 2026-07-26
source_files: ["ca/retrieval.py"]
---

> ⚠️ **死代码（无生产调用方）**：A-stage 实际直接读 turn_stream（`read_turn_stream_all` 全量重建），
> 从不触碰 `self.cache`；`retrieval.py` 无生产调用方、`get_bm25_snapshot` 无消费方——
> BM25/向量双路检索未接入主链路，本页仅供历史参考。

## 问题

（历史设计，未接入）A-stage 曾设想从历史对话中找到与当前用户输入最相关的上下文；
单一检索方式（纯 BM25 或纯向量）在复杂场景下覆盖不全。方向 B 后 A-stage 从 turn_stream
直接读全量重建，不再经过检索子系统。

## 设计方案

- **BM25 检索**：自实现 BM25Okapi（零外部依赖），关键词精确匹配
- **向量检索**：余弦相似度（numpy-free 自实现），语义匹配
- **RRF 融合**：双路得分排序融合，消除单路偏差
- **动态候选分配**：BM25_vs_vector 候选数灵活分配
- **向量降级**：嵌入不可用时自动退回纯 BM25
- **检索范围**：仅搜索当前 session 的 turn_stream（跨 session 由 OV 处理）
