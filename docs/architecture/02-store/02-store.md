---
title: 存储模型
slug: store
category: architecture
version_introduced: v5.10
status: 已实装
decisions: [sqlite-wal, schema-v5, naming-unification]
depends_on: []
updated: 2026-07-26
source_files: ["ca/store.py"]
---

## 问题

旧 `turn_cache` 表使用四复合主键 `(session_id, turn_index, api_call_count, seq_index)` + `conv_encoding` blob 编码层，写操作易错、编码有损、查询慢。

## turn_stream 表（18 列）

```sql
-- PK (session_id, turn, seq)
session_id     TEXT    NOT NULL  -- 会话标识
turn           INTEGER NOT NULL  -- 每轮 user 消息数（1-based）
seq            INTEGER NOT NULL  -- 轮内序号（0=user, 1=thought, 2..=tool, N=fin）
role           TEXT    NOT NULL  -- user / assistant / tool
Elm            TEXT    DEFAULT ''-- 原始消息文本
tool_name      TEXT             -- 工具名（仅 tool 行）
tool_call_id   TEXT             -- 工具调用 ID
args_json      TEXT             -- 工具参数 JSON
status         TEXT             -- ok / error / blocked / pending
duration_ms    INTEGER          -- 工具执行耗时（ms）
tool_calls_json TEXT            -- thought 行的 tool_calls 原始 JSON
finish_reason  TEXT             -- stop / tool_calls / length
usage_prompt_tokens     INTEGER -- LLM prompt token 数
usage_completion_tokens INTEGER -- LLM completion token 数
biz_category   TEXT             -- 业务分类（bg_review 等）
written_at     REAL             -- time.time()
Fct            TEXT             -- 结构化摘要 JSON（F-stage 写入）
Hdl            TEXT             -- 一句话标题（F-stage 写入）
```

- WAL 模式（`PRAGMA journal_mode=WAL`）
- 无编码层 — Elm/Fct/Hdl 直接存储原始文本/JSON

## 关键约束

- 行不可变 — 默认不可变：同 `(session_id, turn, seq)` 重复写入且核心列相同时跳过
  （BUG-09 防重放；`write_turn_v5` 写入前同内容检查）；内容不同
  （引擎恢复/重放路径）保持 `INSERT OR REPLACE` 覆盖语义；回填列（Fct/Hdl）
  更新走 `UPDATE`（`store.py` `update_fin_fct_v5`，不重写其它列）
- 旧 turn_cache 表在 v5.10 迁移后移除

## 数据表族（v6.5 → v7 演进，2026-08-07）

> 本页为 v6.0 基线（turn_stream）；v6.5 theme 层与 v7 reality 层的存储演进如下（详见 `decisions/36-theme-wiki-generation.md` / `decisions/41-reality-production-migration.md`）：

| 表 | 版本 | 状态 | 用途 |
|---|---|---|---|
| `turn_stream` | v5.0 | ✅ | 原始消息核（(session_id, turn, seq) PK） |
| `strand_summaries` | v6.4 | ✅ 活跃 | 话题块内工作线（hdl/ooda/turns/centroid） |
| `themes` | v6.5 | 🧊 冻结（416 行） | 旧归并层（title/overview/ooda/...），v7 退役只读归档 |
| `theme_strand_map` | v6.5 | 🧊 停用 | strand→theme 映射（被 strand_to_reality 取代） |
| `realities` | **v7（决策 41）** | ✅ 生产主表（111 行） | 现实工作对象：name/hdl/current_status/timeline/source_strands + centroid_json/query_centroid_json/query_count + health 字段 |
| `strand_to_reality` | **v7（决策 41）** | ✅（574 条） | strand_id → reality_id（PK strand_id，merge 写库） |
| `cooccurrence_events` | v7（决策 38/41） | ✅ | reality 共现边（(session_id, topic_id, profile, reality_a, reality_b) UNIQUE 幂等） |
| `session_meta` / `refinement_meta` / `wiki_associations` | v5.14+ | ✅/遗留 | 会话元数据 / L4 精炼记账 / graph 关联缓存（wiki_associations 仍读 themes，冻结后无新数据） |

**realities 表结构（决策 41 生产版）**：`reality_id`(PK AI) / `name`(固定标识) / `hdl`(状态锚点,可改) / `current_status`(JSON: goals/current_state/key_facts/context) / `timeline`(JSON 演进序列) / `source_strands`(JSON: {"session_id":[strand_id]}) / `profile` / `centroid_json`(语义检索 fallback) / `query_centroid_json`+`query_count`(提问云形心,注入主拣选) / `health_score`/`flagged_for_review`/`topic_count`/`reviewed_at`/`last_reviewed_turn`(L4 精炼) / `created_at`/`updated_at`。

**写入入口**：strand 生成后 `run_reality_merge`（`ca/reality.py`）upsert realities + strand_to_reality + query_centroid 增量；`record_block_cooccurrences`（`ca/store.py`）写真 reality_id 共现边。
