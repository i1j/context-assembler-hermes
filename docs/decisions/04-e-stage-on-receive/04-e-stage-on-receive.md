---
title: E-stage 写即落盘
slug: e-stage-on-receive
category: decision
date: "2026-06"
version_introduced: v5.0
affects: [e-stage]
status: 已实装
source_files: ["ca/e_stage.py"]
---

## 触发条件

CA 需要在消息到达时立即写入 turn_stream。延迟写入导致运行中会话恢复困难。

## 备选方案

1. **on_session_end 批量写** — 延迟可见性，无法运行中恢复
2. **ToolBuffer 缓冲写入** — buffer→flush 增加复杂度
3. **5 Hook 各写其责** — 立即写入，无 buffer

## 选定

5 Hook 各自写特定 (turn,seq) 行和字段。无 buffer，无回滚。Tool 占位行 status='pending'。

## 影响

✅ 数据实时可见，会话恢复完整 | ❌ 无撤销机制
