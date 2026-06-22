---
title: 尾巴保护（protect_tail=2）
slug: tail-protection
category: decision
date: "2026-06-14"
version_introduced: v5.3
alternatives: ["protect_tail=0（尾部被替换）", "按 token 计算边界（不稳定）"]
chosen: "固定最后 N user 轮保留 Elm，与 grade 判定独立"
affects: ["06-tail-protection"]
status: 已实装
---

## 触发条件

A-stage 装配时最后几轮用户输入被 Fct 替换，丢失关键上下文。

## 备选方案

1. **protect_tail=0**：尾部可能被替换 → 信息丢失
2. **按 token 保护**：边界不稳定，Fct 长度波动影响边界
3. **按 user 轮固定保护（选定）**：protect_tail=2，最后 2 user 轮保留 Elm

## 选定

- protect_tail 默认 2
- 尾巴判定在 grade 判定之前，优先级最高
- 尾部 turn `get_turn_grade()` 返回强制 Grade.ELM
