---
title: L-stage 守护线程 → F-stage per-fin 触发模型
slug: l-stage-daemon
category: decision
date: "2026-06"
version_introduced: v4.4
version_deprecated: v5.10
affects: [l-stage]
status: 已取代（v5.10 被 process_turn_f_stage 取代）
source_files: ["ca/lstage.py"]
---

## 触发条件
F-stage 异步摘要可能延迟或失败。需要独立的补全机制确保所有 turn 有摘要。

## 备选方案
1. **同步摘要** — 阻塞用户路径
2. **后台轮询守护线程** — 独立于主路径运行
3. **触发式 per-fin daemon 线程（v5.10 后选定）** — fin 粒度精确控制

## v4.4~v5.9 状态
独立守护线程。对话/工具双独立线程。限速 2/s 对话 5/s 工具。3 次失败→永久跳过（`_assemble_status=2`）。

## v5.10 取代说明
该机制被 F-stage per-fin 触发模型 (`process_turn_f_stage` in `ca/__init__.py:208`) 完全取代。L-stage 当前仅负责引擎生命周期 (`lstage.py`: `reset()`, `wait_for_pending()`)。
