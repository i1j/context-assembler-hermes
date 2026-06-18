---
title: F-stage 异步摘要
slug: f-stage-async-summary
category: decision
date: "2026-06-14"
version_introduced: v5.2
alternatives: ["同步摘要（阻塞用户路径）", "pre_llm_call 内同步写（不可接受）"]
chosen: "daemon 线程异步读取 Elm → LLM 生成 → 写回 Fct/Hdl"
affects: ["03-f-stage-async-summary", "11-fct-format-evolution"]
status: 已实装
---

## 触发条件

Elm 写入后需要异步生成 Fct 和 Hdl，不阻塞用户路径。

## 备选方案

1. **同步摘要**：用户等待 LLM 生成摘要 → 不可接受
2. **post_llm_call 同步写**：多轮对话中在结束时同步写 → 阻塞最终响应
3. **daemon 线程异步（选定）**：独立线程轮询新 turn，后台生成

## 选定

- F-stage daemon 线程：`_f_stage_daemon` 在 session 启动时创建
- 触发条件：新 turn 的 assistant 行 `finish_reason IN ('stop', 'tool_use')`
- 写回 `turn_stream.Fct` 和 `turn_stream.Hdl`
- LLM 失败保留 Fct 为空，下次重试

## 之前 vs 之后

**之前**：L-stage 双线程，限速/失败降级/自动拆工具轮
**之后**：F-stage 单 daemon + 异步等待 + 失败保留
