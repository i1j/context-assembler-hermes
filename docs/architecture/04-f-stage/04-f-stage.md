---
title: F-stage 异步摘要（fin 粒度）
slug: f-stage
category: architecture
version_introduced: v5.2
status: 已实装（v6.1 决策 44 多事务 OODA + 决策 45 v3 单一数据源全链路）
decisions: [f-stage-async, l-stage-daemon, 44-e-stage-granularity-think, 45-multi-affair-ooda-record]
depends_on: [store, e-stage]
updated: 2026-08-17
source_files: ["ca/f_stage.py", "ca/fct_multi_affair.py", "ca/prompts.py", "ca/store.py", "ca/topic_summary.py"]
---

## 问题

E-stage 写入的 Elm 需要被摘要化为 Fct（单轮摘要 JSON）和 Hdl（单句标题）。同一 user turn 因多次 LLM 调用可能产生多条 fin 行，每条需独立摘要。

## 设计方案

- **fin 粒度触发**：`post_llm_call` 为本次写入的 fin 行调用 `process_turn_f_stage(turn, fin_seq)`
- **daemon 线程**：异步执行，读取增量 Elm → 调用 LLM 生成 Fct+Hdl → 写回 turn_stream
- **多条 fin 互不阻塞**：各有独立 daemon 线程和增量摘要范围
- **决策 44 输入**：事务帧 `[ooda_stage|block_type]` + 代码筛选后的首轮 think 卡
  （orient 优先、截断预算；见 `ca/fct_multi_affair.py:build_fct_think_context`）
- **决策 45 输出（v3，单一数据源）**：prompt 输出
  `{"affairs":[{hdl,turns,ooda}]}`——OODA 四段数组项即该阶段变更记录，
  无 `changes`/`stage_tag`（无【已完成】等标签）；落库 Fct JSON 只有
  `affairs + _assemble_status + _fct_format`（`build_fct_multi_affair`）。
  v2（带 changes）仅旧库只读兼容。
- **读路径派生旧视图**：`collect_turn_fcts` 从 affairs 现场派生
  changes/tags/ooda_tags/todos/补充四段，供 topic fallback/embedding；
  `topic_manager` 语义提取对 affairs 内 ooda 值放行。
- **topic 4B 失败兜底**：`_fallback_strands_from_affairs` 按同名事务
  跨轮聚合多 strands，不再单 strand 打包。

### LLM 降级链

```
multi-affair JSON → legacy Markdown/XML 解析 → Regex fallback
(首选, v6.1)        (兼容旧输出)               (最后防线)
```

## 关键约束

- 降级检测（实际实现）：LLM 输出经 `MEANINGLESS_CORE` 清洗（`无`/`本轮无新内容`
  等无意义 core_change 剔除）；Fct/Hdl 仍写入，core_change 置为
  `user_elm or "本轮无新内容"`（不再保留上次 Fct/Hdl）
- 超时：LLM 调用 `timeout=Config.LLM_TIMEOUT`（默认 120s）
- 多事务截断：`finish_reason=length` 时先尝试解析 partial JSON，再回退
  `PAIR_PATTERN`，均失败才走 pending backfill
- 兼容：`CA_FCT_MULTI_AFFAIR_ENABLED=0` 整体回退旧 prompt/旧解析
