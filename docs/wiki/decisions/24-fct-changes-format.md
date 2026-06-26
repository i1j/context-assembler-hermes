---
title: Fct changes 列表格式
slug: fct-changes-format
category: decision
date: "2026-06-15"
version_introduced: v5.2
alternatives: ["纯 JSON 块（非列表，难扩展）", "旧标签格式（单对无法表达多事项）"]
chosen: "changes 列表 + PAIR_PATTERN 正则 + VALID_STATES + 旧数据回退"
affects: ["11-fct-format-evolution"]
status: 已实装
---

## 触发条件

Fct（单轮摘要）需要表示多事项状态变更（每个事项有 stage_tag + core_change）。

## 备选方案

1. **旧格式 `<stage_tag>/<core_change>`**：单对字符串，一轮只能有一个事项
2. **纯 JSON 块**：非列表无法表达多事项，解析复杂
3. **changes 列表（选定）**：`[{"stage_tag":"...","core_change":"..."}, ...]`

## 选定

- 格式：`{"changes": [{"stage_tag": "已实施", "core_change": "xxx"}, ...]}`
- 正则：`PAIR_PATTERN` = `<stage_tag>...</stage_tag> <core_change>...</core_change>` 非贪婪匹配
- `VALID_STATES`：已实施、评估中、已决、已验证、已回退、标记中
- 零对输出检测：空 changes 时特殊标记
- 旧数据回退：`clean_increment` / `_extract_l0`

## 之前 vs 之后

**之前**：`已实施/修复用户登录超时问题`（单对，无法多事项）
**之后**：`[{"stage_tag":"已实施","core_change":"修复用户登录"}, {"stage_tag":"评估中","core_change":"重构连接池"}]`
