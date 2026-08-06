---
title: bg_review 同步跳过
slug: bg-review-sync
category: decision
date: "2026-06"
version_introduced: v5.5
affects: [hooks]
status: 已实装
source_files: ["plugins/ca_assembler/__init__.py"]
---

## 触发条件
bg_review（背景评审）轮次不应计入 CA 的 turn 计数，不应触发话题检测或 DB 写入。

## 选定
pre_llm_call 中检测 bg_review 会话（biz_category='bg_review'）。完全跳过：不计 turn、不写 DB、不调话题检测。返回 None。
