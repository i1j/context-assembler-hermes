---
title: L-stage 引擎生命周期管理 + L4 空闲精炼
slug: l-stage
category: architecture
version_introduced: v4.4→v5.14
status: 已实装
decisions: [l-stage-daemon, idle-refinement]
depends_on: [store, f-stage, topic_wiki]
updated: 2026-07-30
source_files: ["ca/lstage.py"]
---

## 问题

F-stage 异步摘要（per-fin daemon 线程）在引擎重置或会话切换时需要统一管理。需要生命周期方法清缓存、等待 pending 任务、重建索引。

同时，L2 wiki merge + L3 graphify sync 仅在 session start 触发一次。累积的对话轮在会话间从未被精炼，导致 topic_wiki 中的 entry 内容可能冗余、矛盾、陈旧（v7 推迟项）。

## 设计方案

### 引擎生命周期

- **`reset()`**：清缓存（`cache.destroy()`）、等待所有 F-stage 线程完成（`wait_for_pending(5.0)`）、重建 cache、重置 stats 和 turn_counter
- **`wait_for_pending(timeout=30.0)`**：遍历 `_pending_tasks` 字典，join 每个 daemon 线程

### L4 空闲精炼管线（v5.14 新增）

L4 是插件级 **`IdleRefinementDaemon`**（`ca/refinement.py`，daemon=True，由 plugin
在 session-start 懒启动），在上次精炼以来的跨会话新对话轮数达到阈值后启动精炼轮次。
实现不在 LStageMixin（`ca/lstage.py` 仅引擎生命周期管理）。

**触发条件**：`(全局所有 session 的 turn_stream 最大轮次之和 - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS`

**Phase 1 精炼任务**：
1. 并发会话保护（跳过活跃 session 的 source 数据）
2. Entry 内精炼（1 次 4B per entry：去冗余、纠错、矛盾合并）
3. Fct↔Wiki 交叉验证（重读 Fct → 4B 对比一致性）
4. 僵尸清理（空 centroid / 死 source）
5. Entry 健康评分 + 自动标记 `flagged_for_review`
6. 如有改动 → 触发 L3 graphify 增量同步

**配置**：`REFINEMENT_*` 族（`ca/config.py`），含 `CHECK_INTERVAL`、`MIN_NEW_TURNS`、`MAX_ENTRIES_PER_CYCLE` 等。

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
  │       ├── 计算新对话轮数
  │       ├── 达到阈值 → 执行精炼轮次
  │       │   ├── 内精炼  ──→  upsert_wiki_entry + 重 embed
  │       │   ├── 交叉验证 ──→  修正不一致
  │       │   ├── 僵尸清理 ──→  清 empty centroid / 死 source
  │       │   ├── 健康评分 ──→  标记 flagged_for_review
  │       │   └── graphify 增量同步
  │       └── 未达阈值 → 继续休眠
  └── stop_idle_refinement()           ← 新增：clean 退出
```

### 历史

L-stage 在 v4.4.0 最初设计为**双独立后台守护线程**（`BackfillThread`），轮询 `_assemble_status=1` 的未摘要行，含对话/工具双线程 + 限速 2/s+5/s + 3 次失败跳过。v5.10 后该机制被 **F-stage per-fin 触发模型**（`process_turn_f_stage` 启动 daemon 线程）完全取代。

v5.14 新增 **L4 空闲精炼管线**，作为独立守护线程实现于 `ca/refinement.py`
（`IdleRefinementDaemon`），专注 topic_wiki 的自我维护。

详见：[34-空闲精炼管线](../decisions/34-idle-refinement.md)
