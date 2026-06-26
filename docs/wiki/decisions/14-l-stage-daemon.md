---
title: L-stage 守护线程异步摘要
slug: l-stage-daemon
category: decision
date: "2026-05"
version_introduced: v4.0
alternatives: ["同步摘要（阻塞用户路径）", "post_llm_call 同步写（复杂）"]
chosen: "双独立 daemon 线程 + 限速 + 3 次失败降级 + 自动拆工具轮"
affects: ["03-f-stage-async-summary"]
status: 已替换为 process_turn_f_stage 触发模型（v5.10）（v5.2 后重命名为 F-stage）
---

## 触发条件

摘要生成不应阻塞用户路径。

## 选定

- 双独立 daemon 线程（生成 + 写回）
- 限速：避免 LLM 调用爆管
- 3 次失败降级：LLM 不可用时自动降级
- 自动拆工具轮：工具轮与对话轮分离摘要

旧决策节点：`C-019`, `C-020`, `C-021`, `C-022`, `C-023`
