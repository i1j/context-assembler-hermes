---
title: F-stage 异步摘要
slug: f-stage-async-summary
category: architecture
version_introduced: v5.2
status: 已实装
decisions: ["l-stage-daemon", "fct-changes-format"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-18
---

## 问题

E-stage 写入的 Elm（原始数据）需要被摘要化为 Fct（单轮摘要）和 Hdl（历元摘要）。在何处、何时、由谁执行摘要生成，才能最大化性能并避免阻塞用户路径？

## 决策

### 备选方案

1. **同步摘要** — pre_llm_call 同步调用 LLM 生成摘要，阻塞用户
2. **post_llm_call 同步写** — 每轮对话结束时同步调用 LLM
3. **独立 daemon 线程异步摘要（选定）** — 守护线程轮询新数据，后台生成

### 选定方案

F-stage 在独立的 daemon 线程中运行，异步读取 Elm 生成 Fct/Hdl：

```python
def _f_stage_daemon(self):
    while not self._stop_event.is_set():
        turns = self._fetch_ungenerated_turns()
        for turn_data in turns:
            fct = self._call_llm_for_fct(turn_data)
            hdl = self._call_llm_for_hdl(turn_data)
            self.store.write_fct_hdl(turn_data.turn, fct=fct, hdl=hdl)
        time.sleep(0.5)
```

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
