---
title: 嵌入多后端选择
slug: embedding-multi-backend
category: decision
date: "2026-05"
version_introduced: v4.0
affects: [embedding]
status: 已实装
source_files: ["ca/embedding.py"]
---

## 触发条件
向量检索需要嵌入服务。Ollama 在 GPU 场景最优，无 GPU 时需要 CPU 备选。

## 选定
Ollama / sentence-transformers 双后端。自定义余弦相似度（numpy-free）。sqlite-vec 弃用（二进制兼容性）。4096 维 BLOB 存储。
