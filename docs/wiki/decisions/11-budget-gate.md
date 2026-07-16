---
title: 预算闸门
slug: budget-gate
category: decision
date: "2026-05"
version_introduced: v2.5
alternatives: ["无限制（cost 失控）", "固定轮数（不灵活）"]
chosen: "可配置预算上限：max_llm_calls / max_tokens / max_iterations"
affects: []
status: 已实装
source_files: []  # 已从 CA 移除，历史参考
---

## 触发条件

LLM 调用需要预算控制避免 cost 超支。

## 选定

- `max_llm_calls`：LLM 调用次数上限
- `max_tokens`：总 token 上限
- `max_iterations`：迭代次数上限

旧决策节点：`C-017`
