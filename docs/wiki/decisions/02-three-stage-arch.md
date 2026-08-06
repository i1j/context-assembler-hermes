---
title: 三阶段架构（C/A/L → E/A/L+F）
slug: three-stage-arch
category: decision
date: "2026-05"
version_introduced: v4.3
affects: [管线架构]
status: 已取代（E-stage+F-stage 继承）
source_files: ["ca/__init__.py"]
---

## 触发条件

原始 CA 管线需要独立阶段处理：数据采集、上下文装配、后台补全。

## 备选方案

1. **同步全管线** — 串行执行，延迟累积
2. **异步全管线** — 复杂度高，状态管理难
3. **三阶段 C/A/L** — 阶段独立、可降级

## 选定

C-stage（异步采集）→ A-stage（装配）→ L-stage（后台守护）。v5.0：C→E（写即落盘），L→F（触发模型摘要）。v5.10：CE shell 注册。

## 影响

✅ 各阶段独立开发/测试/降级 | ❌ 命名切换增加理解成本
