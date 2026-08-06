---
title: 嵌入服务
slug: embedding
category: architecture
version_introduced: v4.0
status: 已实装
decisions: [embedding-multi-backend, dual-retrieval]
depends_on: []
updated: 2026-07-26
source_files: ["ca/embedding.py"]
---

## 问题

向量检索需要嵌入服务将文本转为向量。需要支持不同后端，且在没有 GPU 的环境下能正常工作。

## 双后端

| 后端 | 依赖 | 适用场景 |
|------|------|----------|
| **Ollama** | `ollama` 服务 | 本地部署，GPU 加速 |
| **sentence-transformers** | `sentence-transformers` | 无 Ollama 环境，纯 CPU |

- **CachedEmbeddingClient**：缓存层包装，避免重复嵌入相同文本
- **余弦相似度自实现**：numpy-free，零外部依赖
- **sqlite-vec 已弃用**（v4.2）：外部依赖二进制兼容性问题
- **全量嵌入**：Hdl+Fct 均为 4096 维 BLOB 存储
