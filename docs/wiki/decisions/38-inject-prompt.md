# 决策 38 附录：注入侧 4B 拣选 prompt 草稿（v1）

> 状态：草稿（2026-08-03），待注入侧实现时验证
> 对应：决策 38 §四注入侧流程 ② 4B 拣选锚点
> 调用方：`ca/store.py query_themes_by_semantics` 重构后的注入链路

## 设计要点（承自决策 38 全部分歧）

1. **空注入显式化**（用户确认）：全新话题 → 输出空列表 → I=[] → merge 走 new。**宁缺勿错**——P5 先例（strand 125 幻觉越界）证明 4B 会在候选里硬选，必须给显式的"全部拒绝"出口
2. **用候选 index 引用，不用 reality_id**（P5 教训）：4B 输出候选列表之外的 id 是幻觉，index 天然越界校验
3. **relevance 理由必填**：既是承接依据（约束 4B 认真判断），也是空注入率/拣选质量的审计日志（决策 38 §九健康指标）
4. **与 merge decide 同一语义尺度**：注入判"用户提问需要哪些 reality 背景"，merge 判"strand 承接哪个 reality goals"——都是工作关联判定，不是语义相似

## Prompt 草稿

```
你是上下文检索器。给定用户的当前提问和候选现实工作对象（reality）列表，
拣选出与提问最相关的 top-3 reality 作为注入上下文。

【reality 定义】现实工作对象（工作线）：多个语义独立但工作中有关联的 strand
的集合，跨话题块持续演进。其当前状态由 current_status 描述
（current_state=现状 / goals=进行中的目标）。

【用户提问】
{query}

【候选 reality】（已按语义负向排除，仅保留可能与提问相关的）
{candidates 格式: [index] name | hdl | goals | current_state}

【拣选规则】
1. 相关性判定：该 reality 的 goals/current_state 是否与提问的工作对象
   承接/相关？用户问这个提问时，是否需要该 reality 的背景才能有效回答？
2. 宁缺勿错：若没有任何 reality 与提问相关（全新话题），必须输出空列表
   ——空注入是合法且正确的结果，禁止硬选"最不无关"的 reality。
3. 数量：0~3 个，按相关度降序。
4. 只能引用候选列表中的 index，禁止编造列表外的 reality。

【输出格式】（严格 JSON，无 markdown 围栏）
{
  "selected": [
    {"index": 0, "relevance": "承接理由（中文，说明与提问的工作关联）", "priority": 1}
  ]
}
全新话题 → {"selected": []}
```

## 候选格式（candidates 段）

```
[0] 模型与工作流信息获取协同体
    goals: 获取 CLIP 和 LoRA 明确下载链接 | 确认 GGUF 格式接受度
    state: Checkpoint 仅 GGUF 格式待确认 | CLIP/LoRA 缺失
[1] ...
```

## 待验证点

1. **4B 是否真的会输出空集**（顺从偏差测试）——P5 教训，不能假设 4B 听话
2. **relevance 理由质量**：空洞理由 = 硬选信号，代码可据此降权（per-anchor 权重 α 联动）
3. **index 引用稳定性**：候选顺序变化是否影响 4B 选择（打乱顺序测试）
4. **拣选与 merge decide 的一致性**：注入选 A，块内 strand 却归 B——审计日志应暴露这种分裂（注入利用率指标的基础）
