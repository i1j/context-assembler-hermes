---
title: v5.1 注入重构
slug: injection-refactor
category: decision
date: "2026-06-14"
version_introduced: v5.1
alternatives: ["直接替换（覆盖旧数据）", "追加（上下文膨胀）", "关闭（无注入）"]
chosen: "Replace/Append/Off 三模式 + 行类型表 + bypass_turns + 工具行清空"
affects: ["04-a-stage-role-match"]
status: 已实装
source_files: ["ca/a_stage.py"]
---

## 触发条件

CA 注入上下文的策略需要灵活配置。

## 选定

- **三模式**：Replace（替换原文）/ Append（追加）/ Off（不注入）
- **行类型表**：不同角色行使用不同注入策略
- **bypass_turns**：指定 turn 跳过注入
- **工具行清空**：工具行注入后清空原始 JSON

旧决策节点：`V-001`, `V-002`, `V-003`, `V-004`, `V-005`
