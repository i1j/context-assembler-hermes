---
title: 工具调用规则引擎摘要
slug: tool-summarizer
category: decision
date: "2026-05"
version_introduced: v4.3
affects: [tool-summarizer]
status: 已实装
source_files: ["ca/tool_summarizer.py"]
---

## 触发条件
工具调用（exec/search/read 等）JSON 输出大量消耗 token。LLM 摘要每次调用的延迟不可接受。

## 选定
规则引擎摘要—不调 LLM。YAML 三段回退链。四级字段优先级 VIP/P0/P1/P2。10 个结构化 Handler。
