---
title: E-stage 写入协议
slug: e-stage-write-protocol
category: architecture
version_introduced: v5.0
status: 已实装
decisions: ["e-stage-write-on-receive", "schema-v5-rewrite"]
depends_on: ["storage-model"]
updated: 2026-06-18
---

## 问题

Hermes 对话系统的 Hook 生命周期包含 `pre_llm_call`、`post_api_request`、`pre_tool_call`、`post_tool_call`、`post_llm_call` 5 个阶段。CA 插件需要确定：**哪个 Hook 负责写入哪一行、哪些字段？** 错误的分配会导致字段缺失或数据覆盖。

## 决策

### 备选方案

1. **on_session_end 批量写** — 延迟可见性，无法在运行中恢复会话
2. **ToolBuffer 缓冲写入** — 先写入 buffer 再 flush，增加复杂度
3. **4 Hook 各写其责（选定）** — 每个 Hook 负责自身阶段的字段，写即落盘

### 选定方案

每个 Hook 写入特定的 `(turn, seq)` 行和字段：

| Hook | 写入行 | 写入字段 | 时机 |
|------|--------|----------|------|
| `pre_llm_call` | `(turn=n, seq=0)` | `role='user'`, `content`, `Elm` | LLM 调用前，写 user 消息 |
| `post_api_request` | `(turn=n, seq=0)` | `role='assistant'`, `content`, `tool_calls_json`, `finish_reason` | API 返回后，写 assistant 首行 |
| `pre_tool_call` | `(turn=n, seq=m)` | `tool_call_id`, `tool_name` | 工具调用前，写元信息 |
| `post_tool_call` | `(turn=n, seq=m)` | `role='tool'`, `content` | 工具执行后，写入结果 |
| `post_llm_call` | `(turn=n, seq=999999)` | `role='assistant'`, `content`, `finish_reason='stop'` | 多轮对话结束 |
| `_f_stage_daemon` | 已有行 | `Fct`, `Hdl` | 异步线程 |

### 实现要点

- **无回滚**：一行写入即不可撤销
- E-stage 写入在 `ca/store.py` 的 `write_turn_stream()` 方法中完成
- `pre_llm_call` 写入 user 行的同时，还负责注入 A-stage 装配的上下文文本
- `post_api_request` 分为两种场景：单轮（`finish_reason='stop'` 时合并写 `seq=0`）和多轮的临时行

## 数据验证

```sql
-- 验证每个 Hook 对应的行类型完整
SELECT role, seq, COUNT(*),
       SUM(CASE WHEN content IS NOT NULL AND content != '' THEN 1 ELSE 0 END) AS has_content,
       SUM(CASE WHEN tool_calls_json IS NOT NULL THEN 1 ELSE 0 END) AS has_tool_calls
FROM turn_stream
GROUP BY role, seq;
```

## 优点

- 写即落盘：崩溃不丢失已写入数据
- 每个 Hook 职责明确，不重复不遗漏
- 无需 buffer/queue，路径最短

## 约束 / 已知问题

- 多轮对话中 `post_api_request` 临时行在 final assistant 行写入后不会被清理（可通过 seq=999999 识别）
- `pre_llm_call` 注入 A-stage 上下文时，如果上下文过长可能触发 truncation
