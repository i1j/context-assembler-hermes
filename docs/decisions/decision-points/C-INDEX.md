# C-stage 决策索引

> 三阶段架构的第一个阶段，包含 **C-stage 核心**（异步化、OODA 摘要、工具轮）、**A-stage**（组装、检索、预算）、**L-stage**（后台守护）。
> C-stage 核心属于 R-001（三阶段架构），A-stage 和 L-stage 分别对应该架构的后两个阶段。

---

> ⚠️ = 历史存档项（v4.4.0 或更早版本），保留供溯源，不对应当前代码。

## C-stage 核心 (C-001~C-009)

C-stage（上下文阶段）负责异步采集对话轮次，通过降级链调用 LLM 生成 OODA 摘要，并用规则引擎处理工具轮。

- [[C-001]] C-stage daemon thread 异步化
  - [[C-002]] turn_index 分配
  - [[C-003]] 降级链 (LLM fallback + 参数可配置 + OODA 容错) ⚠️ 历史
    - [[C-003a]] num_predict 512→24768 (v4.3.2 事故根因)
    - [[C-003b]] LLM 参数三级回退
    - [[C-003c]] OODA 文本 vs JSON (妥协方案) ⚠️ 历史
    - [[C-003d]] OODAParser 别名匹配 (+ bug-007 修复) ⚠️ 历史
  - [[C-004]] 工具轮不调 LLM — 规则引擎摘要
    - [[C-005]] 按单工具调用拆分 → [[C-006]] 响应内容三级处理 ⚠️ 历史
    - [[C-007]] 四级字段优先级 (VIP/P0/P1/P2)
    - [[C-008]] YAML 配置 + 三段回退链
    - [[C-009]] 10 个结构化 Handler

---

## A-stage 决策 (C-010~C-018)

A-stage（组装阶段）负责从 C-stage 产出的 Fct/Hdl 中，按三区模型（Head/Middle/Tail）筛选内容，经双路检索和预算闸门后组装到上下文。

- [[C-010]] A-stage 解耦 — 仅接收 user_message
  - [[C-011]] 三区模型 (Head/Middle/Tail) ⚠️ 历史
      - [[C-011a]] `_is_valid_summary` fail-close (审查修正)
    - [[C-012]] 尾区保护区 — 20K 对话保护区 (未实装，旧方案运行中) ⚠️ 历史
      - [[C-013]] 尾区保护区 — 旧方案: `_compute_tail_start()` 消息索引 ⚠️ 历史
    - [[C-014]] 双路检索 (BM25 + 向量/纯 BM25 降级)
      - [[C-014a]] RRF 得分排序 (审查修正)
      - [[C-015]] 动态分配 BM25/向量候选
      - [[C-016]] 嵌入维度不匹配 → 过滤 + 纯 BM25
    - [[C-017]] 预算闸门 ⚠️ 历史
    - [[C-018]] 工具组组装格式
  - [[C-025]] should_compress() = False (always assemble) ⚠️ 历史
  - [[C-027]] save/restore 快照机制

## L-stage 决策 (C-019~C-027)

L-stage（后台守护阶段）在后台独立运行，当 C-stage 失败时补充摘要，对话补全后自动拆分工具轮。

- [[C-019]] L-stage 独立守护线程 (v4.4.0)
  - [[C-020]] 对话/工具双独立线程
    - [[C-021]] 限速 (2/s 对话, 5/s 工具)
    - [[C-022]] 3 次失败 → `_assemble_status=2` 永久跳过
    - [[C-023]] 对话补全后自动拆工具轮
- [[C-025]] should_compress() = False (always assemble)
- [[C-027]] save/restore 快照机制

---

**父节点:** [[R-000]] → [[R-001]]（三阶段架构）
**总决策点:** 31
**覆盖:** 三阶段架构的 C-stage 异步采集、A-stage 组装检索、L-stage 后台守护
