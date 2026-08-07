---
title: AssemblyCache
slug: cache
category: architecture
version_introduced: v4.0
status: 死代码（未接入主链路）
decisions: [assembly-cache, fingerprint-dedup]
depends_on: [store]
updated: 2026-07-26
source_files: ["ca/cache.py"]
---

> ⚠️ **死代码（无生产调用方）**：A-stage 从 turn_stream 直接读全量（`read_turn_stream_all`），
> 从不触碰 `self.cache`；AssemblyCache 未接入主链路，本页仅供历史参考。

## 问题

（历史设计，未接入）曾设想每次 A-stage 装配需要频繁读取 Fct/Hdl 数据，引入内存缓存层
减少 DB 查询次数和嵌入计算；方向 B 后 A-stage 每次从 DB 全量重建，缓存层无生产调用方。

## 四字典缓存（v6.0 后）

| 缓存 | Key | 说明 |
|------|-----|------|
| `Hdls` | `int[turn]` | 对话轮 Hdl 文本 |
| `Fcts` | `int[turn]` | 对话轮 Fct JSON |
| `hdl_embeddings` | `int[turn]` | 对话轮 Hdl 嵌入向量 |
| `fct_embeddings` | `int[turn]` | 对话轮 Fct 嵌入向量 |

> 工具轮缓存（tool_Hdls/tool_Fcts/tool_group_*）已随方向 B 移除（v6.0），
> 工具行不再进入 AssemblyCache。

## 关键约束

- **冷启动 warmup**：首次访问时从 DB 批量加载历史数据
- **指纹去重**：SHA256 指纹避免重复嵌入相同文本
- **v6.0 增量缓存废弃**：`_A_stable_cache`、`_A_cache_turns` 不再使用（方向 B 每次从 DB 全量重建，不再需要增量缓存）
