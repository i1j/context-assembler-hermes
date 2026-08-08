# 决策 42：Hindsight 借鉴——图路注入扩展 + 生长序建构约束（需求设计修正）

## 1. 背景

### 1.1 借鉴源：Hindsight（vectorize-io/hindsight，arXiv:2512.12818）

Hindsight 是 fact-centric agent memory system（LongMemEval 基准 SOTA，2026-01 独立复现）。核心三件套：
- **Retain**：LLM 抽取 facts/时序/实体/关系 → 实体解析归一化 → 稀疏+稠密双向量
- **Recall**：4 路并行检索（semantic + BM25 + **knowledge graph** + **temporal filtering**）
- **Reflect**：跨记忆反思合成洞察（记忆之上的推理，带置信度）

记忆架构四层（paper 摘要）：World facts / Experiences / **synthesized entity summaries** / **evolving beliefs**。

### 1.2 整合度反思（2026-08-07 用户驱动）

**核心认知**：Hindsight 是 **agent 记忆系统**（Retain 摄入→Recall 检索→Reflect 分析，服务 agent 的认知），CA 是 **上下文组装**（注入背景→归并→精炼，服务用户工作线的维护）。两者共享「图/时序/实体」表层词汇，但**目的不同**——借鉴应取「图结构存储」「生长有序性」等数据结构思想，而非「检索多路化」「心智模型」等功能。

**逐项反思结论**：

| 需求 | 原设计 | 反思结论 | 修正 |
|------|--------|----------|------|
| R-1 图路 | retrieval.py 三路 | **层级错配**：retrieval.py 检索 topic 级（对话块），reality 图是 L2 产物（session-start 后）——图路加在这里对象不同层 | 改落点：进 `pick_injection_realities`（reality 级检索，候选扩展） |
| R-2 时序 | 检索加权（最近优先） | **注入场景无时序诉求**（切换话题要「相关」非「最近」）；真正需求是 reality 随轮次**生长**，重构须按生长序 | 改定性：生长序建构约束（非检索信号） |
| R-3 心智模型 | 新表+社区反思任务 | **消费方悬空**：CA 无 agent 认知层（reality 是用户工作对象，非 agent 对世界的认识）；Hindsight Mental Models = synthesized entity summaries + evolving beliefs，服务 on-demand 分析（项目风险/销售复盘）——CA 无此诉求 | **删除** |
| R-4 timeline 结构化 | 加 ts | 保留——支撑生长序建构（R-2 新定性） | 保留 |

### 1.3 关键实证（2026-08-07 实测）

1. **reality 时序数据可用**：`update_reality` 的 timeline_entry 已是 `{topic_id, turns, session_id, overview, seq}` 结构化（实测 8 条落库）；reality.updated_at 为真实 wall-clock（8/5→8/7，115 唯一值）
2. **生长序信号存在但未结构化**：`strand_to_reality` 仅 `(strand_id, reality_id)` 无顺序标记；多成员 reality 内 strand 有真实 turns 轮次（R3: S2[t1,2]→S5[t5,7]→S6[t7]）；timeline 111/115 无 seq（96% 为字符串）——生长序需结构化
3. **决策 38 已定**：注入侧抛弃 sim 思维、merge 侧全用共现图信号；注入拣选已用提问云形心距离（41 §2.4b，0.393 优于基线）——不推翻现有设计

## 2. 需求修正（借鉴落点）

### R-1 图路：注入拣选候选扩展（graph-augmented injection）

**需求**：`pick_injection_realities`（reality 级注入拣选）在提问云形心主路径之外，补图邻居候选扩展。

**设计**：
```
pick_injection_realities(query, q_emb, ...)（现有 41 §2.4b 主路径）
  → ① 提问云形心散度距离 ② 范围预筛 ③ 截断 top-15
  → ④（新增）图路扩展：top-15 中 reality 的图邻居（co_occurs_with/shares_topic/
    depends_on 边）补入候选池（深度 1，去重，上限 +5）
  → ⑤ 4B 拣选 top-3（工作关联判定，空注入允许）
  → ⑥ 4B 失败 → 距离 top-3 兜底
```

**约束**：
- 图路只做**候选扩展**（recall augmentation），排序仍由提问云形心 + 4B 决定（决策 38「行为信号优先」不被破坏）
- 邻居深度 1 固定 + 候选池上限（+5），防止图爆炸
- 依赖 Step 8.5 事实关联边质量（shares_topic/depends_on 越准，图路越有效）
- **落点修正**：不进 retrieval.py（topic 级错配）——reality 级检索只有注入拣选

### R-2 生长序建构约束（growth-order reconstruction）

**需求**：reality 是随轮次逐渐生长的（strand 逐个归并），**重构/归并时必须按生长秩序建构**——timeline 顺序 = strand 归并顺序，防止打乱历史顺序。

**设计**：
```
生长序信号 = strand.turns（实测可用：R3 S2[t1,2]→S5[t5,7]→S6[t7]）
约束 1：timeline 追加顺序 = strand 归并顺序（append_timeline_hdl 防重已实现，seq 需结构化）
约束 2：Step 3 详情重生成输入按 strand turns 升序排列（勿按 LLM 输出序）
约束 3：证据/关联建边（8.5）按时间上下文排序
```

**用法**：
- 精炼轮 Step 3 详情重生成：成员 strand 按 turns 升序输入（生长序重建）
- 归并审查（Step 2）执行合并：被并入 strand 按 turns 插入 timeline（保持生长序）
- 与 flash 经验「timeline 归代码维护」（决策 37 §4.2）一致——代码保证顺序，勿信 LLM

**注**：不再做「检索时序加权」——注入场景无此诉求（切换话题要相关非最近）；时序在 CA 是建构约束而非检索信号。

### R-3 ~~心智模型层~~（删除）

**原设计**：社区反思产出 insight 条目（{topic, observation, evidence_reality_ids, confidence}），新表 mental_models。

**删除理由**（整合度反思，2026-08-07）：
- Hindsight Mental Models = synthesized entity summaries + evolving beliefs，服务 agent 对世界的第二层认知（on-demand 分析：项目风险/销售复盘）
- CA 无此诉求：reality 是用户工作对象（注入/归并消费），不是 agent 的认知层
- 消费方悬空：新表+新任务产出无人消费 = 装饰品，违背「不叠加复杂设计」
- CA 已有等价物：Step 3 详情重生成 = 对 reality 内容的深度融合（内容层）

### R-4 timeline 结构化（支撑生长序）

**需求**：timeline 条目结构化 + 时间戳，支撑 R-2 生长序约束。

**设计**：
1. `update_reality`：`timeline_entry` 增加 `ts=updated_at`（写入时取当前时间戳）
2. `append_timeline_hdl`（reality.py）：改为追加 `{"hdl": h, "ts": now}` 结构化条目（保留防重逻辑：空 hdl 不追加、与末条相同不追加）
3. 兼容：存量字符串条目读取时容忍（`str` → 视为 hdl，ts 缺省用 updated_at）

**收益**：
- timeline 支持 `seq` 有序序列查询（生长序可见）
- 详情重生成（Step 3）按 seq/turns 排序输入（R-2 约束 2）
- seq 是已有字段（update_reality 自动 +1），改动最小

## 3. 与现有设计的关系（边界声明）

| 现有设计 | 本决策关系 |
|----------|-----------|
| 决策 38（共现图/S 匹配分） | **不修改**——图路是消费其产出（共现边） |
| 决策 39（成员云表征） | **不修改**——注入/归并侧表征不变 |
| 决策 41 §2.4b（提问云形心拣选） | **扩展**——R-1 在其主路径上加图路候选扩展 |
| 34 v2.4（精炼轮 8.5 事实关联边） | **联动**——R-1 图路依赖 8.5 建边；R-2 生长序约束 Step 3 |
| 34 v2.6（数据修复前置） | **前置**——事实调查（含 8.5）依赖数据修复 |
| retrieval.py（双路） | **不修改**——topic 级检索与 reality 图不同层 |

**核心原则**：借鉴 Hindsight 的「图结构存储」「生长有序性」数据结构思想；**不引入**「检索多路化」「心智模型」等功能（目的不同）。

## 4. 需求验收（可测试）

| 需求 | 验收标准 |
|------|----------|
| R-1 图路 | 构造 2 个 connected reality 的测试图 → pick_injection_realities 含邻居 reality 的查询 → 候选池含邻居（单测）；无图/无边时行为与现状一致（回归） |
| R-2 生长序 | 详情重生成输入按 strand turns 升序（单测断言 prompt 构造顺序）；合并后 timeline 按 turns 插入（单测） |
| R-4 结构化 timeline | `append_timeline_hdl` 输出 {hdl,ts} 结构化条目（单测）；存量字符串兼容读（回归）；timeline_entry 含 ts 字段（单测） |

## 5. 处置层级

- R-1 图路：L1 代码（图遍历零 4B）
- R-2 生长序：L1 代码（排序约束零 4B）
- R-4 timeline 结构化：L1 代码（格式变更，无 4B）

全部符合精炼轮三级处置原则（34 v2.2）。

## 6. 修订记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-08-07 | 初始：Hindsight 借鉴需求修正（R-1 图路检索 / R-2 时序加权 / R-3 心智模型 / R-4 timeline 结构化增强） |
| v1.1 | 2026-08-07 | 实证修正（用户质疑）：R-4 改为本期可做（timeline_entry 已结构化、updated_at 真实 wall-clock）；R-2 从 turns 降级改为 wall-clock 主信号 |
| v2 | 2026-08-07 | **整合度反思（用户驱动）**：R-1 改落点（retrieval.py → pick_injection_realities 候选扩展）；R-2 改定性（检索加权 → 生长序建构约束）；**R-3 心智模型删除**（消费方悬空，CA 无 agent 认知层）；R-4 保留（支撑生长序） |
