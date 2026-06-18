---
title: 测试策略
slug: test-strategy
category: architecture
version_introduced: v5.0
status: 已实装
decisions: []
depends_on: []
updated: 2026-06-18
---

## 问题

CA 插件包含 261 个测试用例，分布在不同模块中。需要清晰的测试分类和策略，以确保核心路径可验证、回归不遗漏。

## 决策

### 备选方案

1. **全集成测试** — 验证完整链路，但定位慢、执行时间长
2. **纯单元测试** — 快速，但覆盖不到 Hook 间交互
3. **3 阶段分层 + topic 集成 + 其他（选定）**

### 选定方案

**测试分类与统计**（当前 261 个测试）：

| 分类 | 文件 | 数量 | 覆盖内容 |
|------|------|------|----------|
| E-stage 测试 | `test_estage.py` | ~60 | 写入协议、Hook 分发、字段完整性 |
| F-stage 测试 | `test_fstage.py` | ~50 | 异步 LLM 调用、结果回写、daemon 生命周期 |
| A-stage 测试 | `test_astage.py` | ~70 | 角色队列匹配、grade 驱动替换、尾巴保护 |
| Topic 测试 | `test_v521_topic.py` | ~30 | 话题分割集成、TopicGradeManager |
| Config/Health 测试 | `test_config.py`, `test_health.py` | ~15 | 配置加载、健康检测 |
| 其余 | `test_circuit.py`, `test_embedding.py`, `test_degradation.py`, `test_lifecycle.py`, `test_parse_v1.py` | ~36 | 断路器、嵌入服务、降级、生命周期、Fct 解析 |

**死测试清理**（v5.7）：已删除 46 个指向已移除代码的测试（`a_planner`、`a_injector`、`compute_topic_groups` 等），测试集从 ~307 减少至 261。

**已知缺失**：`TopicGradeManager` 无独立单元测试（仅通过集成测试覆盖）。

## 数据验证

```bash
# 运行完整 CA 测试套件
cd ~/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/ -q --tb=short 2>&1 | tail -5

# 查看分类统计
python -m pytest tests/ -q --tb=short --collect-only 2>&1 | tail -3
```

## 优点

- 分层覆盖：E→F→A 逐阶段验证
- 集成测试覆盖 topic 真实场景
- 死测试清理后测试集可维护

## 约束 / 已知问题

- `TopicGradeManager` 无独立单元测试
- 测试运行依赖 Hermes 环境，不能在孤立环境下执行
- 部分测试使用 `conftest.py` 的 fixture，修改 fixture 可能影响大量测试
