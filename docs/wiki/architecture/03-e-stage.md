---
title: E-stage 写入协议
slug: e-stage
category: architecture
version_introduced: v5.0
status: 已实装
decisions: [e-stage-on-receive, schema-v5]
depends_on: [store]
updated: 2026-07-26
source_files: ["ca/e_stage.py"]
---

## 问题

Hermes 对话系统的 Hook 生命周期包含 5 个阶段。需要确定每个 Hook 负责写入哪一行、哪些字段——错误的分配导致字段缺失或覆盖。

## 5 Hook 写即落盘

| Hook | 写入 (turn,seq) | 写入字段 | 时机 |
|------|-----------------|----------|------|
| `pre_llm_call` | `(n, 0)` | `role='user', Elm` | LLM 调用前 |
| `post_api_request` | `(n, 1)` | `role='assistant', Elm(thought), tool_calls_json` | API 返回后，写 thought + tool 占位行 |
| `pre_tool_call` | — | **no-op** | 占位行已写入 |
| `post_tool_call` | `(n, m)` | `role='tool', Elm, per-tool Fct` | 工具执行后回填 |
| `post_llm_call` | `(n, N)` | `role='assistant', Elm(fin_reason='stop')` | LLM 最终回复，同时触发 F-stage |

## 关键约束

- **无 buffer** — 每条消息立即写入 turn_stream
- **无回滚** — 一行写入即不可撤销
- **Tool 占位行** — `post_api_request` 为每个 tool_call 写入 `role='tool', status='pending'`
- **纯对话轮跳过** — `post_api_request` 在无 tool_defs 且 finish_reason != `tool_calls`
  时直接 return，不写 thought 行（纯对话轮由 `post_llm_call` 的 fin 行承载）
- **幂等** — 同一 (turn,seq) 不会重复写入
