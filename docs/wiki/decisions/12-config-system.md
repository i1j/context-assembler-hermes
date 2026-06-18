---
title: 配置体系
slug: config-system
category: decision
date: "2026-05"
version_introduced: v3.0
alternatives: ["硬编码（无法热配置）", "JSON 配置（无 schema 校验）"]
chosen: "Config 类 + YAML + 三段回退 + protect_tail 唯一源"
affects: ["06-tail-protection"]
status: 已实装
---

## 触发条件

CA 插件需要统一配置管理。

## 选定

- Config 类：所有配置集中管理
- YAML：用户可写配置文件
- 三段回退（hardcoded default → config file → env overrides）
- protect_tail 作为唯一源（不是冗余存储）
- `_assemble_status` 常量统一
- `CONTEXT_LENGTH` 配置

旧决策节点：`F-001`, `F-002`, `F-003`, `F-004`

## 影响

✅ 三段回退覆盖了所有配置场景
✅ protect_tail 唯一源避免了配置冲突
