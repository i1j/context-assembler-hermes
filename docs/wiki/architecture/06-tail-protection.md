---
title: 尾巴保护
slug: tail-protection
category: architecture
version_introduced: v5.3
status: 已实装
decisions: ["tail-protection"]
depends_on: ["storage-model", "a-stage-role-match"]
updated: 2026-06-18
---

## 问题

A-stage 装配时，最近几轮用户输入（对话尾部）是最关键的上下文——LLM 需要看到完整的用户最新输入。如果尾部也被 Fct 替换，将丢失关键信息，导致 LLM 响应偏离。

## 决策

### 备选方案

1. **不保护尾巴**（`protect_tail=0`）— 尾部也可能被替换，信息丢失
2. **按 token 计算尾巴边界** — 边界不稳定，容易被 Fct 长度波动影响
3. **按 assistant 行保护** — 最后几轮 assistant 输出，但用户最新输入更关键
4. **按 user 轮保护（选定）** — 固定最后 N 个 user 行保留 Elm

### 选定方案

- 配置参数 `protect_tail`（默认 2），表示保护最后 N 个 user 轮
- 检测方式：从 `turn_stream` 末尾向前扫描，找到最后 N 个 `role='user'` 的行
- 这些行即使有 Fct 也不替换，保留 Elm 原文
- **尾巴保护判定在 Grade 判定之前执行**（尾巴优先于 topic-grade）

### 实现要点

- `ca/__init__.py` 的 `_on_pre_llm_call_v5` 中，在遍历 turn 前先确定尾巴边界
- 尾巴内的 turn 无论 grade 判定结果如何，都强制保留 Elm
- 与 grade 交互：尾巴内 `get_turn_grade()` 返回强制 Grade.ELM

## 数据验证

```sql
-- 查看最后 3 个 user 行的替换状态
SELECT turn, role, content,
       CASE WHEN Fct != '' THEN '有Fct' ELSE '无Fct' END as has_fct,
       Elm
FROM turn_stream
WHERE role='user'
ORDER BY turn DESC
LIMIT 3;
```

## 优点

- 保护最关键的尾输入上下文
- 配置简单，默认可用
- 与 grade 系统的交互明确（尾巴优先）

## 约束 / 已知问题

- 固定轮数不灵活：短轮对话可能浪费保护 slot，长轮对话可能保护不够
- 当前不支持按 token 数量动态调整保护范围
