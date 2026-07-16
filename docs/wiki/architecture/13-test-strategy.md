---
title: 测试策略
slug: test-strategy
category: architecture
version_introduced: v5.0
status: 已实装
decisions: []
depends_on: []
updated: 2026-06-23
---

## 问题

CA 插件包含 416 个测试用例（v5.10 重构后），分布在不同模块中。需要清晰的测试分类和策略，以确保核心路径可验证、回归不遗漏。

## 决策

### 备选方案

1. **全集成测试** — 验证完整链路，但定位慢、执行时间长
2. **纯单元测试** — 快速，但覆盖不到 Hook 间交互
3. **3 阶段分层 + topic 集成 + 其他（选定）**

### 选定方案

**测试分类与统计**（当前 416 个测试）：

| 分类 | 文件 | 数量 | 覆盖内容 |
|------|------|------|----------|
| E-stage 测试 | `tests/stage/test_e_stage.py` | ~20 | 写契约、Hook 字段、幂等性、行不可变 |
| F-stage 测试 | `tests/stage/test_f_stage.py` | ~31 | LLM 摘要、截断回退、故障隔离、Fct 覆盖 |
| A-stage 测试 | `tests/stage/test_a_stage.py` | ~15 | 角色队列匹配、增量缓存、快照可逆 |
| A-stage topic 测试 | `tests/stage/test_a_stage_topic_aware.py` | ~20 | ACT/REL/FAR grade 驱动替换 |
| Fct 解析测试 | `tests/parse/test_parse_v1.py` | ~24 | PAIR_PATTERN、截断检测、所有格式场景 |
| 话题管理测试 | `tests/unit/test_topic_manager.py` | ~99 | Jaccard、强制短语、半径定级、全管线 |
| 存储层测试 | `tests/store/test_store_v5.py` | ~6 | 列契约、session_id |
| 嵌入测试 | `tests/store/test_embedding.py` | ~6 | 嵌入缓存、降级 |
| 插件测试 | `tests/plugin/test_plugin.py` | ~35 | 生命周期、断路器、bg_review |
| 审计测试 | `tests/audit/` | ~48 | 交叉验证、DOA 自防御、dump 监测 |
| 配置测试 | `tests/config/` | ~17 | 配置加载、健康检测 |

**目录重组**（v5.10）：根目录独立测试已合并入 stage/、parse/ 目录。conftest 拆分为 tests/fixtures/ 子包。

**已填补**：`TopicGradeManager` 现有 99 个独立单元测试（TestGradeTopicsByRadius 11 个、TestDetect 8 个、TestGradeOnSwitch 6 个等）。

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

- 测试运行依赖 Hermes 环境，不能在孤立环境下执行
- 部分测试使用 `conftest.py` 的 fixture，修改 fixture 可能影响大量测试
