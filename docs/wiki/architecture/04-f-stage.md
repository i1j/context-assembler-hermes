---
title: F-stage 异步摘要（fin 粒度）
slug: f-stage
category: architecture
version_introduced: v5.2
status: 已实装
decisions: [f-stage-async, l-stage-daemon]
depends_on: [store, e-stage]
updated: 2026-07-26
source_files: ["ca/f_stage.py"]
---

## 问题

E-stage 写入的 Elm 需要被摘要化为 Fct（单轮摘要 JSON）和 Hdl（单句标题）。同一 user turn 因多次 LLM 调用可能产生多条 fin 行，每条需独立摘要。

## 设计方案

- **fin 粒度触发**：`post_llm_call` 为本次写入的 fin 行调用 `process_turn_f_stage(turn, fin_seq)`
- **daemon 线程**：异步执行，读取增量 Elm → 调用 LLM 生成 Fct+Hdl → 写回 turn_stream
- **多条 fin 互不阻塞**：各有独立 daemon 线程和增量摘要范围

### LLM 降级链

```
OODA 文本  →  JSON 解析  →  Regex fallback
(首选)       (备选)         (最后防线)
```

## 关键约束

- 降级检测（实际实现）：LLM 输出经 `MEANINGLESS_CORE` 清洗（`无`/`本轮无新内容`
  等无意义 core_change 剔除）；Fct/Hdl 仍写入，core_change 置为
  `user_elm or "本轮无新内容"`（不再保留上次 Fct/Hdl）
- 超时：LLM 调用 `timeout=Config.LLM_TIMEOUT`（默认 120s）
