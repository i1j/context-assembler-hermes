---
title: 指纹去重
slug: fingerprint-dedup
category: decision
date: "2026-05"
version_introduced: v4.1
affects: [store]
status: 已实装
source_files: ["ca/cache.py"]
---

## 触发条件
同一内容在不同路径下被多次嵌入（如 Fct 和 Hdl 共享部分文本），浪费嵌入服务和计算资源。

## 选定
SHA256 指纹用于重复检测。缓存查找优先于嵌入调用。避免重复嵌入相同文本。
