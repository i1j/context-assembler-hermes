---
title: E-stage 写入协议
slug: e-stage-write-protocol
category: architecture
version_introduced: v5.0
status: 已实装
decisions: ["e-stage-write-on-receive", "schema-v5-rewrite"]
depends_on: ["storage-model"]
updated: 2026-06-23
source_files: ["ca/e_stage.py"]
---

## 问题

Hermes 对话系统的 Hook 生命周期包含 `pre_llm_call`、`post_api_request`、`pre_tool_call`、`post_tool_call`、`post_llm_call` 5 个阶段。CA 插件需要确定：**哪个 Hook 负责写入哪一行、哪些字段？** 错误的分配会导致字段缺失或数据覆盖。

## 决策

### 备选方案

1. **on_session_end 批量写** — 延迟可见性，无法在运行中恢复会话
2. **ToolBuffer 缓冲写入** — 先写入 buffer 再 flush，增加复杂度
3. **5 Hook 各写其责（选定）** — 每个 Hook 负责自身阶段的字段，写即落盘

### 选定方案

每个 Hook 写入特定的 `(turn, seq)` 行和字段（定义在 `ca/e_stage.py` 的 `EStageMixin` 中）：

| Hook | 写入行 | 写入字段 | 时机 |
|------|--------|----------|------|
| `pre_llm_call` | `(turn=n, seq=0)` | `role='user'`, `content` | LLM 调用前，写 user 消息 |
| `post_api_request` | `(turn=n, seq=1)` | `role='assistant'`, `content`(thought), `tool_calls_json`, `finish_reason`, thought 代码摘要 | API 返回后，写 thought 行 + tool 占位行 |
| `pre_tool_call` | **no-op** | — | 占位行已在 `post_api_request` 写入 |
| `post_tool_call` | `(turn=n, seq=m)` | `role='tool'`, `content`, per-tool Fct | 工具执行后，写入结果 |
| `post_llm_call` | `(turn=n, seq=max_seq+1)` | `role='assistant'`, `content`, `finish_reason='stop'` | LLM 最终回复，写 fin 行 |
| `process_turn_f_stage`(触发) | 已有 fin 行 | `Fct`, `Hdl` | post_llm_call 末尾触发异步线程 |

### 实现要点

- **无回滚**：一行写入即不可撤销
- E-stage 写入在 `ca/store.py` 的 `write_turn_v5()` 函数中完成
- `post_api_request` 纯文本回复（无 tool_calls）跳过写入；有 tool_calls 时写入 thought 行 (seq=1) + tool 占位行 (seq=2+)
- `post_api_request` 顺便计算 thought 代码摘要（无需 LLM）写入 Fct/Hdl
- `post_tool_call` 顺便计算 per-tool 代码摘要写入 Fct/Hdl
- `pre_tool_call` 是 no-op：占位行已在 `_on_api_response_v5` 中写入，`_tool_seq_map` 映射已建立
- F-stage LLM 摘要通过 `process_turn_f_stage` 在 `post_llm_call` 末尾触发

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

- `pre_llm_call` 注入 A-stage 上下文时，如果上下文过长可能触发 truncation
- 纯文本对话（无 tool_calls 的 assistant 回复）不走 `post_api_request` 写入——只在 `post_llm_call` 写 fin 行
