---
title: LLM 降级链
slug: llm-fallback-chain
category: decision
date: "2026-05"
version_introduced: v2.0
alternatives: ["单 LLM 崩了就崩了（高错误率）", "固定降级配置（不灵活）"]
chosen: "三级回退 + OODAParser 别名匹配"
affects: []
status: 已实装
source_files: []  # 全局配置，非 CA 特有
---

## 触发条件

LLM 调用可能因超时、限流、格式异常失败，需要降级链。

## 选定

- **C-003 num_predict**：512→24768（扩大 token 预算）
- **C-003a**：LLM 参数三级回退（different provider → smaller model → local fallback）
- **C-003b**：OODA 文本 vs JSON 双模式
- **C-003c**: OODAParser 别名匹配（容忍模型输出变体）

旧决策节点：`C-003`, `C-003a`, `C-003b`, `C-003c`, `C-003d`

## 影响

✅ 三级降级覆盖了大部分故障场景
✅ OODAParser 别名匹配避免格式异常
