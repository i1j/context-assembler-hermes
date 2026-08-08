# CA 测试-调试记录索引

> CA 项目 testing/ 目录入口 — 测试体系、环境配置与调试记录的统一索引。
> 权威本地源：`~/.hermes/profiles/tester/plugins/ca_assembler/docs/` + `tests/`

## 测试体系

| 文档 | 内容 | 位置 |
|------|------|------|
| [overview.md](overview.md) | 测试总览：分层架构、当前状态（826 collected = 824 passed / 1 skipped / 1 xfailed）、缺口登记 | 本目录 |
| [13-test-strategy.md](13-test-strategy.md) | 完整测试策略：分层测试体系、系统级不变量 | 本目录 |
| [test-env.md](../test-env.md) | 测试环境配置：路径结构、依赖、运行命令（`/usr/bin/python3 -m pytest tests/ -q`） | 项目根 |
| `tests/INDEX.md` | 决策点 ↔ 测试文件对照矩阵、缺口登记、覆盖统计 | 本地仓库 |

## 调试验证记录（稳定经验，已入 OV）

| 文档 | 内容 | 状态 |
|------|------|------|
| [debug-20260612-verification-report.md](debug-20260612-verification-report.md) | v5.1 出口记录验证（bg_review + 20K 三区行为） | 历史存档（v5.10 后 `_mutation_mode` 已移除） |
| [20260610-ca-mutation-thinking-rootcause.md](../architecture/20260610-ca-mutation-thinking-rootcause.md) | CA Mutation 思维问题根因分析（四层失效：超限/映射 bug/快速插入/汇编对策） | 有效 |
| [test-system-refactoring-v5.0.md](test-system-refactoring-v5.0.md) | v5.0 测试系统重构设计 | 有效 |

## 调试记录（原始发现，存本地项目文件夹）

> 按 tester 存储约定（`store-findings-convention`）：调试记录**优先项目文件夹**，OV 仅收录稳定后的经验。以下为本地下游调试记录，不入 OV 持久库，经 session_search 追溯。

| 记录 | 本地路径 |
|------|----------|
| 任务书（Codex 修复任务） | `docs/fix-task-20260807.md` / `docs/fix-task-20260808.md` / `docs/fix-task-20260808-r3-refinement.md` |
| Codex 审计记录 | `docs/audit-codex-20260807.md` / `docs/audit-codex-20260808.md` |
| 代码状态快照 | `docs/current-code-state-2026-08-06.md` / `docs/current-code-state-2026-08-07.md` |
| 测试改进计划（P0） | `docs/P0-ca-test-conftest-improvement-plan.md`（conftest 收窄 + 去 autouse mock） |
| 变更日志 | `docs/changelog.md` |

## 维护约定

1. **统计基线**：每次全量回归后更新 `overview.md` 与 `13-test-strategy.md` 的测试数字（当前实测命令：`/usr/bin/python3 -m pytest tests/ -q -p no:cacheprovider`）。
2. **调试记录**：新调试/任务记录写本地 `docs/`（`fix-task-YYYYMMDD.md` / `audit-*.md`），不直接入 OV；稳定后的排障经验（根因分析、验证报告）可同步到 `architecture/` 并在本索引登记。
3. **环境性失败**：沙箱/受限环境下 `tests/store/test_embedding.py` 2 个（Ollama 不可达）+ `tests/plugin/test_plugin.py` 1 个（profile ca_cache 写入受限）会失败，不计入基线。
