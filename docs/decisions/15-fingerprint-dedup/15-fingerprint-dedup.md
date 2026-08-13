---
title: 指纹去重
slug: fingerprint-dedup
category: decision
date: "2026-05"
version_introduced: v4.1
affects: [store]
status: 未落地（代码无 SHA256 指纹实现，2026-08-13 核对修订）
source_files: ["ca/cache.py"]
---

## 触发条件
同一内容在不同路径下被多次嵌入（如 Fct 和 Hdl 共享部分文本），浪费嵌入服务和计算资源。

## 选定
SHA256 指纹用于重复检测。缓存查找优先于嵌入调用。避免重复嵌入相同文本。

## 修订（2026-08-13 代码核对）
**SHA256 指纹方案未落地**：`ca/cache.py` 无任何 sha256/fingerprint 相关实现
（全仓 grep 0 匹配）。当前嵌入去重由 `semantic_fct_embeddings` 以 turn 为 key 的
dict 天然承担（同 turn 不重复嵌入，`cache.py:133/161` + `topic_manager.py:628`），
非指纹机制。原 status"已实装"不成立；如需指纹级去重（跨 turn 相同文本），需另行实现。
