---
title: A-stage 角色队列匹配
slug: a-stage-role-match
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "tail-protection", "bg-review-sync", "incremental-cache"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-23
source_files: ["ca/a_stage.py", "ca/__init__.py", "topic_manager.py"]
---

## 问题

A-stage 负责在 `pre_llm_call` 中将历史上下文装配为最终的 prompt 文本。旧方案使用三区模型（active/retrieval/fallback）+ 简单的替换规则，无法满足以下需求：

- 不同话题应使用不同粒度的摘要
- 尾巴保留原文不应被替换
- 角色队列应匹配而不是简单替换

## 决策

### 备选方案

1. **旧三区模型**（active=尾部保留, retrieval=检索区, fallback=降级区）— 与 topic 无关，缺乏灵活性
2. **三层替换**（Elm=保留原文, Fct=全文替换, Hdl=截断）— 需要与 topic 联动
3. **topic-aware 角色队列匹配 + grade 驱动三级替换（选定）**

### 选定方案

核心逻辑分布在 `ca/a_stage.py`（`AStageMixin._simple_mutation_mode_v5` / `_incremental_mutation`——主力，含角色队列匹配、grade 驱动三级替换、尾巴保护、增量缓存）和 `topic_manager.py`（`TopicGradeManager`，话题等级定级）中。插件层 `plugins/ca_assembler/__init__.py` 做话题检测 + 调度委托。

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
      Elm  Fct Hdl   tail        │
     保留  全文 截断   保留        │
     content Fct[:150] content    │
        │                 │       │
        └──────┬──────────┘       │
               │                  │
               ▼                  ▼
        ┌──────────────────────────┐
        │    装配输出文本          │
        └──────────────────────────┘
```

**Grade 驱动三级替换**：
- `Grade.ELM`：保留 content 原文
- `Grade.FCT`：替换为 Fct 全文
- `Grade.HDL`：替换为 Hdl（截断 150 字符）

**尾巴保护优先**：protect_tail 内的 turn 强制走 ELM，不受 grade 影响。

**reasoning_content 清理**：assistant 行的 `reasoning_content` 在 A-stage 主动 pop，避免思维链泄露到上下文。

### 实现要点

- 角色队列：user/assistant/tool 三类独立队列
- 按 role 一一对应匹配，多余的丢弃，不足的保留 Elm
- 函数位于 `ca/a_stage.py`（`AStageMixin._simple_mutation_mode_v5` / `_incremental_mutation`）+ `topic_manager.py`（`TopicGradeManager` 话题定级）
- 增量缓存优化：`_A_stable_cache`（list[dict]）缓存稳定区已替换的 conv_hist 片段

## 数据验证

关闭插件对比：
```bash
# 开启 CA：查看 Fct 替换效果
SELECT turn, role,
       CASE WHEN Fct != '' THEN '已替换为Fct' 
            ELSE '无数据' 
       END AS replacement_status
FROM turn_stream 
WHERE role='user'
ORDER BY turn;
```

## 优点

- topic-aware：不同话题使用不同 grade
- 尾巴保护保留关键上下文
- 增量缓存避免每轮全量重组
- reasoning_content 清理防止思维链泄漏

## 约束 / 已知问题

- 角色队列匹配依赖于 Fct 已经生成，未生成时走 Fct-pending 防护（→ `_A_cache_is_stale=True`）
- grade 判定在 topic_manager 中，依赖话题分割的准确性
