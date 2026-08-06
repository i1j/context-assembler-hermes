---
title: 工具调用摘要引擎
slug: tool-summarizer
category: architecture
version_introduced: v4.3
status: 已实装
decisions: [tool-summarizer]
depends_on: [store]
updated: 2026-07-26
source_files: ["ca/tool_summarizer.py", "ca/tool_field_priority.yaml"]
---

## 问题

工具调用（exec、search、read 等）产生大量 JSON 输出，直接填入 conv_history 会浪费 token。需要在保持关键信息的同时压缩工具调用结果。

## 设计方案

- **规则引擎**：工具调用不调 LLM，使用预定义规则摘要
- **四级字段优先级**：
  | 等级 | 含义 |
  |------|------|
  | VIP | 必须保留（如搜索结果列表） |
  | P0 | 高优先级 |
  | P1 | 中优先级 |
  | P2 | 可忽略 |

- **三段回退链**：YAML 配置 handler → JSON 框架 → 字符串截断
- **10 个结构化 Handler**：exec、search、read、write、code、web 等
- 每个 tool_call 单独摘要，输出 `tool_name:result_summary` 格式
