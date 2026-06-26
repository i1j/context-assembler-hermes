---
title: 存储模型
slug: storage-model
category: architecture
version_introduced: v5.0
status: 已实装
decisions: ["e-stage-write-on-receive", "stage-terminology-unification", "schema-v5-rewrite"]
depends_on: []
updated: 2026-06-23
source_files: ["ca/store.py"]
---

## 问题

旧的 `turn_cache` 表携带 v4 遗留的四复合主键 `(session_id, turn_index, api_call_count, seq_index)`，以及 `conv_encoding` blob 列用于存储自定义编码的上下文数据。这两个设计导致：

- 四键主键复杂度过高，写操作易出错
- `conv_encoding` 序列化/反序列化性能差，且编码有损
- `elp`/`l0`/`l1` 摘要列命名与统一术语脱节

## 决策

### 备选方案

1. **保留 turn_cache 四主键 + conv_encoding** — 向后兼容，但复杂度不降反增
2. **新建 turn_stream 表，走 (session_id, turn, seq) 三主键** — 简化主键、无编码层、术语统一
3. **原地修改 turn_cache 加列** — 字段冗余无法清理

### 选定方案

新建 `turn_stream` 表，核心设计：

```sql
CREATE TABLE IF NOT EXISTS turn_stream (
    session_id   TEXT    NOT NULL,
    turn         INTEGER NOT NULL,
    seq          INTEGER NOT NULL,

    -- 原始数据核
    role          TEXT    NOT NULL,
    Elm       TEXT    NOT NULL DEFAULT '',

    -- tool 行专用
    tool_name     TEXT,
    tool_call_id  TEXT,
    args_json     TEXT,
    status        TEXT,
    duration_ms   INTEGER,

    -- thought / assistant 行专用
    tool_calls_json TEXT,
    finish_reason  TEXT,
    usage_prompt_tokens     INTEGER,
    usage_completion_tokens INTEGER,

    -- 标记
    biz_category  TEXT,
    written_at    REAL,

    -- 摘要（F-stage 写入）
    Fct       TEXT,
    Hdl       TEXT,

    PRIMARY KEY (session_id, turn, seq)
);
```

### 实现要点

- `turn` = 对话轮序号（从 1 递增）
- `seq` = 轮内序号：0=user, 1=assistant thought, 2..n=tool 行, N=fin（finish_reason='stop' 的最终 assistant）
- `Fct` 列由 E-stage 写代码级摘要，F-stage 异步覆盖为 LLM 版真摘要（非 Elm 原文）
- `Hdl` 列从 Fct 的 `core_change` 提取（首行摘要），非跨轮历元
- 旧 `turn_cache` 表已于 v5.10 清理，不再写入双表

详见 `ca/store.py` 的 `turn_stream` 表 schema（`_SCHEMA_SQL_V50`）。

## 数据验证

```sql
-- 验证 turn_stream 表结构
PRAGMA table_info(turn_stream);

-- 统计各类行分布
SELECT role, seq, COUNT(*) FROM turn_stream GROUP BY role, seq;

-- 验证 Fct/Hdl 覆盖率
SELECT SUM(CASE WHEN Fct != '' THEN 1 ELSE 0 END) AS has_fct,
       SUM(CASE WHEN Hdl != '' THEN 1 ELSE 0 END) AS has_hdl
FROM turn_stream;
```

## 优点

- 主键简化：`(session_id, turn, seq)` 多会话安全
- 无编码层：内容直接存明文字段，可读可查
- E-stage 写即落盘，无 buffer，写入路径最短
- 术语统一：Elm/Fct/Hdl 贯穿代码和 DB

## 约束 / 已知问题

- `content` 列可能包含冗长 LLM 输出，需配合 truncation
- 旧 `turn_cache` 表已于 v5.10 彻底删除，无迁移负担
