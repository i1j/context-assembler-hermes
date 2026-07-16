---
title: bg_review 同步写 Fct
slug: bg-review-sync
category: decision
date: "2026-06-15"
version_introduced: v5.5
alternatives: ["走 LLM 摘要（浪费）", "不写 Fct（数据空洞）"]
chosen: "检测 skill_provenance 来源，跳过 A-stage，同步写 Fct"
affects: ["07-bg-review-sync-write"]
status: 已实装
source_files: ["ca/lstage.py", "ca/e_stage.py"]
---

## 触发条件

bg_review 轮在 pre_llm_call 中不需要 A-stage 上下文装配，但 Fct 列必须有值。

## 备选方案

1. **走完整 A-stage + LLM 摘要**：bg_review 内容已存在，浪费 LLM 调用
2. **跳过不写 Fct**：数据空洞，下游组件（topic_manager 等）无法处理
3. **同步写 Fct（选定）**：检测 + 跳过 + 直接填充

## 选定

- 检测：`get_current_write_origin() == 'skill_provenance'`
- 跳过 A-stage 装配和 LLM 摘要
- 同步写 Fct = user content（原文）
- Hdl 留空
