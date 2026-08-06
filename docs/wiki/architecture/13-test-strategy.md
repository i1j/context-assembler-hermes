---
title: 测试策略
slug: test-strategy
category: architecture
version_introduced: v5.0
status: 已实装
decisions: []
depends_on: []
updated: 2026-07-26
source_files: ["tests/"]
---

## 问题

CA 插件需要覆盖从 Hook 接收到 DB 写入到 conv_history 装配的完整链路。测试需要分层以平衡定位速度和覆盖广度。

## 分层测试体系

| 层 | 分类 | 覆盖内容 |
|----|------|----------|
| Stage 测试 | E-stage 写契约 | Hook 字段写入、幂等性、行不可变 |
| | F-stage 摘要 | fin 粒度触发、LLM 降级链 |
| | A-stage conv_history | 三区降级、尾巴保护、FAR 行删除 |
| Unit 测试 | 工具摘要 | 四级优先级、10 Handler |
| | 检索 | BM25 检索、RRF 排序 |
| | 缓存 | 冷启动 warmup、指纹去重 |
| 集成测试 | 端到端 | Hook→DB→A-stage 全链路 |

## 不变量测试

核心假设（系统级不变量）必须有独立测试覆盖：

- turn_stream 行不可变（写入后不会被修改）
- E-stage 幂等（同一数据多次写入结果一致）
- CE 管线不修改 state.db（方向 B 约束）
- bg_review 跳过所有 CA 处理

## 统计（v5.10）

416 个测试用例，分布在 `tests/stage/`、`tests/unit/`、`tests/integration/`。
