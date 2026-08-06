---
title: 尾巴保护区
slug: tail-protection
category: decision
date: "2026-06"
version_introduced: v5.3
affects: [a-stage]
status: 已实装
source_files: ["ca/a_stage.py", "ca/config.py"]
---

## 触发条件
A-stage 装配时最近几轮用户输入（对话尾部）是最关键上下文。Fct 替换将丢失关键信息。

## 选定
保护最后 CA_PROTECT_TAIL_TOKENS（默认 20000）token 对应的 user 轮。从 turn_stream 末端向前扫描。保护区内全 Elm 保留，不替换为 Fct/Hdl。
