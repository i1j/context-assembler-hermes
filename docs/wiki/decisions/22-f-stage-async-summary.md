---
title: F-stage 异步摘要（fin 粒度）
slug: f-stage-async-summary
category: decision
date: "2026-06-14"
version_introduced: v5.2
alternatives: ["同步摘要（阻塞用户路径）", "pre_llm_call 内同步写（不可接受）", "daemon 线程轮询（旧）"]
chosen: "post_llm_call 触发 + fin 粒度 daemon 线程"
affects: ["03-f-stage-async-summary", "11-fct-format-evolution"]
status: 已实装（v5.11 fin 粒度重构）
---

## 触发条件

Elm 写入后需要异步生成 Fct 和 Hdl，不阻塞用户路径。同一个 turn 可能因两次 LLM 调用产生多条 fin 行，每条 fin 行需要独立的增量摘要。

## 备选方案

1. **同步摘要**：用户等待 LLM 生成摘要 → 不可接受
2. **post_llm_call 同步写**：多轮对话中在结束时同步写 → 阻塞最终响应
3. **daemon 线程轮询（旧）**：独立线程轮询新 turn，后台生成 → 有延迟，且无法按 fin 粒度处理
4. **post_llm_call 触发 + fin 粒度 daemon 线程（选定）**：每条 fin 行写入后立即触发

## 选定

- **触发机制**：`post_llm_call_v5` 在写 fin 行后调用 `engine.process_turn_f_stage(turn, fin_seq=seq)`
- **fin 粒度**：每条 fin 行独立 daemon 线程，`_pending_tasks[(turn, fin_seq)]` 追踪
- **增量摘要**：`read_incremental_elm(store, session_id, turn, fin_seq)` 读取 user + 自上次 fin 以来的增量 Elm
- **写回**：通过 `update_fin_fct_v5(store, session_id, turn, seq, fct, hdl)` 写回指定 fin 行
- LLM 失败保留 Fct 为空，下次重试

## 之前 vs 之后

**之前**：turn 级别 F-stage，`update_fin_fct_v5` 用 `MAX(seq)` 自动定位 fin 行，双 fin 行下第一条 fin 的 Fct 永远为 None

**之后**：fin 粒度 F-stage，`update_fin_fct_v5` 接受 `seq` 参数写指定 fin 行，每条 fin 独立摘要
