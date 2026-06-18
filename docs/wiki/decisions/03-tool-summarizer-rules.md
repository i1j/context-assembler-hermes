---
title: 工具轮规则引擎
slug: tool-summarizer-rules
category: decision
date: "2026-05"
version_introduced: v1.0
alternatives: ["每工具轮调 LLM 摘要（3~5 秒延迟）", "纯正则提取（丢失结构化信息）"]
chosen: "四级字段优先级规则引擎，不调 LLM"
affects: []
status: 已实装
---

## 触发条件

工具输出需要从 JSON 中提取关键信息，摘要格式需标准化。

## 备选方案

1. **LLM 摘要**（3~5 秒延迟，不可接受）
2. **规则引擎（选定）**：四级字段优先级提取
3. **纯 truncation**：丢失结构化信息

## 选定

- 工具输出 JSON 通过四级字段优先级提取（`result` > `error` > `content` > `data`）
- 摘要格式：`[tool_name] {tool_name}: core_value truncated_to_100ch`
- 10 个结构化 Handler 覆盖常见工具类型
- 配置三段回退（default → tool-specific → handler）

旧决策节点：`C-004`, `C-005`, `C-006`, `C-007`, `C-008`, `C-009`, `T-001`, `T-002`

## 影响

✅ 零 LLM 延迟：规则引擎毫秒级完成
✅ 三级回退配置灵活
❌ 100ch 截断可能丢失关键细节
