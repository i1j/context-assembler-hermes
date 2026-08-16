---
title: E-stage 写入协议
slug: e-stage
category: architecture
version_introduced: v5.0
status: 已实装（v6.1 决策 44 扩展）
decisions: [e-stage-on-receive, schema-v5, 44-e-stage-granularity-think]
depends_on: [store]
updated: 2026-08-17
source_files: ["ca/e_stage.py", "ca/blocks.py", "ca/meta_marker.py", "ca/think_collect.py"]
---

## 问题

Hermes 对话系统的 Hook 生命周期包含 5 个阶段。需要确定每个 Hook 负责写入哪一行、哪些字段——错误的分配导致字段缺失或覆盖。

## 13 Hook 写即落盘（v6.1 决策 44）

| Hook | 写入 (turn,seq) | 写入字段 | 时机 |
|------|-----------------|----------|------|
| `pre_llm_call` | `(n, 0)` | `role='user', Elm, block_type=user_message, ooda_stage=orient` | LLM 调用前 |
| `pre_api_request` | — | 内存 pending 请求元数据（model/provider/input_chars 等） | 每次 API 尝试前 |
| `on_stream_start/delta/end` | — | 内存流式计数（reasoning/text/chunk），end 时 UPSERT 补丁 llm_calls | 流式 token 旁路（异步 worker） |
| `post_api_request` | `(n, k)` | THINKING/AGENT_REPLY 拆块 + `tool_call_request` 占位；`block_type/ooda_stage/request_id/provider/model/usage`；llm_calls 一行；decision/orient 思考卡 | API 响应后（每次 API 调用，非每轮） |
| `api_request_error` | — | llm_calls `status=failed, finish_kind=error` | 每次失败尝试 |
| `pre_tool_call` | — | **no-op** | 占位行已写入 |
| `post_tool_call` | `(n, m)` | `role='tool', Elm, per-tool Fct, block_type=tool_call_result, ooda_stage=observe, result_chars/error_text` | 工具执行后回填 |
| `post_llm_call` | `(n, N)` | `role='assistant', Elm, finish_reason='stop', is_fin=1, block_type=agent_reply, ooda_stage=decide` + conclusion 思考卡 | LLM 最终回复，同时触发 F-stage |

## 关键约束

- **无 buffer** — 每条消息立即写入 turn_stream；流式计数只走内存 pending，on_stream_end 一次性 UPSERT（不做逐 token 落盘）
- **无回滚** — 默认不可变：同 `(turn, seq)` 内容相同重复写入跳过（BUG-09 防重放）；
  内容不同（引擎恢复/重放）保持 `INSERT OR REPLACE` 覆盖；Fct/Hdl 回填列更新走 REPLACE
- **Tool 占位行** — `post_api_request` 为每个 tool_call 写入 `role='tool', status='pending', block_type=tool_call_request`
- **纯对话轮跳过** — `post_api_request` 在无 tool_defs、无 reasoning 且 finish_reason != `tool_calls`
  时直接 return 不写 turn_stream（纯对话轮由 `post_llm_call` 的 fin 行承载）；llm_calls 仍每 API 调用一行
- **幂等** — 同一 (turn,seq) 内容相同的重复写入被跳过；内容不同按 REPLACE 覆盖语义处理
- **乱序安全** — `on_stream_*` 异步 worker 与 `post_api_request` 同步 invoke 无顺序保证；
  llm_calls 以 `(session_id, request_id)` UPSERT，统计列 MAX 合并（决策 44 §3.4）
