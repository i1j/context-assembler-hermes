# GAP-2: `_compute_tail_start()` 旧方案 — 已被 v5.10 替代

---
**父节点:** [[R-000]]
---

**来源:** WIKI

**问题（旧方案）:** `_compute_tail_start()` 20K token 尾区方案未实装，旧方案在 v4.4.0 基线运行中。

**v5.10 状态: ✅ 已替代**

v5.10 的 A-stage 用不同方式实现了尾保护区：
- 实现：`_simple_mutation_mode_v5` / `_incremental_mutation`（`ca/a_stage.py`）
- 策略：倒数第 2 个 user 消息之后 → 原文保留（不依赖 token 预算估算）
- 详见 [[TP-002]]（替换映射表）和 [[C-012+C-013]]

> 旧方案 `_compute_tail_start()` 仅为 Baseline A (v4.4.0) 问题。Baseline B (v5.10) 已用新机制取代。
