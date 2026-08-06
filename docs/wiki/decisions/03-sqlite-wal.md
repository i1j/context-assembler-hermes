---
title: SQLite WAL 存储选择
slug: sqlite-wal
category: decision
date: "2026-05"
version_introduced: v4.2
affects: [store]
status: 已实装
source_files: ["ca/store.py"]
---

## 触发条件

需要嵌入式存储引擎支持本地化上下文管理。外部服务有网络延迟。

## 备选方案

1. **PostgreSQL + pgvector** — 网络延迟不可控，部署复杂
2. **sqlite-vec** — 外部依赖二进制兼容性问题
3. **SQLite WAL** — 零依赖，WAL 模式支持并发读

## 选定

SQLite WAL 模式。三主键 (session_id, turn, seq)，无 conv_encoding 编码层，Elm/Fct/Hdl 直接文本存储。

## 影响

✅ 零外部依赖，便携部署 | ❌ 无分布式能力
