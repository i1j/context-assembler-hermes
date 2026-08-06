---
title: 引擎 TTL 恢复（CR-009）
slug: engine-ttl-recovery
category: decision
date: "2026-07"
version_introduced: v6+
affects: [plugin]
status: 已实装
source_files: ["plugins/ca_assembler/__init__.py"]
---

## 触发条件
SessionManager TTL 1800s 清理空闲引擎后，plugin._engine 指向已销毁的 ContextAssembler。

## 选定
pre_llm_call 中每次调用重新从 session_manager.get() 获取引擎引用。TTL 清理后自动重建新连接。CR-009 修复。
