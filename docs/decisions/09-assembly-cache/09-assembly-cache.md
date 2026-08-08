---
title: AssemblyCache 内存缓存层
slug: assembly-cache
category: decision
date: "2026-05"
version_introduced: v4.0
affects: [cache]
status: 已实装（增量缓存 v6.0 废弃）
source_files: ["ca/cache.py"]
---

## 触发条件
频繁读取 Fct/Hdl → DB 查询性能瓶颈。需要内存缓存层。

## 选定
六字典缓存（Hdls/Fcts/tool_Hdls/tool_Fcts/tool_group_Hdls/tool_group_Fcts）。冷启动 warmup。指纹去重避免重复嵌入。v6.0 方向 B 后增量缓存 _A_stable_cache 废弃（全量 DB 重建）。
