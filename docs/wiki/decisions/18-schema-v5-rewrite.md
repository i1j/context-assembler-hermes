---
title: Schema v5 重构（turn_stream 主键重设计）
slug: schema-v5-rewrite
category: decision
date: "2026-06-12"
version_introduced: v5.0
alternatives: ["保留 turn_cache 四主键（复杂度不降）", "原地加列（无清理）"]
chosen: "新 turn_stream 表 + (turn, seq) PK + 3 钩子采集 + 惰性迁移"
affects: ["01-storage-model", "02-e-stage-write-protocol"]
status: 已实装
---

## 触发条件

turn_cache 的四复合主键 `(session_id, turn_index, api_call_count, seq_index)` 复杂度太高，conv_encoding 有损且性能差。

## 备选方案

1. **保留 turn_cache**：复杂度不降反增
2. **新表 turn_stream（选定）**: (turn, seq) 双主键，主键简化
3. **原地修改**：字段冗余无法清理

## 选定

- 新表 `turn_stream`，主键 `(turn, seq)`
- 3 钩子采集：`pre_llm_call`, `post_api_request`, `post_tool_call`
- 惰性迁移：旧数据逐步搬运，不阻塞
- `query_embedding` 列保留用于外部检索

旧决策节点：`SC-001`, `SC-002`, `SC-003`, `SC-004`
