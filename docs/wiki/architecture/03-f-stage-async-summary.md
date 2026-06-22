---
title: F-stage 异步摘要
slug: f-stage-async-summary
category: architecture
version_introduced: v5.2
status: 已实装（v5.10 重构）（v5.10 重构）
decisions: ["l-stage-daemon", "fct-changes-format"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-07-26
---

## 问题

E-stage 写入的 Elm（原始数据）需要被摘要化为 Fct（单轮摘要）和 Hdl（历元摘要）。在何处、何时、由谁执行摘要生成，才能最大化性能并避免阻塞用户路径？

## 决策

### 备选方案

1. **同步摘要** — pre_llm_call 同步调用 LLM 生成摘要，阻塞用户
2. **post_llm_call 同步写** — 每轮对话结束时同步调用 LLM
3. **独立 daemon 线程异步摘要（选定）** — 守护线程轮询新数据，后台生成

### 选定方案

F-stage 由 `post_llm_call` 回调触发，调用 `process_turn_f_stage` 启动异步线程执行 LLM 摘要：

```python
# 在 post_llm_call_v5 中触发
engine.process_turn_f_stage(turn)
```

### 实现要点

- **触发条件**：`post_llm_call_v5` 回调末尾调用 `engine.process_turn_f_stage(turn)`
- **异步线程**：`process_turn_f_stage` 在 daemon 线程中执行 `_run_f_stage`
- **线程防护**：`_pending_tasks[turn_index].is_alive()` 防止重复线程
- **Fct 写入**：LLM 响应解析后通过 `_update_fct_v5` 写回
- **Hdl 写入**：从 Fct dict 的 `core_change` 提取（`_extract_hdl`）
- **截断回退**：保留 partial[:500] 并标记 `_assemble_status=1`
- **LLM 全失败回退**：fallback Fct + `_assemble_status=1`
- **FctTruncatedException**：catch 后保留 partial text

### 实现要点

- **触发条件**：新 turn 的 assistant 行写完（识别方式：`seq=0, role='assistant'` 且 `finish_reason IN ('stop', 'tool_use')`）
- **守护线程生命周期**：在 `_on_session_start` 中启动，`_on_session_end` 中停止
- **Fct 写入**：每次生成后立即写回 `turn_stream.Fct` 列
- **Hdl 写入**：历元（epoch）结束时写入，不需要每轮都生成
- **LLM 调用降级**：LLM 调用失败时保留 Fct 为空，下次重试

### 生成内容

- **Fct（单轮摘要）**：基于当前轮 Elm 生成 `changes` 列表格式
- **Hdl（历元摘要）**：综合多轮 Fct 生成跨轮概述

详见 `ca/__init__.py` 的 `_f_stage_daemon` 和 `_process_f_stage`。

## 数据验证

```sql
-- 验证 Fct 覆盖率
SELECT turn, 
       CASE WHEN Fct != '' THEN '有Fct' ELSE '无Fct' END AS fct_status
FROM turn_stream 
WHERE role='assistant' AND seq=0
ORDER BY turn;
```

## 优点

- 异步非阻塞：用户路径无等待
- daemon 线程自动管理生命周期
- 失败重试机制保证最终一致性

## 约束 / 已知问题

- 异步延迟：新 turn 的摘要可能在下次 A-stage 前来不及生成
- `_assembly_skipped` 机制处理未生成摘要的 turn
- daemon 线程在 session 结束时可能尚有未完成的 LLM 调用
