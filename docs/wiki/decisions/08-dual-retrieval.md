---
title: 双路检索 + RRF 融合
slug: dual-retrieval
category: decision
date: "2026-05"
version_introduced: v4.0
affects: [retrieval]
status: 已实装
source_files: ["ca/retrieval.py"]
---

## 触发条件
单路检索（纯 BM25 或纯向量）覆盖不全。BM25 无法语义匹配，向量检索对精确关键词不敏感。

## 选定
BM25+向量双路+RRF 融合。自实现 BM25Okapi（零外部依赖）。动态候选分配。向量降级→纯 BM25。
