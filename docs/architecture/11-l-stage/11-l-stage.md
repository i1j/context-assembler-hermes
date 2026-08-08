---
title: L-stage 引擎生命周期管理 + L4 空闲精炼
slug: l-stage
category: architecture
version_introduced: v4.4→v5.14
status: 已实装
decisions: [l-stage-daemon, idle-refinement]
depends_on: [store, f-stage, realities]
updated: 2026-07-30
source_files: ["ca/lstage.py"]
---

## 问题

F-stage 异步摘要（per-fin daemon 线程）在引擎重置或会话切换时需要统一管理。需要生命周期方法清缓存、等待 pending 任务、重建索引。

同时，L2 reality merge + L3 graphify sync 仅在 session start 触发一次。累积的对话轮在会话间从未被精炼，导致 realities 中的内容可能冗余、矛盾、陈旧（v7 推迟项，v2 起 L4 承担归并审查）。

## 设计方案

### 引擎生命周期

- **`reset()`**：清缓存（`cache.destroy()`）、等待所有 F-stage 线程完成（`wait_for_pending(5.0)`）、重建 cache、重置 stats 和 turn_counter
- **`wait_for_pending(timeout=30.0)`**：遍历 `_pending_tasks` 字典，join 每个 daemon 线程

### L4 空闲精炼管线（v5.14 新增，v2 reality 化 2026-08-07）

L4 是插件级 **`IdleRefinementDaemon`**（`ca/refinement.py`，daemon=True，由 plugin
在 session-start 懒启动），在上次精炼以来的跨会话新对话轮数达到阈值后启动精炼轮次。
实现不在 LStageMixin（`ca/lstage.py` 仅引擎生命周期管理）。

**触发条件**：
- A：`(全局所有 session 的 turn_stream 最大轮次之和 - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS`
- B（v2）：单 strand reality 数 >= `REFINEMENT_SINGLE_STRAND_TRIGGER`（过碎信号）

**精炼任务（v2，对象 = realities 表）**：
1. 并发会话保护（跳过活跃 session 的 source 数据）
2. Reality 归并审查（v2 核心，宁并不分：单 strand/同 session 拆裂/向量预筛/跨块 hdl 相同/graphify 社区 五源候选 → 承接判定 → s2r 重映射）
3. 内容层详情重生成（仅 affected，分批 ≤10，hdl/current_status 与全成员同步，timeline 代码维护）
4. Reality 内精炼（1 次 4B per reality：去冗余、纠错、矛盾合并）
5. Fct↔Reality 交叉验证（重读 Fct → 4B 对比一致性）
6. 僵尸清理（空 centroid / 死 source）
7. Reality 健康评分 + 自动标记 `flagged_for_review`
8. 知识子图全量重建（v2.3：reality 权威，清僵尸/悬挂边，重建后社区发现供 Step 2e）
9. 事实关联建边（v2.4→v2.6：shares_topic bigram / depends_on·continues 承接判定 / references_ov reality↔OV 项目数据 → 知识图谱关联层；代码关联不建边，权威源是 OV 文档 frontmatter；**数据修复前置**：Step 1.5/4/6/7 先修数据，Step 8/8.5 后建边）

**配置**：`REFINEMENT_*` 族（`ca/config.py`），含 `CHECK_INTERVAL`、`MIN_NEW_TURNS`、
`SINGLE_STRAND_TRIGGER`、`MAX_ENTRIES_PER_CYCLE`、`MERGE_REVIEW`、`MAX_MERGES_PER_CYCLE` 等。

### 数据流

```
# 生命周期（v5.10 前）
BackfillThread → 轮询未摘要行 → 写 Fct/Hdl
                                    ↑
LStageMixin.reset() ── wait_for_pending() ── join all threads

# + L4 空闲精炼（v5.14）
LStageMixin
  ├── reset() / wait_for_pending()     ← 继承
  ├── start_idle_refinement()          ← 新增：启动 daemon 守护线程
  │   └── _idle_refinement_loop()
  │       ├── 休眠 (CHECK_INTERVAL)
  │       ├── 计算新对话轮数 / 单 strand 过碎信号
  │       ├── 达到阈值 → 执行精炼轮次
  │       │   ├── 归并审查 ──→  决策层承接判定 + s2r 重映射（宁并不分）
  │       │   ├── 详情重生成 ──→  affected reality 分批 ≤10，timeline 代码维护
  │       │   ├── 内精炼  ──→  update_reality + 重 embed
  │       │   ├── 交叉验证 ──→  修正不一致
  │       │   ├── 僵尸清理 ──→  清 empty centroid / 死 source
  │       │   ├── 健康评分 ──→  标记 flagged_for_review
  │       │   └── 知识子图重建（v2.3：reality 权威全量，清僵尸/悬挂边）
  │       └── 未达阈值 → 继续休眠
  └── stop_idle_refinement()           ← 新增：clean 退出
```

### 历史

L-stage 在 v4.4.0 最初设计为**双独立后台守护线程**（`BackfillThread`），轮询 `_assemble_status=1` 的未摘要行，含对话/工具双线程 + 限速 2/s+5/s + 3 次失败跳过。v5.10 后该机制被 **F-stage per-fin 触发模型**（`process_turn_f_stage` 启动 daemon 线程）完全取代。

v5.14 新增 **L4 空闲精炼管线**，作为独立守护线程实现于 `ca/refinement.py`
（`IdleRefinementDaemon`），专注 realities 的自我维护（v2 起，决策 41 reality 化）。

详见：[34-空闲精炼管线](../../decisions/34-idle-refinement/34-idle-refinement.md)
