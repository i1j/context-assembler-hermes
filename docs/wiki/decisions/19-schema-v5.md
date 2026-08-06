---
title: Schema v5 存储重写
slug: schema-v5
category: decision
date: "2026-06"
version_introduced: v5.0
affects: [store]
status: 已实装
source_files: ["ca/store.py"]
---

## 触发条件
旧 schemav5 四主键 (session_id, turn_index, api_call_count, seq_index) + conv_encoding 编码层有损且复杂。

## 选定
三主键 (session_id, turn, seq)。取消 conv_encoding。Elm/Fct/Hdl 三列明文。迁移路径：从 turn_cache 迁移到 turn_stream。
