> ⚠️ **历史存档** — 此文档对应 v4.4.0+v5.10 代码基线，已被 `wiki/` 双维度 Wiki 取代。保留供架构溯源、版本间对比、理解"为什么不用方案 X"时查阅。
> **当前权威文档请访问**: `wiki/INDEX.md`（双维度 Wiki 入口）或 `wiki/architecture/`（14 页当前组件设计）+ `wiki/decisions/`（31 页 v0.x~v6.0 决策时间线）

# CA 决策树 v4.4.0+v5.10（文档-代码对照版）

> 每个节点 = 一个独立决策。子节点 = 父决策的细化/实现/约束。
> 对照文档: SRS v4.3.3+v4.4.0, Design v4.3.4+v4.4 supplement, code-review-v4.4.0
> 对照事故: ca-l1-fallback-investigation, ca-v4.3.2-incident-report
> 代码基线: projects/context-assembler (master=v4.4.0) + tester profile/plugins/ca_assembler (deploy=v6.0)

---

## 根节点 R-000: 设计哲学

Root: (无父节点)
Context: Hermes 内置 ContextCompressor 被动压缩导致上下文质量下降; Qwen3.5 thinking 无法关闭, JSON 输出<60%
Decision: 增量 Elm/Fct/Hdl 三级汇编（话题等级 ACT/REL/FAR 驱动注入）+ BM25/向量双路检索 + Token 预算闸门 + 全指纹去重。不依赖外部 AI/ML 库。
Evidence: changelog v0.1 (2026-05-16), SRS v4.3.3 §1.2
Children: R-001, R-002, R-003, R-004, INC-001, INC-002, INC-003, CR-001, CR-002, CR-003, CR-004, CR-005, CR-006, CR-007, P-006, TP-010

---

### R-001: 三阶段架构 (C/A/L) 替代双阶段

Parent: R-000
Decision: 任务拆为三个阶段 — C-stage(异步摘要生产), A-stage(同步上下文组装), L-stage(独立后台补全)
Evidence: changelog v4.3 (异步化), v4.4.0 (L-stage), Design v4.4 supplement
Children: C-001, C-010, C-019, P-001

---

## 根分支: C-stage (异步摘要生产)

### C-001: C-stage daemon thread 异步化

Parent: R-001
Decision: `process_turn_async` 创建 daemon thread → 立即返回 (≤50ms), 后台执行 LLM+解析+嵌入+写入
Context: LLM ~1-5s 阻塞 post_llm_call → 阻塞 Hermes 消息管线
Option A: 同步阻塞 / Option B: 异步 daemon
Choice: B
Evidence (v4.4 Baseline A): `process_turn_async()` (ca/__init__.py, v4.4); changelog v4.3 (2026-05-24)
⚠️ v5.10 重构 — C-stage daemon 入口改为 `process_turn_f_stage()` (ca/__init__.py:208)，功能由 E-stage+Mixin (`ca/e_stage.py ES
