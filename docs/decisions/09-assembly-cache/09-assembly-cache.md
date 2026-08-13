---
title: AssemblyCache 内存缓存层
slug: assembly-cache
category: decision
date: "2026-05"
version_introduced: v4.0
affects: [cache]
status: 已实装（增量缓存 v6.0 废弃）→ 部分接入（2026-08-13 核对修订）
source_files: ["ca/cache.py"]
---

## 触发条件
频繁读取 Fct/Hdl → DB 查询性能瓶颈。需要内存缓存层。

## 选定
六字典缓存（Hdls/Fcts/tool_Hdls/tool_Fcts/tool_group_Hdls/tool_group_Fcts）。冷启动 warmup。指纹去重避免重复嵌入。v6.0 方向 B 后增量缓存 _A_stable_cache 废弃（全量 DB 重建）。

## 修订（2026-08-13 代码核对）
**实际为部分接入**：四字典（Hdls/Fcts/hdl_embeddings/fct_embeddings）与 tool_* 缓存
无生产调用方（A-stage 从 turn_stream 直接读全量）；但 `semantic_fct_embeddings`
（`cache.py:133/161`）由 `TopicGradeManager` 消费（`topic_manager.py:628`，
经 `__init__.py:475` 传入）——话题切换时复用 Fct 语义 embedding，v7.1 BUG-08。
**"指纹去重"未落地**：`cache.py` 无任何 SHA256/fingerprint 实现，去重由 dict key 天然承担。
