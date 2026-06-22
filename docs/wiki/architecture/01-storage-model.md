---
title: 存储模型
slug: storage-model
category: architecture
version_introduced: v5.0
status: 已实装
decisions: ["e-stage-write-on-receive", "stage-terminology-unification", "schema-v5-rewrite"]
depends_on: []
updated: 2026-06-18
---

## 问题

旧的 `turn_cache` 表携带 v4 遗留的四复合主键 `(session_id, turn_index, api_call_count, seq_index)`，以及 `conv_encoding` blob 列用于存储自定义编码的上下文数据。这两个设计导致：

- 四键主键复杂度过高，写操作易出错
- `conv_encoding` 序列化/反序列化性能差，且编码有损
- `elp`/`l0`/`l1` 摘要列命名与统一术语脱节

## 决策

### 备选方案

1. **保留 turn_cache 四主键 + conv_encoding** — 向后兼容，但复杂度不降反增
2. **新建 turn_stream 表，走 (turn, seq) 双主键，三列存储 Elm/Fct/Hdl** — 简化主键、无编码层、术语统一
3. **原地修改 turn_cache 加列** — 字段冗余无法清理

### 选定方案

新建 `turn_stream` 表，核心设计：

```sql
CREATE TABLE IF NOT EXISTS turn_stream (
    turn    INTEGER NOT NULL,   -- 对话轮序号
    seq     INTEGER NOT NULL,   -- 轮内序号：0=user, 1..n=tool, 999999=final assistant
    role    TEXT NOT NULL,
    content TEXT,
    tool_call_id   TEXT,
    tool_name      TEXT,
    tool_calls_json TEXT,
    finish_reason  TEXT,
    Elm     TEXT NOT NULL DEFAULT '',  -- 原始数据
    Fct     TEXT NOT NULL DEFAULT '',  -- 单轮摘要
    Hdl     TEXT NOT NULL DEFAULT '',  -- 历元摘要
    _assemble_status INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (turn, seq)
);
```

### 实现要点

- `turn` = 对话轮序号（从 1 递增）
- `seq` = 轮内序号：0=user, 1=assistant 首行, 2..n=tool 行, 999999=finish_reason='stop' 的最终 assistant
- `Elm` 列存原始 LLM request/response 文本
- `Fct` 列由 F-stage 异步写入（单轮摘要）
- `Hdl` 列由 F-stage 异步写入（跨轮历元摘要）
- `_assemble_status` 列追踪 A-stage 装配状态

详见 `ca/store.py` 的 `turn_stream` 表 schema。

## 数据验证

```sql
-- 验证 turn_stream 表结构
PRAGMA table_info(turn_stream);

-- 统计各类行分布
SELECT role, seq, COUNT(*) FROM turn_stream GROUP BY role, seq;

-- 验证 Elm/Fct 覆盖率
SELECT SUM(CASE WHEN Elm != '' THEN 1 ELSE 0 END) AS has_elm,
       SUM(CASE WHEN Fct != '' THEN 1 ELSE 0 END) AS has_fct
FROM turn_stream;
```

## 优点

- 主键简化：`(turn, seq)` 清晰无歧义
- 无编码层：Elm/Fct/Hdl 直接存明文字段，可读可查
- E-stage 写即落盘，无 buffer，写入路径最短
- 术语统一：Elm/Fct/Hdl 贯穿代码和 DB

## 约束 / 已知问题

- `Elm` 列可能包含冗长 LLM 输出，需配合 truncation
- 旧 `turn_cache` 表保留至迁移完成，写入双表有额外开销
- v4 遗留列（`turn_type`, `tool_sub_index`, `elm_text`）保留至 PR2
