---
title: F-stage 异步摘要
slug: f-stage-async-summary
category: architecture
version_introduced: v5.2
status: 已实装（v5.10 重构）（v5.10 重构）
decisions: ["l-stage-daemon", "fct-changes-format"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-23
source_files: ["ca/f_stage.py"]
---

## 问题

E-stage 写入的 Elm（原始数据）需要被摘要化为 Fct（单轮摘要）和 Hdl（单句摘要）。在何处、何时、由谁执行摘要生成，才能最大化性能并避免阻塞用户路径？

## 决策

### 备选方案

1. **同步摘要** — pre_llm_call 同步调用 LLM 生成摘要，阻塞用户
2. **post_llm_call 同步写** — 每轮对话结束时同步调用 LLM
3. **独立 daemon 线程异步摘要（旧）** — 守护线程轮询新数据 — 已替换为触发模型
4. **post_llm_call 触发 + daemon 线程（当前）** — 回调触发，异步 LLM 执行

### 选定方案

F-stage 由 `post_llm_call` 回调末尾触发，`process_turn_f_stage` 路由决策后启动 daemon 线程执行 LLM 摘要：

```python
# 在 post_llm_call_v5 中触发（ca/__init__.py ContextAssembler.process_turn_f_stage）
engine.process_turn_f_stage(turn)
```

### 实现要点

- **触发条件**：`post_llm_call_v5` 回调末尾调用 `engine.process_turn_f_stage(turn)`
- **路由决策**（`process_turn_f_stage`）：
  1. 检查 fin 行是否已有 Fct → 有则跳过
  2. 检测 bg_review → 同步写代码级 Fct（无需 LLM），跳过
  3. 否则 → 启 daemon 线程执行 `_run_f_stage`
- **异步线程**：`_run_f_stage` 在 daemon 线程中执行（`ca/f_stage.py` `FStageMixin`）
- **线程防护**：`_pending_tasks[turn_index].is_alive()` 防止重复线程
- **Fct 写入**：LLM 响应解析后通过 `_update_fct_v5` 写回 fin 行
- **Hdl 写入**：从 Fct dict 的 `core_change` 提取（`_extract_hdl`），约 100 字符
- **截断回退**：保留 partial[:500] 并标记 `_assemble_status=1`
- **LLM 全失败回退**：fallback Fct + `_assemble_status=1`
- **FctTruncatedException**：catch 后保留 partial text

### 生成内容

- **Fct（单轮摘要）**：基于当前轮 Elm 生成 `changes` 列表格式（JSON）
- **Hdl（单句摘要）**：从 Fct 的 `core_change` 提取首句，约 100 字符
  - 不是跨轮历元摘要——仅从当前轮 Fct 提取

详见 `ca/f_stage.py` 的 `_run_f_stage` 和 `_call_llm_for_fct`。

## 数据验证

```sql
-- 验证 Fct 覆盖率（fin 行）
SELECT turn,
       CASE WHEN Fct != '' THEN '有Fct' ELSE '无Fct' END AS fct_status
FROM turn_stream
WHERE role='assistant' AND finish_reason='stop'
ORDER BY turn;
```

## 优点

- 异步非阻塞：用户路径无等待
- 回调触发：无需轮询，新数据立即处理
- 失败重试机制保证最终一致性

## 约束 / 已知问题

- 异步延迟：新 turn 的摘要可能在下次 A-stage 前来不及生成
- `_assemble_status=1` 标记未完成的摘要，A-stage 忽略该 turn 的 Fct
- session 结束时可能尚有未完成的 LLM 调用（`wait_for_pending` 保障等待）
