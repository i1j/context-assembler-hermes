---
title: A-stage 角色等级装配（v6 方向 B）
slug: a-stage-role-match
category: architecture
version_introduced: v6.0
status: 已实装
decisions: ["topic-grade-manager", "tail-protection", "bg-review-sync"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-28
source_files: ["ca/a_stage.py", "topic_manager.py"]
---

## 问题

A-stage 负责将历史上下文装配为最终的 prompt 文本。v5 使用 topic-aware 三级替换（`_simple_mutation_mode_v5`），
在 Hermes 的 messages 列表上**原地修改**（mutation），存在以下问题：

- mutation 破坏了 Hermes 原始消息，需要 `_full_backup` / `_saved_history_snapshot` 来回退
- 增量缓存（`_A_stable_cache`）增加了复杂度且与话题切换联动脆弱
- Fct pending 防护需要额外的 stale 标记和回退逻辑
- mutation 修改了 state.db 的原始数据，post_llm_call 必须恢复

v6 采用**方向 B**：完全摒弃 mutation，**从 turn_stream DB 重建 conv_history**。

## 决策

### 备选方案

1. **Mutation（v5 方案）** — 原地修改 messages，备份恢复 — ❌ 复杂度高
2. **方向 A** — CE 从 Hermes messages 构造 conv_history — 仍依赖 Hermes 数据格式
3. **方向 B（选定）** — 完全从 CA 自己的 turn_stream DB 重建，不与 Hermes messages 交织

### 选定方案

核心实现：`ca/a_stage.py` 的 `AStageMixin._build_conv_history_v6(topic_mgr, system_message=None)`。

不再通过 CE `compress()` 入口对 messages 做原地 mutation。compress() 改为调用
`_build_conv_history_v6` 返回一个**新列表**，由 CE 管线替换传给 LLM 的 conv_history。

**三区模型 + 行类型降级**：

```
conv_history 结构（三区）：
  ┌─ 远区（FAR 话题）    → user/fin:Hdl,    thought/tool:删除
  ├─ 中区（REL 话题）    → user/fin:Fct,    thought/tool:Hdl
  ├─ 中区（ACT 话题）    → user/fin:Elm,    thought/tool:Fct
  └─ 尾部保护区（最后 2 轮 user） → 全 Elm（不降级）
```

| 位置（区域） | user/fin | thought/tool |
|---|---|---|
| 尾部保护区 | Elm（原文） | Elm（不降级） |
| ACT 区 | Elm | Fct（降一级） |
| REL 区 | Fct | Hdl（降一级） |
| FAR 区 | Hdl | 删除 |

**降一级规则**：thought/tool 行的内容等级比 user/fin 低一级。
尾部保护区内**不执行降级**，所有行保持 Elm。

**尾巴保护优先**：protect_tail 内的 turn（最后 2 个 user 轮）强制走 Elm，不受 grade 影响。

`reasoning_content` 在当前版本中不携带（同 v5 behavior）。

### 实现要点

- 数据源：`turn_stream` 表的 Fct/Hdl 列，纯 DB 读取
- 话题等级查询：`topic_mgr.get_turn_grade(turn)` 返回 `TopicGrade.{ACT,REL,FAR}`
- 尾部边界：扫描 conv_history 找倒数第 2 个 user turn 之后的所有行
- 保护区外：按区域定级 + thought/tool 行降一级，FAR thought/tool 行删除
- system_message：从 Hermes messages[0] 保留，拼接在 conv_history 头部
- 返回的新列表只包含干净的 LLM 输入格式（role + content 为主），不含 Hermes 内部字段

## 数据验证

```bash
# 查看 turn_stream 的各行 Fct/Hdl 覆盖率
SELECT turn, role,
       CASE WHEN Fct IS NOT NULL AND Fct != '' THEN '有 Fct' ELSE '缺 Fct' END AS fct_status,
       CASE WHEN Hdl IS NOT NULL AND Hdl != '' THEN '有 Hdl' ELSE '缺 Hdl' END AS hdl_status
FROM turn_stream
ORDER BY turn, seq;
```

## 优点

- 不再修改 Hermes 原始消息，消除 `_full_backup` / `_saved_history_snapshot` 复杂度
- turn_stream DB 是单源真相，每轮 compress 重新计算
- 无需增量缓存和 Fct pending 防护
- conv_history 结构可控（只含需要的字段）
- 简化 CE compress() 逻辑

## 缺点

- 每次 compress 都完整遍历 turn_stream DB（性能可接受，因 turn 数<1000）
- 放弃增量缓存意味着短轮次优势缩小（6+ 轮后 mutation 才有明显缓存优势）
- `_build_conv_history_v6` 需要 topic_mgr 有完整的 turn→topic 映射（否则退化到 ACT 保守策略）
