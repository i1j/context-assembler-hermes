# 已知差距索引

> 设计决策中明确知悉但未完全消除的风险、未实装功能和测试空白。
> v5.10 已关闭 GAP-2（尾保护区被 v5.10 「倒数第2个user轮」方案替代）。
> GAP-1~GAP-6 为架构/代码质量问题；GAP-7~GAP-9 为测试体系空白；GAP-10 为分支管理问题。

---

## 架构/代码质量

| ID | 问题 | 状态 |
|----|------|------|
| [[GAP-1]] | `_available_budget()` 预算未传入组装管线 | ⚠️ 未修复 |
| [[GAP-2]] | `_compute_tail_start()` 旧方案 — ✅ v5.10 已替代 | ✅ 已关闭 |
| [[GAP-3]] | tool_group l2_tokens 列未写入 | ⚠️ 未写入 |
| [[GAP-4]] | write_turns_batch 无显式 rollback (SQLite auto) | ⚠️ 已知设计取舍 |
| [[GAP-5]] | `_parse_bool_env` 非 fail-safe | ⚠️ 调用方传 True 已满足 |
| [[GAP-6]] | 无 `__all__`, 无 `from __future__` in stats/health | ⚠️ 代码规范 |

## 测试体系空白

| ID | 问题 | 状态 |
|----|------|------|
| [[GAP-7]] | 核心模块 cache/retrieval/SessionManager 零直接单元测试 | ⚠️ 空白 |
| [[GAP-8]] | 空壳测试文件 (test_concurrency.py 等) | ⚠️ 空壳 |
| [[GAP-9]] | 端到端测试全部 mock LLM → 真实调用从未验证 | ⚠️ 空白 |

## 分支/部署

| ID | 问题 | 状态 |
|----|------|------|
| [[GAP-10]] | v5.1 `_format_tool_group_assembly` 实现在 tester profile 独立副本 | ⚠️ 待统一 |

---

**总差距项:** 10
**父节点:** [[R-000]]
