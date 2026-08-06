---
title: 存储模型
slug: store
category: architecture
version_introduced: v5.10
status: 已实装
decisions: [sqlite-wal, schema-v5, naming-unification]
depends_on: []
updated: 2026-07-26
source_files: ["ca/store.py"]
---

## 问题

旧 `turn_cache` 表使用四复合主键 `(session_id, turn_index, api_call_count, seq_index)` + `conv_encoding` blob 编码层，写操作易错、编码有损、查询慢。

## turn_stream 表（18 列）

```sql
-- PK (session_id, turn, seq)
session_id     TEXT    NOT NULL  -- 会话标识
turn           INTEGER NOT NULL  -- 每轮 user 消息数（1-based）
seq            INTEGER NOT NULL  -- 轮内序号（0=user, 1=thought, 2..=tool, N=fin）
role           TEXT    NOT NULL  -- user / assistant / tool
Elm            TEXT    DEFAULT ''-- 原始消息文本
tool_name      TEXT             -- 工具名（仅 tool 行）
tool_call_id   TEXT             -- 工具调用 ID
args_json      TEXT             -- 工具参数 JSON
status         TEXT             -- ok / error / blocked / pending
duration_ms    INTEGER          -- 工具执行耗时（ms）
tool_calls_json TEXT            -- thought 行的 tool_calls 原始 JSON
finish_reason  TEXT             -- stop / tool_calls / length
usage_prompt_tokens     INTEGER -- LLM prompt token 数
usage_completion_tokens INTEGER -- LLM completion token 数
biz_category   TEXT             -- 业务分类（bg_review 等）
written_at     REAL             -- time.time()
Fct            TEXT             -- 结构化摘要 JSON（F-stage 写入）
Hdl            TEXT             -- 一句话标题（F-stage 写入）
```

- WAL 模式（`PRAGMA journal_mode=WAL`）
- 无编码层 — Elm/Fct/Hdl 直接存储原始文本/JSON

## 关键约束

- 行不可变 — 写入即不可撤销
- 旧 turn_cache 表在 v5.10 迁移后移除
