---
title: L4 空闲精炼管线
slug: idle-refinement
category: architecture
version_introduced: v5.14
status: 设计完成（v2 reality 化，2026-08-07）
decisions: [idle-refinement]
depends_on: [l-stage, store, topic_summary, embedding, reality]
updated: 2026-08-07
source_files: ["ca/refinement.py", "ca/store.py", "ca/reality.py"]
---

## 问题

L2 reality merge + L3 graphify sync 仅在 session start 触发。累积的对话轮在会话间从未被精炼，导致：

- realities 可能包含冗余、矛盾、陈旧的信息（hdl/current_status/key_facts/goals）
- 单 strand reality 过碎（决策 41 生产库 53% 单 strand）无归并机制纠偏
- source 数据（Fct）与 reality 内容可能脱节
- 没有健康度评分来标记低质量 reality

## 设计方案

L4 是 CA 的空闲期自我维护管线，实现为插件级 `IdleRefinementDaemon`
（`ca/refinement.py`，由 plugin 在 session-start 懒启动），**不是**消息处理路径的一部分，
也不依附 LStageMixin（LStageMixin 仅保留引擎生命周期管理）。

**v2（2026-08-07）**：精炼对象为 `realities` 表 reality（决策 41 reality 化）。
核心职责从「内容精炼」扩展为「**归并审查（宁并不分）+ 内容精炼 + 一致性维护**」。

### 触发

- 休眠 `REFINEMENT_CHECK_INTERVAL`（默认 120 秒）后检查触发条件
- 条件 A：`(全量 session 的 max_turn 之和 - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS`
- 条件 B（v2）：单 strand reality 数 >= `REFINEMENT_SINGLE_STRAND_TRIGGER`（过碎信号）
- 达到任一阈值 → 执行一轮精炼；未达到 → 继续休眠

### Phase 1 精炼任务（v2）

| # | 任务 | 类型 | 说明 |
|---|------|------|------|
| 1 | 并发会话保护 | 安全 | 跳过活跃 session 的 source 数据 |
| 2 | Reality 归并审查（Phase 2） | 4B 决策层 | 宁并不分：单 strand/同 session 拆裂/向量预筛候选 → 承接判定 → s2r 重映射 |
| 3 | 内容层详情重生成 | 4B 内容层 | 仅 affected reality，分批 ≤10，hdl/current_status 与全成员同步 |
| 4 | Reality 内精炼 | 4B | 1 次 4B per reality：去冗余、纠错、合并 |
| 5 | Fct↔Reality 交叉验证 | 4B | 重读 Fct → 4B 对比 → 修正不一致 |
| 6 | 僵尸清理 | 规则 | 空 centroid → 重 embed；死 source → 移除 |
| 7 | 健康评分 | 规则 | 综合评分 → 低分标记 `flagged_for_review` |
| 8 | Graphify 增量同步 | 脚本 | 如有改动 → sync_realities_to_graph |

### 关键铁律（2026-08-05 手工精炼轮 179→140→111 经验）

- **宁并不分**：精炼轮与生成阶段相反——纠过碎，边界模糊倾向合并；仅同领域不同任务不并
- **两阶段重构**：决策层（紧凑 ref + s2r 输出）+ 内容层（affected 分批详情）；决策层全量超 context → 向量粗筛 top-30
- **goals 承接锚**：member_count≥6 只接受明确承接 goals 的 strand（防大 reality 吞新 strand）
- **合并后必须详情重生成**：hdl/current_status 与全成员同步；timeline 代码维护（旧 hdl append，勿信 LLM timeline）
- **member 权威 = strand_to_reality 反推**；执行合并含被并入 reality 重算为空再删（勿漏）

## 存储

- `refinement_meta` 表（ca_topics.db）：记录每轮精炼信息（v2 新增 entries_merged/s2r_remapped 字段）
- `realities` 表（决策 41 schema 已含）：`health_score`、`flagged_for_review`、`topic_count`、`reviewed_at`、`last_reviewed_turn`

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REFINEMENT_ENABLED` | False | 总开关（默认停用；启用需显式设置） |
| `REFINEMENT_CHECK_INTERVAL` | 120s | 守护线程休眠间隔 |
| `REFINEMENT_MIN_NEW_TURNS` | 50 | 新对话轮触发阈值（≈2× 平均会话长度） |
| `REFINEMENT_SINGLE_STRAND_TRIGGER` | 20 | v2 过碎信号触发阈值 |
| `REFINEMENT_MAX_DURATION` | 300s | 单轮最大耗时 |
| `REFINEMENT_MERGE_REVIEW` | False | v2 归并审查开关（Phase 2） |
| `REFINEMENT_MAX_MERGES_PER_CYCLE` | 3 | v2 单轮归并数上限 |
| `REFINEMENT_MAX_ENTRIES_PER_CYCLE` | 5 | 单轮最多精炼 reality 数 |

详见：[34-空闲精炼管线（v2 reality 化）](../decisions/34-idle-refinement.md)
