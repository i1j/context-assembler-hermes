---
title: L4 空闲精炼管线
slug: idle-refinement
category: architecture
version_introduced: v5.14
status: 设计完成
decisions: [idle-refinement]
depends_on: [l-stage, store, topic_summary, embedding]
updated: 2026-07-30
source_files: ["ca/lstage.py", "ca/store.py"]
---

## 问题

L2 wiki merge + L3 graphify sync 仅在 session start 触发。累积的对话轮在会话间从未被精炼，导致：

- topic_wiki entry 可能包含冗余、矛盾、陈旧的信息
- 没有机制去纠正或合并大 entry
- source 数据（Fct）与 entry 内容可能脱节
- 没有健康度评分来标记低质量 entry

## 设计方案

L4 是 CA 的空闲期自我维护管线，实现为插件级 `IdleRefinementDaemon`
（`ca/refinement.py`，由 plugin 在 session-start 懒启动），**不是**消息处理路径的一部分，
也不再依附 LStageMixin（LStageMixin 仅保留引擎生命周期管理）。

### 触发

- 休眠 `REFINEMENT_CHECK_INTERVAL`（默认 120 秒）后检查触发条件
- 条件：`(全量 session 的 max_turn 之和 - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS`
- 达到阈值 → 执行一轮精炼；未达到 → 继续休眠

### Phase 1 精炼任务

| # | 任务 | 类型 | 说明 |
|---|------|------|------|
| 1 | 并发会话保护 | 安全 | 跳过活跃 session 的 source 数据 |
| 2 | Entry 内精炼 | 4B | 1 次 4B per entry：去冗余、纠错、合并 |
| 3 | Fct↔Wiki 交叉验证 | 4B | 重读 Fct → 4B 对比 → 修正不一致 |
| 4 | 僵尸清理 | 规则 | 空 centroid → 重 embed；死 source → 移除 |
| 5 | 健康评分 | 规则 | 综合评分 → 低分标记 `flagged_for_review` |
| 6 | Graphify 增量同步 | 脚本 | 如有改动 → 触发 wiki_to_graph.py |

## 存储

- `refinement_meta` 表（ca_topics.db）：记录每轮精炼信息
- `topic_wiki` 新增字段：`health_score`、`flagged_for_review`、`topic_count` 等

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REFINEMENT_ENABLED` | False | 总开关（决策 36 起默认停用；启用需显式设置） |
| `REFINEMENT_CHECK_INTERVAL` | 120s | 守护线程休眠间隔 |
| `REFINEMENT_MIN_NEW_TURNS` | 50 | 新对话轮触发阈值（≈2× 平均会话长度） |
| `REFINEMENT_MAX_DURATION` | 300s | 单轮最大耗时 |
| `REFINEMENT_MAX_ENTRIES_PER_CYCLE` | 5 | 单轮最多精炼条目数 |

详见：[34-空闲精炼管线](../decisions/34-idle-refinement.md)
