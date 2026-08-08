---
title: 尾巴保护区
slug: tail-protection
category: decision
date: "2026-06"
version_introduced: v5.3
affects: [a-stage]
status: 已实装
source_files: ["ca/a_stage.py", "ca/config.py"]
---

## 触发条件
A-stage 装配时最近几轮用户输入（对话尾部）是最关键上下文。Fct 替换将丢失关键信息。

## 选定
**保护最后 2 个 user 轮（保底恒保护）**。保护区内全 Elm 保留，不替换为 Fct/Hdl。

### 边界说明（2026-08-08 用户裁定）

「保护后 2 轮正常对话」是正确设计：保护区 = 最后 2 个 user 轮（1 轮时全保护），
**不做 token 预算扫描、不做「超预算向前扩展」**——动态扩展会使 conv_history 内容
随会话长度变化，破坏前缀稳定性，进而破坏云端 prompt 缓存命中率（第一轮 D1 修复
曾引入「按 CA_PROTECT_TAIL_TOKENS 预算扫描 + 超预算向前扩展」，2026-08-08 裁定
为错误设计已回退，恢复原始硬编码实现）。

> 注意：`CA_PROTECT_TAIL_TOKENS`（settings.yaml `protect_tail_tokens: 20000`，
> 注释「当前话题块 Token 保护安全阀」）**不参与尾部保护区**——其语义是**话题块
> token 保护**（强制切分过长主题块），由 `TOPIC_PEAK_TOKEN`（同样 20000，水位满压
> 最后防线，`topic_manager._apply_water_pressure` 消费）承接。`PROTECT_TAIL_TOKENS`
> 配置当前无消费点（死配置，语义已被 TOPIC_PEAK_TOKEN 取代）。
