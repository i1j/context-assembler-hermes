---
title: 双路检索 + RRF 融合
slug: dual-retrieval
category: decision
date: "2026-05"
version_introduced: v4.0
affects: [retrieval]
status: 已实装 → 死代码（无生产调用方，2026-08-13 核对修订）
source_files: ["ca/retrieval.py"]
---

## 触发条件
单路检索（纯 BM25 或纯向量）覆盖不全。BM25 无法语义匹配，向量检索对精确关键词不敏感。

## 选定
BM25+向量双路+RRF 融合。自实现 BM25Okapi（零外部依赖）。动态候选分配。向量降级→纯 BM25。

## 修订（2026-08-13 代码核对）
**方案代码存在但未接入主链路**：`ca/retrieval.py` 无生产调用方（全仓 grep 仅
`cache.py:223` 定义 `get_bm25_snapshot` 且无消费方）。方向 B 后 A-stage 直接读
turn_stream 全量重建，不再走检索子系统。语义检索实际由 `ca/inject.py`
（`pick_injection_realities`，提问云形心 + 4B 拣选）承担，与本文档的
BM25/向量双路方案不同。本页供历史参考。
