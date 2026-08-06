---
title: LLM 降级链
slug: llm-fallback
category: decision
date: "2026-05"
version_introduced: v4.3
affects: [f-stage, l-stage]
status: 已实装
source_files: ["ca/f_stage.py", "ca/prompts.py"]
---

## 触发条件
LLM 摘要可能失败（超时、JSON 解析错误、OODA 文本不完整）。需要渐进式降级。

## 选定
三级降级：首选 LLM OODA 文本 → 备选 JSON 解析 → Regex fallback 最后防线。num_predict=24768 可配置。OODAParser 别名匹配。降级检测：`startswith("核心摘要：无有效增量")` + `"资源与观察：\n- 无"`。
