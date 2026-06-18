---
title: L1 摘要重构（v4.7.0, PDD 驱动）
slug: l1-summary-pdd
category: decision
date: "2026-06-09"
version_introduced: v4.7.0
alternatives: ["保留旧 L1 prompt（质量不足）", "手动调参（不可复现）"]
chosen: "PDD 驱动：新 Prompt / 独立配置 / 防御性解析 / 截断校验 / 旧数据适配"
affects: ["11-fct-format-evolution"]
status: 已实装（v5.2 后被 Fct/F-stage 取代）
---

## 触发条件

L1 摘要质量不足，需要系统化重构。

## 选定（PDD 驱动）

- **L1-001** PDD → **L1-002~L1-012** 逐点重构：
  - 新 Prompt（结构化输出 `<core_change>` 标签）
  - 独立配置（摘要模块独立配置）
  - 防御性解析（`<stage_tag>/<core_change>` 正则）
  - 截断校验（200ch 截断 + 完整性检查）
  - 异常处理（LLM 输出格式异常兜底）
  - 旧数据适配（`_extract_l0` 回退兼容）
  - Metrics（精度/召回率监控）
  - 截断优先级（content > tool_calls > reasoning）
  - 预编译正则（性能优化）
  - 语义短路（无变更 skip LLM）
  - 标签清洗移除

## 影响

✅ PDD 驱动保证了渐进式重构
✅ 独立配置精细化控制
❌ v5.2 后 Fct/F-stage 替代此方案
