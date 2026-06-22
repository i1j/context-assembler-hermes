---
title: C-stage 异步化 + turn_index 分配
slug: c-stage-async
category: decision
date: "2026-05"
version_introduced: v4.0
alternatives: ["同步写入（阻塞主线程）", "批量写入（延迟可见性）"]
chosen: "daemon 线程异步化 + turn_index 分配钩子"
affects: ["02-e-stage-write-protocol"]
status: 已实装（v5.0 后 E-stage 继承此设计）
---

## 触发条件

原始数据写入不应阻塞对话主路径。

## 选定

- C-stage daemon 线程异步处理写入
- turn_index 分配：在 pre_llm_call 中分配唯一 turn 序号
- v5.0 后重命名为 E-stage（写入阶段），继承异步写入设计

旧决策节点：`C-001`, `C-002`
