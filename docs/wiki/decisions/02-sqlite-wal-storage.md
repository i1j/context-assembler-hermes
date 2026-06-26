---
title: SQLite + WAL 持久化选型
slug: sqlite-wal-storage
category: decision
date: "2026-05"
version_introduced: v0.x
alternatives: ["PostgreSQL（太重）", "Redis（内存不够）", "sqlite-vec（依赖二进制兼容）"]
chosen: "SQLite + WAL + busy_timeout + 后台 checkpoint"
affects: ["01-storage-model"]
status: 已实装
---

## 触发条件

需要持久化存储对话轮数据，支持并发读写、崩溃恢复。

## 备选方案

1. **SQLite + WAL（选定）** — 零配置、WAL 模式支持读写并发
2. **PostgreSQL** — 需外部服务，运维成本高
3. **Redis** — 内存有限，重启丢失
4. **sqlite-vec** — 二进制兼容性问题，最终在 v4.x 弃用

## 选定

- `WAL` 模式 + `busy_timeout=5000` + 后台 checkpoint 守护线程
- 线程本地连接，空闲超时自动关闭
- 写入重试（指数退避）
- 批量写入单事务
- 表结构完整性检查，损坏自动重建

旧决策节点：`S-001`, `S-001a`, `S-002`, `S-003`, `S-004`, `S-005`

## 之前 vs 之后

**之前**：无持久化，全部在内存中
**之后**：每次写入立即落盘，WAL 模式支持并发读写

## 影响

✅ 零配置：无需外部数据库服务
✅ WAL 模式：读不阻塞写
✅ 崩溃恢复：WAL 自动恢复
❌ 并行写限制（SQLite 单写者）
❌ sqlite-vec 弃用后向量检索缺失
