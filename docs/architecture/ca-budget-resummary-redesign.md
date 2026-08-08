> **⚠️ 历史存档 — v4.4/v5.1 预算-尾区设计方案。v5.10 已改用 topic-aware 替换 + `_simple_mutation_mode_v5` (plugin)。详见 TP-002。**

# 预算-再摘要体系设计约束

> 重建自原 OV wiki `CA/ca-budget-resummary-redesign.md`（源 session 20260610_094540）

## 背景

`_compute_assemble_plan()` 第 999 行调用 `_compute_turn_plan_v2()` 时，`budget` 参数被**硬编码**传值，未从 env 读取。修复后留下一个更根本的问题：预算驱动的动态再摘要体系是否值得做？

## 设计约束（4 点）

### 1. 必要性

预算充足时，把已有的 Hdl 再通过 LLM 摘要到 Fct，收益是否覆盖成本？

- 当前 C-stage 异步生成所有轮次的 Fct/Hdl，turn_cache 中三级并存
- 预算溢出的场景（超 100%）：全部 Hdl 注入，不调用 LLM 做额外再摘要
- 预算充裕的场景：直接用已有的 Fct 注入，也不需要再摘要
- **结论：** 预算驱动的动态再摘要在当前架构下没有明确的收益场景

### 2. 粒度

如果将来要做预算驱动，一轮轮做还是按话题组做？

- 逐轮：细粒度控制但决策次数多
- 按话题组：LLM 一次看到一段连续对话的语义，摘要质量更高
- **建议：** 将来做的话选话题组粒度

### 3. 成本收益

- 把 Hdl→Fct 需要调 LLM（token 开销）
- 当前 Fct 是 C-stage 异步生成的，已经是免费（pre-LLM）得到
- 预算驱动再摘要意味着**用在线推理成本换注入精度**
- 当前 3 级都全量生成，注入时只选一个级别，没有再摘要的必要

### 4. 缓存稳定性

Anthropic prompt caching 的核心机制是 prompt prefix 不变。

- 当前 C-stage 固定输入 → 固定 Fct 输出 → 缓存命中
- 动态再摘要每次换不同的轮次集传给 LLM → prompt prefix 变化 → 缓存失效
- 缓存 miss 会导致首 token 延迟增加 2-3x
- **结论：** 动态再摘要会破坏 prompt caching 收益，当前架构不应引入

## 最终决定

> 暂时不改，写注释说明留待评估。

代码中 `_compute_assemble_plan()` 第 1006 行已添加注释记录上述 4 点约束。
