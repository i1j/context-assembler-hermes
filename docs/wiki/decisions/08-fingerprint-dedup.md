---
title: 全指纹去重
slug: fingerprint-dedup
category: decision
date: "2026-05"
version_introduced: v3.0
alternatives: ["content 全文比较（O(n²)）", "不处理去重（重复内容膨胀）"]
chosen: "全指纹 MD5 + 规范化 + Fail-Safe + role 字段"
affects: []
status: 已实装
source_files: []  # 已从 CA 移除，历史参考
---

## 触发条件

LLM 可能输出相同工具结果，避免重复存储。

## 选定

- MD5 全指纹（content + role 联合 hash）
- 规范化（字典排序、嵌套解析）
- Fail-Safe：指纹冲突时回退到 content 比较
- role 字段区分同内容不同角色

旧决策节点：`DE-001`, `DE-002`, `DE-003`, `DE-004`
