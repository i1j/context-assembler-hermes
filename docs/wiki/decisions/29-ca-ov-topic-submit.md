---
title: CA-OV 话题提交
slug: ca-ov-topic-submit
category: decision
date: "2026-06-16"
version_introduced: v5.5
alternatives: ["同步提交（阻塞用户路径）", "不持久化（重启丢失）", "每次话题更新都提交（写压力大）"]
chosen: "fire-and-forget 异步提交 + 话题切换时触发"
affects: ["10-ca-ov-topic-submit"]
status: 已实装
---

## 触发条件

话题分割后的结构在 session 结束后丢失，需要持久化到 OV 实现跨 session 复用。

## 备选方案

1. **同步提交**：等待 OV 响应 → 阻塞用户路径
2. **不持久化**：重启丢失所有话题边界信息
3. **每次更新提交**：话题微调也触发 OV 写 → 压力大
4. **fire-and-forget + 切换触发（选定）**

## 选定

- fire-and-forget daemon 线程
- 触发时机：话题切换时（TopicGradeManager 检测到新话题）
- 提交内容：标题、形心、turn 范围、摘要
- 失败不重试，不影响主流程
