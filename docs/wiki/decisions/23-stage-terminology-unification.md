---
title: 阶段术语统一（C/L → E/F/A）
slug: stage-terminology-unification
category: decision
date: "2026-06-17"
version_introduced: v5.5
alternatives: ["保留 C/A/L（三阶段架构继承）", "自创新术语（无生态支持）"]
chosen: "E-stage（写入）/ F-stage（异步摘要）/ A-stage（装配）+ Elm/Fct/Hdl"
affects: ["12-naming-convention"]
status: 已实装
---

## 触发条件

旧术语 C-stage/A-stage/L-stage 经过 v5.0 重写后，与代码实际作用不再匹配。C-stage 已变为纯数据写入，L-stage 已变为异步摘要。

## 备选方案

1. **保留 C/A/L**：名不副实
2. **Elm/Fct/Hdl + E/F/A（选定）**

## 选定

- **E-stage**（write）：原始数据写入 → `turn_stream.Elm`
- **F-stage**（async summary）：异步 LLM 摘要 → `turn_stream.Fct`
- **A-stage**（assemble）：上下文装配
- **Hdl**（handle）：跨轮历元摘要
- Grade 枚举：`Grade.ELM`, `Grade.FCT`, `Grade.HDL`
- 2016-06-17 用户多次纠正确保 DB 列名和代码常量一致
