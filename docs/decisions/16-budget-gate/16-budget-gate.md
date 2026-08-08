---
title: 预算闸门
slug: budget-gate
category: decision
date: "2026-05"
version_introduced: v4.2
affects: [a-stage]
status: 已取代（方向 B 后弱化）
source_files: ["ca/a_stage.py"]
---

## 触发条件
A-stage 装配时 conv_history 可能超过 LLM 的 context window，需要预算控制。

## 选定
从 context_length 参数获取最大 token 预算。超预算时回退 `_hard_truncation` 最早对话轮。v6.0 方向 B 后预算闸门弱化——DB 重建无 mutation 限制，可直接丢弃最早轮次。
