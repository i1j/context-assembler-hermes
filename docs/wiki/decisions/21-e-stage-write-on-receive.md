---
title: E-stage 写即落盘
slug: e-stage-write-on-receive
category: decision
date: "2026-06-14"
version_introduced: v5.0
alternatives: ["ToolBuffer 缓冲写入（已拒绝）", "on_session_end 批量写（延迟可见性）"]
chosen: "turn_stream (turn, seq) PK，写即落盘，无 buffer"
affects: ["01-storage-model", "02-e-stage-write-protocol"]
status: 已实装
source_files: ["ca/store.py", "ca/e_stage.py"]
---

## 触发条件

E-stage 需要确定写入策略——是否缓冲、何时落盘。

## 备选方案

1. **ToolBuffer**：先写 buffer，flush 时再落盘 → 增加复杂度，崩溃丢失
2. **on_session_end 批量写**：整个会话的数据延迟落盘 → 不可恢复
3. **写即落盘（选定）**：每个 Hook 获取数据后立即写入 turn_stream

## 选定

- `turn_stream` 表 `(turn, seq)` PK
- 每行独立写入，无跨行事务
- 无 conv_encoding、无转换层
- 可用元数据应存尽存

## 之前 vs 之后

**之前**：conv_encoding blob + turn_cache 四主键，写入路径含两层编码
**之后**：turn_stream 明文三列，写入路径：数据 → DB
