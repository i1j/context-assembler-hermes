---
title: 嵌入服务多后端
slug: embedding-multi-backend
category: decision
date: "2026-05"
version_introduced: v2.0
alternatives: ["单后端绑定（vendor lock-in）", "纯本地（性能差）"]
chosen: "多后端（Ollama 本地 + API 远程）+ LRU 缓存 + 降级"
affects: []
status: 已实装（v5.x 后逐渐降级）
source_files: []  # 已从 CA 移除，历史参考
---

## 触发条件

检索需要的向量嵌入应从何处获取。

## 选定

- 多后端支持：Ollama 本地 + API 远程
- 连接池：避免重复建立连接
- LRU 缓存：不缓存 fallback 结果
- 线程安全：锁保护
- 降级：主后端失败→切换备用→最终报错

旧决策节点：`E-001`, `E-002`, `E-003`, `E-004`

## 影响

✅ 多后端灵活切换
❌ v5.0 后检索已不再是核心功能
