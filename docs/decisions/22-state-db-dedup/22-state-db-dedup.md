---
title: State DB 用户消息去重
slug: state-db-dedup
category: decision
date: "2026-06"
version_introduced: v6.1
affects: [plugin]
status: 未落地（代码无对应实现，2026-08-13 核对修订）
source_files: ["plugins/ca_assembler/__init__.py"]
---

## 触发条件
Web UI 会话中 state.db user 消息被重复写入（gap=1，内容完全相同）。合规审计发现。

## 根因
Hermes Gateway：新 turn 重置 `_last_flushed_db_idx=0` → `_flushed_db_message_ids` 清空 → 所有 user dict 被二次写入。非 CA 故障。

## 选定
CA 注册 `on_session_finalize` hook，SQL 清理相邻重复 user 行。仅删除 gap=1 且内容完全相同的 user 行，保留第一次写入。

## 修订（2026-08-13 代码核对）
**本决策的选定方案未落地**：`register()` 只注册 8 个 hooks（`on_session_start/end/reset`、
`pre_llm_call`、`post_llm_call`、`post_api_request`、`pre_tool_call`、`post_tool_call`），
**无 `on_session_finalize`**（全仓 grep 0 匹配）。原 status"已实装"不成立。
当前 state.db 去重实际依赖 Hermes 侧行为（重复写入场景是否仍存在未复核）；如确需
按本决策执行，需在 register() 补注册 `on_session_finalize` 并实现相邻重复 user 行清理。
