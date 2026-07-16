---
title: 命名统一
slug: naming-convention
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["stage-terminology-unification"]
depends_on: []
updated: 2026-06-23
source_files: ["ca/grade.py"]
---

## 问题

CA 系统演进过程中，同一个概念使用了多套术语：
- **级别术语**：L2/L1/L0（来自旧 Hermes compress）、原始数据/摘要/历元（来自设计文档）、Elm/Fct/Hdl（v5.5 新引入）
- **阶段术语**：C-stage/A-stage/L-stage（来自旧三阶段架构）、E-stage/F-stage/A-stage（v5.0 重构）
- **DB 列名**：`turn_cache` 使用 `l0/l1/elp`，`turn_stream` 使用 `Elm/Fct/Hdl`
- **代码常量**：部分使用 `L2` 字符串字面量，部分使用 Grade 枚举

术语不一致导致阅读代码和数据库时混淆，2026-06-17 用户多次纠正 L2/L1/L0 问题。

## 决策

### 备选方案

1. **中英混合** — 一次修改不到位，留下技术债
2. **保留 L2/L1/L0** — 用户反复纠正，不可接受
3. **全项目强制统一为 Elm/Fct/Hdl + E-stage/F-stage/A-stage（选定）**

### 选定方案

**统一术语表**：

| 旧术语 | 新术语 | 范围 |
|--------|--------|------|
| L2, 原始数据 | **Elm** | DB 列名、Grade 枚举、代码变量 |
| L1, 摘要 | **Fct** | DB 列名、Grade 枚举、代码变量 |
| L0, 历元 | **Hdl** | DB 列名、Grade 枚举、代码变量 |
| C-stage | **E-stage**（写入阶段） | 代码、文档 |
| A-stage | **A-stage**（保留） | 代码、文档 |
| L-stage | **F-stage**（异步摘要阶段） | 代码、文档 |

**代码级别要求**：
- DB 列名：`turn_stream.Elm`, `turn_stream.Fct`, `turn_stream.Hdl`
- Grade 枚举：`Grade.ELM`, `Grade.FCT`, `Grade.HDL`（`topic_manager.py`）
- 不允许在代码和注释中出现 L2/L1/L0 字符串字面量
- 文档、注释、调试输出全部统一使用新术语

## 数据验证

```bash
# 检查代码中是否还残留旧术语（v5.10 已全部清理）
grep -rn 'ca\.l[012]\.\\|\bL[012]\b' ca/ --include='*.py' | grep -v '.pyc' || echo '无残留'
```

## 优点

- 3 字母短名：Elm/Fct/Hdl 在日志和 DB 中一目了然
- 层级清晰：Elm→Fct→Hdl 对应 raw→turn→epoch
- 与旧文档完全切割：不再有 L2/L1/L0 混淆

## 测试覆盖

- 术语一致性审计测试 — `tests/audit/`（审计 `ca.{fct|hdl}.{metric}` 日志键名格式）
- 残留旧术语检查 — 全局 grep 测试（`ca/` 中无 L2/L1/L0 残留）

## 约束 / 已知问题

- 旧数据中仍使用旧列名（`l0`/`l1`/`elp`），查询时需额外映射
- `Grade` 枚举值使用 `ELM=2, FCT=1, HDL=0`（保留了旧 L2/L1/L0 的整数值以便兼容）
- ~~部分注释和日志中可能仍有残留的 L2/L1/L0 未清理干净~~ ✅ v5.10 全部清理完毕
- CA-METRIC 日志键名审计测试强制 `ca.{fct|hdl}.{metric}` 格式，防止旧术语回归
