---
title: AssemblyCache
slug: cache
category: architecture
version_introduced: v4.0
status: 部分接入（v7.1 BUG-08：semantic_fct_embeddings 由 TopicGradeManager 消费）
decisions: [assembly-cache, fingerprint-dedup]
depends_on: [store]
updated: 2026-08-13
source_files: ["ca/cache.py"]
---

> 修订（2026-08-13 代码核对）：原文档标"死代码（未接入主链路）"不准确——`__init__.py:475`
> 把 `engine.cache`（AssemblyCache 实例，`ca/__init__.py:172-173` 由 CacheBuilder 构建）传给
> `TopicGradeManager`，后者在话题切换时读取 `self._cache.semantic_fct_embeddings`
> （`topic_manager.py:628`；`cache.py:133/161` 定义并填充）复用 Fct 语义 embedding，避免重算。
> **实际状态：部分接入**——`semantic_fct_embeddings` 活，四字典其余（Hdls/Fcts/hdl_embeddings/
> fct_embeddings）无生产调用方（A-stage 从 turn_stream 直接读全量，不触碰它们）。

## 问题

（历史设计）曾设想每次 A-stage 装配需要频繁读取 Fct/Hdl 数据，引入内存缓存层
减少 DB 查询次数和嵌入计算；方向 B 后 A-stage 每次从 DB 全量重建，四字典缓存无生产调用方。
v7.1（BUG-08）起 `semantic_fct_embeddings` 被 TopicGradeManager 复用，是本类当前唯一活跃用途。

## 四字典缓存（v6.0 后）

| 缓存 | Key | 说明 | 生产状态 |
|------|-----|------|---------|
| `Hdls` | `int[turn]` | 对话轮 Hdl 文本 | 🧊 无调用方 |
| `Fcts` | `int[turn]` | 对话轮 Fct JSON | 🧊 无调用方 |
| `hdl_embeddings` | `int[turn]` | 对话轮 Hdl 嵌入向量 | 🧊 无调用方 |
| `fct_embeddings` | `int[turn]` | 对话轮 Fct 嵌入向量 | 🧊 无调用方 |
| `semantic_fct_embeddings` | `int[turn]` | 归一化 Fct 语义文本的嵌入向量（v7.1 BUG-08） | ✅ TopicGradeManager 消费 |

> 工具轮缓存（tool_Hdls/tool_Fcts/tool_group_*）已随方向 B 移除（v6.0），
> 工具行不再进入 AssemblyCache。

## 关键约束

- **冷启动 warmup**：首次访问时从 DB 批量加载历史数据
- **嵌入去重（实际实现）**：`semantic_fct_embeddings` 以 turn 为 key 的 dict 天然去重
  （同 turn 不重复嵌入）；**无 SHA256 指纹实现**（原文档"SHA256 指纹"为误述，
  代码 `ca/cache.py` 无任何 sha256/fingerprint 逻辑）
- **v6.0 增量缓存废弃**：`_A_stable_cache`、`_A_cache_turns` 不再使用（方向 B 每次从 DB 全量重建，不再需要增量缓存）
