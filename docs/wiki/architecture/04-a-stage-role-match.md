---
title: A-stage 角色队列匹配
slug: a-stage-role-match
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "tail-protection", "bg-review-sync", "incremental-cache"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-18
---

## 问题

A-stage 负责在 `pre_llm_call` 中将历史上下文装配为最终的 prompt 文本。旧方案使用三区模型（active/retrieval/fallback）+ 简单的替換规则，无法满足以下需求：

- 不同话题应使用不同粒度的摘要
- 尾巴保留原文不应被替换
- 角色队列应匹配而不是简单替换

## 决策

### 备选方案

1. **旧三区模型**（active=尾部保留, retrieval=检索区, fallback=降级区）— 与 topic 无关，缺乏灵活性
2. **三层替换**（L2=保留Elm, L1=全文替换Fct, L0=150ch截断）— 需要与 topic 联动
3. **topic-aware 角色队列匹配 + grade 驱动三级替换（选定）**

### 选定方案

核心逻辑在 `_on_pre_llm_call_v5` 中按角色分队列后逐行替换：

```
角色队列
  ┌──────────┐    ┌───────────┐    ┌──────────┐
  │ user 队列│    │assistant  │    │ tool 队列 │
  │          │    │ 队列      │    │          │
  │ role=user│    │role=ast   │    │role=tool │
  └────┬─────┘    └─────┬─────┘    └────┬─────┘
       │                │               │
       └────────────────┼───────────────┘
                        │
                        ▼
              +-----------------+
              | 1:1 按角色匹配   |
              +-----------------+
                        │
               ┌────────┴────────┐
               ▼                 ▼
        ┌─────────────┐   ┌─────────────┐
        │ 有 Grade     │   │ 无 Grade    │
        │ get_turn_grade│   │ 默认 Elm    │
        └──────┬──────┘   └──────┬──────┘
               │                 │
        ┌──────┴──────┐          │
        ▼  ▼  ▼       ▼          │
     L2  L1  L0   tail           │
    保留 全文  150ch  保留         │
    Elm  Fct  截断   Elm          │
        │                 │       │
        └──────┬──────────┘       │
               │                  │
               ▼                  ▼
        ┌──────────────────────────┐
        │    装配输出文本          │
        └──────────────────────────┘
```

**Grade 驱动三级替换**：
- `Grade.ELM`（L2）：保留 Elm 原文
- `Grade.FCT`（L1）：替换为 Fct 全文
- `Grade.HDL`（L0）：截断 150ch

**尾巴保护优先**：protect_tail 内的 turn 强制走 ELM，不受 grade 影响。

**reasoning_content 清理**：assistant 行的 `reasoning_content` 在 A-stage 主动 pop，避免思维链泄露到上下文。

### 实现要点

- 角色队列：user/assistant/tool 三类独立队列
- 按 role 一一对应匹配，多余的丢弃，不足的保留 Elm
- 函数位于 `ca/__init__.py` 的 `_on_pre_llm_call_v5`

## 数据验证

关闭插件对比：
```bash
# 开启 CA：查看 Fct 替换效果
SELECT turn, role, 
       CASE WHEN Fct != '' AND Fct != content THEN '已替换为Fct' 
            WHEN Elm != '' THEN '保留Elm' 
            ELSE '无数据' 
       END AS replacement_status
FROM turn_stream 
WHERE _assemble_status > 0
ORDER BY turn;
```

## 优点

- topic-aware：不同话题使用不同 grade
- 尾巴保护保留关键上下文
- reasoning_content 清理防止思维链泄漏

## 约束 / 已知问题

- 角色队列匹配依赖于 Fct 已经生成，未生成时走 Fct-pending 防护
- grade 判定在 topic_manager 中，依赖话题分割的准确性
