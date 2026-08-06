---
title: F-stage 异步摘要（fin 粒度）
slug: f-stage-async
category: decision
date: "2026-06"
version_introduced: v5.2
affects: [f-stage]
status: 已实装
source_files: ["ca/f_stage.py"]
---

## 触发条件

E-stage 写入的 Elm 需要异步摘要为 Fct+Hdl。同一 turn 可能有多条 fin（多次 LLM 调用）。

## 备选方案

1. **同步 LLM 于 pre_llm_call** — 阻塞用户
2. **post_llm_call 同步写** — 每轮结束时同步调 LLM
3. **守护线程轮询（旧）** — 取代为触发模型
4. **post_llm_call 触发 + daemon 异步** — fin 粒度独立触发

## 选定

fin 粒度触发 + daemon 线程异步执行。LLM 降级链：OODA 文本→JSON→Regex。

## 影响

✅ 不阻塞主线程，fin 粒度精确控制 | ❌ 线程数随 fin 数增长
