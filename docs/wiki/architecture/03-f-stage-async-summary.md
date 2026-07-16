---
title: F-stage 异步摘要（fin 粒度）
slug: f-stage-async-summary
category: architecture
version_introduced: v5.2
status: 已实装（v5.10 重构）（v5.10 重构 → fin 粒度 v5.11）
decisions: ["l-stage-daemon", "fct-changes-format"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-25
source_files: ["ca/f_stage.py", "ca/store.py", "ca/__init__.py"]
---

## 问题

E-stage 写入的 Elm（原始数据）需要被摘要化为 Fct（单轮摘要）和 Hdl（单句摘要）。同一个 user turn 可能因两次 LLM 调用产生多条 fin 行，每条 fin 行都需要独立的摘要。

## 决策

### 备选方案

1. **同步摘要** — pre_llm_call 同步调用 LLM 生成摘要，阻塞用户
2. **post_llm_call 同步写** — 每轮对话结束时同步调用 LLM
3. **独立 daemon 线程异步摘要（旧）** — 守护线程轮询新数据 — 已替换为触发模型
4. **post_llm_call 触发 + daemon 线程（当前）** — 回调触发，异步 LLM 执行

### 选定方案

F-stage 按 **fin 行粒度**触发：`post_llm_call_v5` 为本次写入的 fin 行（通过 `seq` 标识）独立调用 `process_turn_f_stage`。多条 fin 行互不阻塞，各有独立的 daemon 线程和增量摘要范围。

```python
# 在 post_llm_call_v5 中触发（__init__.py CAContextAssemblerPlugin）
seq = engine._seq_counter.get(turn, 0) + 1  # 本次 fin 行的序号
engine.process_turn_f_stage(turn, fin_seq=seq)
```

### 实现要点

- **触发条件**：`post_llm_call_v5` 回调末尾调用 `engine.process_turn_f_stage(turn, fin_seq=seq)`
- **路由决策**（`process_turn_f_stage`）：
  1. 检查指定 fin 行（`fin_seq`）是否已有 Fct → 有则跳过
  2. 检测 bg_review → 同步写代码级 Fct（无需 LLM），跳过
  3. 否则 → 启 daemon 线程执行 `_run_f_stage`
- **异步线程**：`_run_f_stage` 在 daemon 线程中执行（`ca/f_stage.py` `FStageMixin`）
- **线程防护**：`_pending_tasks[(turn, fin_seq)].is_alive()` — 按 `(turn, fin_seq)` 键，fin 粒度，互不阻塞
- **增量 Elm 范围**：`read_incremental_elm(store, session_id, turn, fin_seq)` 读取 **user 行 + 上次 fin 之后到本次 fin 之间的内容**（非全量 turn）
  ```
  seq=0: user          ← 始终包含
  seq=1: thought
  seq=2: tool
  seq=3: fin_1         ← 输入 = user + seq=1~3
  seq=4: fin_2         ← 输入 = user + seq=4（上次 fin=3 之后）
  ```
- **Fct 写入**：通过 `_update_fct_v5(session_id, turn, fin_seq, fct, hdl)` 写回**指定 fin 行**（不再用 MAX(seq) 自动定位）
- **Hdl 写入**：从 Fct dict 的 `core_change` 提取（`_extract_hdl`），约 100 字符
- **截断回退**：保留 partial[:500] 并标记 `_assemble_status=1`
- **LLM 全失败回退**：fallback Fct + `_assemble_status=1`
- **FctTruncatedException**：catch 后保留 partial text

### 生成内容

- **Fct（单轮增量摘要）**：基于增量 Elm（user + 自上次 fin 以来的内容）生成 `changes` 列表格式（JSON）
- **Hdl（单句摘要）**：从 Fct 的 `core_change` 提取首句，约 100 字符
  - 不是跨轮历元摘要——仅从当前增量 Fct 提取

详见 `ca/f_stage.py` 的 `_run_f_stage` 和 `_call_llm_for_fct`，以及 `ca/store.py` 的 `read_incremental_elm` 和 `update_fin_fct_v5`。

## 数据验证

```sql
-- 验证 Fct 覆盖率（fin 行）
SELECT turn, seq,
       CASE WHEN Fct != '' THEN '有Fct' ELSE '无Fct' END AS fct_status
FROM turn_stream
WHERE role='assistant' AND finish_reason='stop'
ORDER BY turn, seq;
```

## 优点

- 异步非阻塞：用户路径无等待
- 回调触发：无需轮询，新数据立即处理
- **fin 粒度**：同一 turn 的多条 fin 行各自独立触发 F-stage，互不阻塞
- **增量摘要**：每条 fin 的 LLM 输入仅包含自上次 fin 以来的增量内容，避免重复处理
- 失败重试机制保证最终一致性

## 测试覆盖

- F-stage 摘要测试 — `tests/stage/test_f_stage.py`（`TestFStageSummary`、`TestFStageTruncation`、`TestFStageIsolation`）
- Fct 解析测试 — `tests/parse/test_parse_v1.py`（`TestCleanIncrement`、`TestValidStates`）

## 约束 / 已知问题

- F-stage daemon 线程需确保 `turn_stream` 行不可变：
- `_assemble_status=1` 标记未完成的摘要，A-stage 忽略该 fin 行的 Fct
- session 结束时可能尚有未完成的 LLM 调用（`wait_for_pending` 保障等待）
- 多条 fin 行并发 F-stage 可能增加 LLM 调用压力（但同一 turn 的 fin 行数量极少）
