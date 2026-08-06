---
title: 术语统一（Elm/Fct/Hdl）
slug: naming-unification
category: decision
date: "2026-06"
version_introduced: v5.5
affects: [全系统]
status: 已实装
source_files: [全代码库]
---

## 触发条件
旧术语 L0/L1/L2（LLM 压缩层次）与 C-stage/L-stage（管线阶段）命名混用，代码/文档/数据库列名不统一。

## 选定
L0→Elm（原始消息文本）、L1→Fct（结构化摘要）、L2→Hdl（一句话标题）。C-stage→E-stage、L-stage→F-stage。stage_tag 独立不混入 core_change。全量迁移 DB 列名、代码变量名、文档引用。
