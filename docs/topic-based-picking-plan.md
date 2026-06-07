# 话题拣选重构方案（v4.6.0）

## 背景

当前 `_compute_turn_plan` 对每个对话轮独立做出 L0/L1/L2 决策，存在三个问题：

1. **上下文不稳定** — 同一话题的相邻轮次在多次 `assemble()` 调用中可能因检索噪声产生不同级别，缓存命中率低
2. **检索升级的有效性** — 全量检索升级（0-3轮/次）在 9 个历史会话中实际从未触发
3. **工具轮过度膨胀** — 工具轮 L1 摘要（~228 词/条）远高于 L0（~14 词/条），tool_head 特权浪费大量预算

---

## 第一层：拣选（Pick）— 话题分割 + 三级定级

### 话题分割规则

仅依赖 L1 JSON 的 5 个已有字段（`core_change`, `new_materials`, `objective_facts`, `consensus`, `todo`），不需要额外 embedding 或 LLM。

```
对于相邻对话轮 A → B：

R1: 字段存在性检测（= BG / 实义分类）
  [new_materials, objective_facts, consensus, todo] 四字段全空 → BG轮
  连续 BG → 合并
  BG↔实义 → 分裂

R2: todo 链 + 关键词 Jaccard（双实义轮）
  词袋定义：中文用字符二元组，英文词用原词，union 全部 5 字段
  J = Jaccard(A 全字段 ∩ B 全字段)
  todo_重叠 = A.todo ∩ (B.core_change ∪ B.new_materials)

  todo_重叠 ≥ 1 AND J ≥ 0.03 → 首次合并（进入链）
  todo_重叠 ≥ 1 AND J ≥ 0.04 → 链内扩展（已在链中）
  不满足条件 → 分裂
```

**验证结果：** 跨 9 会话 271 轮，仅一处长链合理截断，其余零变化。

### 检索机制（BM25 + Vector 双路保留）

检索对象从 per-turn 升级为 **per-topic**：

```
当前：                  新：
BM25(query, turn_L1) ─┐  BM25(query, topic_agg_text) ─┐
                       ├─ RRF → upgrades               ├─ RRF → retrieved_topics
vector(query, turn_emb)┘  vector(query, topic_centroid)┘
```

- BM25 输入：topic 内所有轮的 5 字段拼接文本（比单轮 L1 更丰富）
- Vector 输入：topic 形心 = topic 内所有 L1 embedding 的算术平均
- RRF 融合规则：完全不变
- 短 query 自动补偿：`_dynamic_allocation` 按 BM25 命中数分配两路比例，无需词汇长度判断

### 对话轮三级定级

核心新概念：**topic 半径 r**，统一单轮与多轮 topic 的度量

```
r = min(max_intra, 最近邻形心距离/2)

max_intra = topic 内所有轮到形心的最大距离（仅多轮 topic）
最近邻形心距离 = 本 topic 形心到最近异 topic 形心的距离（所有 topic）
```

**定级规则：**

```
对非 tail、非 BG 的实义 topic：
  ┌─ BM25 + vector → RRF → retrieved_topics
  │
  ├─ 内球 (vec_dist < r/2)         → L2   ← 当前核心语义话题
  ├─ 外球 (vec_dist < r)           → L1   ← 历史相关话题（基线）
  ├─ retrieved 但球外 (vec_dist ≥ r) → L1  ← BM25 捞回
  └─ 非 retrieved 且球外           → L0   ← 远距离话题降级
```

| 条件 | Level | 含义 |
|------|-------|------|
| tail 区 | L2 | 尾区保护，不变 |
| BG 类 topic | L0 | 固定降级，不参与检索 |
| `d < r/2` | L2 | 内球 — 当前核心 |
| `d < r` | L1 | 外球 — 基线（检索命中或不命中均为 L1） |
| `d ≥ r` 且检索命中 | L1 | BM25 捞回 — 仍给 L1 |
> **⚠️ 未讨论：BM25 捞回后的细节判定条件（此处仅列设计者推断）**
| `d ≥ r` 且检索未命中 | L0 | 降级 |

### 工具轮定级

| 条件 | 新方案 |
|------|--------|
| tail | L2（不变） |
| retrieved | L1（不变） |
| 其余（含 head） | **L0**（新基线） |

### 数据结构变更

`turn_plan` 表已有 `topic_group` 列，`TurnPlanEntry` 已有同名 field，无需 schema 变更。

新增 `query_embedding` 记录：

与现有 `l0_embedding`/`l1_embedding` 一致，以 BLOB 列存入 `turn_cache`。
每条对话轮对应一次 `embed(user_message)`，一一映射。

写入时机：`assemble()` 在 `embed(user_message)` 后立即写入当前对话轮的 `query_embedding`。方便事后复盘回放。

---

## 第二层：压缩（Compress）— 空闲时话题摘要，递归压缩

### 设计动机

长会话（50+轮）即使全走 L0 也可能超上下文窗口。预算是不可测量的——Hermes 不暴露系统提示词、tool schema、缓存等实际开销。因此放弃精确预算，改为**按语义距离自动决定压缩深度**。

### 话题摘要生成

话题边界确定后，在**对话间隙（C-stage 完成，等待下一轮用户输入时）** 的空闲时段，对已封存的话题块做话题摘要。

```
触发条件：
  - 话题已封存（有新的话题开始后，旧话题不再有新轮加入）
  - 当前有空闲（异步线程，不阻塞 assemble）
  - 该话题尚未生成话题摘要

生成方式（二选一）：
  路径 A：将 topic 内所有轮的 L1 JSON 发给 OpenViking，请求返回话题摘要
  路径 B：L-stage 用已有 LLM 连接直接聚合摘要，写回 DB

摘要内容（类似 L1 但上升到 topic 级）：
  - topic_core_change: 整个话题的核心变化流
  - topic_new_materials: 话题中发现的关键材料
  - topic_conclusion: 话题结论 / 共识
  - turn_range: [起始轮, 结束轮]
```

存储位置：新建 `topic_summary` 表

```sql
CREATE TABLE IF NOT EXISTS topic_summary (
    session_id    TEXT NOT NULL,
    topic_id      INTEGER NOT NULL,
    -- 话题摘要内容
    summary_json  TEXT NOT NULL,        -- 结构化摘要 JSON
    embedding     BLOB,                -- 摘要的 embedding
    -- 血缘追溯
    turn_range_start INTEGER NOT NULL, -- 起始对话轮
    turn_range_end   INTEGER NOT NULL, -- 结束对话轮
    -- 层次信息（T_l 命名区别于消息压缩的 L0/L1/L2）
    parent_summary_id INTEGER,            -- 父摘要 ID（下探/上卷链）
    summary_level     INTEGER DEFAULT 0,  -- T_l0=原始话题, T_l1=一级摘要, T_l2=二级摘要...
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, topic_id, summary_level)
);
```

### 替换机制

```
assemble() 中，对每个 topic：
  1. 如果 topic 被选为 L0（远距离降级）
  2. 且 topic 有已生成的话题摘要
  3. 则用 1 条 topic_summary 替代 topic 内所有对话轮的 L0 内容
```


### 递归压缩

话题摘要本身也可以聚类 + 再摘要：

```
T_l0: 原始对话话题 ──→ 话题A, 话题B, 话题C, ...
                         │
T_l1: 一级话题摘要 ──→ 摘要A, 摘要B, 摘要C, ...
                         │
T_l2: 二级摘要 ────→ [摘要A+B], [摘要C+D], ...
                         │
... 周而复始
```

每次递归都是同一个模式：
1. 等待空闲
2. 扫描同级话题摘要
3. 聚类（按语义相近或按血缘关系）
4. 生成上级摘要
5. 写入 `topic_summary(summary_level=N+1)`

### 可追溯性

每条 `topic_summary` 的 `turn_range` 记录了原始的对话轮范围。从任意级别的话题摘要都可以倒查回原始的 L2 全文。做到无限压缩 + 原始数据可追溯。

### 自然增长抑制

系统不主动膨胀上下文。话题摘要仅在以下情况被使用：
1. topic 在球外（`d ≥ r`），需要降级到 L0 或更低
2. 且 topic 有现成的话题摘要
3. 直接用摘要替代多轮 L0，压缩比自动提升

随着会话拉长，历史 topic 自然漂向球外 → 自然被摘要替代 → 无需预算计算。

---

## 自适应参数环路

### 动机

话题分割的阈值（Jaccard 0.03/0.04）、半径 r 的计算方式、BM25/vector 的融合权重——这些参数在当前是静态的。但不同会话的语义分布不同：有的会话话题跳跃性大（需要更小的链内阈值），有的平缓延续（更大的阈值更合理）。静态参数无法适应所有场景。

### 设计

利用 LLM 自身对上下文质量的感知，形成闭环：

```
空闲时（异步）：
  1. 采集当前会话的 topic 分布统计
     - 实义 topic 数 / BG topic 数
     - topic 平均半径 r
     - BM25 vs vector 各自的检索命中率
     - 用户 query 长度分布
     - 被降级到 L0 的 topic 占比

  2. 构造观察报告
     结构化描述当前参数下的行为特征，例如：
     "会话 30 轮，实义 topic 12 个，平均 r=0.09。
      检索命中率 BM25 为主 (70%)，短 query (<3词) 占 40%。
      L0 占比 25%，其中 60% 是 BG topic。"

  3. 提交 LLM（或 OpenViking）评估
     prompt 示例：
     "评估当前话题分割效果。参数：J_entry=0.03, J_chain=0.04。
      观察：... 建议调整哪些参数以改善上下文组装质量？"

  4. 应用建议
     LLM 返回的参数调整经安全检查后生效
     （如阈值范围约束、单次调整步长上限）

  5. 记录调整历史
     写入 config 或独立表，供后续复盘回放
```

### 调整对象

| 参数 | 调整范围 | 说明 |
|------|---------|------|
| Jaccard 首次合并阈值 | 0.02~0.05 | 话题分割入口敏感度 |
| Jaccard 链内扩展阈值 | 0.03~0.06 | 链内紧致度 |
| r 的最近邻权重系数 | 0.3~0.7（当前为 0.5） | 半径公式中 `最近邻/2` 的系数 |
| BM25 命中阈值 `BM25_HIT_THRESHOLD` | 1~10 | BM25 主导切换点 |
| 工具轮 L0 基线是否保留 | 开/关 | 极端长会话可考虑 L-1 |

### 安全措施

- 步长限制：单次调整不超过当前值的 20%
- 范围钳位：每个参数有硬边界（如 Jaccard 不低于 0.02）
- 回滚：保存最近 3 次调整快照，可一键恢复
- 不影响当前 assemble()：调整仅在空闲线程中评估，在下次 assemble() 时生效

---

## 完整决策管线

```
assemble() 进入：

┌─────────────────────────────────────┐
│ 0. 计算 query_embedding            │
│    embed(user_message)              │
│    → 写入当前轮的 query_embedding   │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 1. 话题分割（规则引擎）              │
│    → {turn → topic_id}              │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 2. Tail 保护区判定                  │
│    tail 区内的 topic 直接 L2，不参与│
│    后续话题级决策                   │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 3. BG 话题固定降级                  │
│    BG topic → L0                    │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 4. BM25 + Vector 检索              │
│    BM25(topic_agg_text, query)      │
│    vector(query_emb, topic_centroid)│
│    → RRF → retrieved_topics         │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 5. 三级定级（半径 r）              │
│    d = 1 - cos(query_emb, centroid) │
│    r = min(max_intra, 最近邻/2)    │
│                                    │
│    d < r/2  → L2                    │
│    d < r    → L1                    │
│    retrieved → L1（BM25 捞回）      │
│    其余     → L0                    │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 6. Topic 摘要替换（可选降级）      │
│    对 L0 的 topic：                 │
│    如有话题摘要 → 1条替代N轮 L0     │
│    如无摘要     → 维持原 L0         │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 7. 构建上下文消息                  │
│    write_turn_plan(topic_group)     │
│    build_messages_from_plan()       │
│    deduplicate()                    │
└─────────────────────────────────────┘

空闲线程（异步，不阻塞）：
┌─────────────────────────────────────┐
│ 8. 扫描已封存 topic                 │
│    → 生成 topic_summary             │
│    → 写入 DB                        │
│    → 递归聚类 + 再摘要              │
└─────────────────────────────────────┘
```

---

## 未变更项

| 模块 | 状态 |
|------|------|
| C-stage (process_turn_async) | 不变 |
| L-stage 回填 | 不变（仅新增话题摘要生成） |
| BM25 + vector 检索结构 | 不变（仅换检索对象） |
| RRF 融合逻辑 | 不变 |
| 去重 (deduplicate) | 不变 |
| 消息组装 (build_messages) | 不变 |
| Tail 保护 | 不变 |
| 存储层 core schema | 不变（topic_group 已有） |

---

## 新增变更

| 文件 | 变更 |
|------|------|
| `ca/__init__.py` | `_compute_topic_groups`；修改 `_compute_turn_plan`（topic 级决策）；话题摘要替换逻辑 |
| `ca/retrieval.py` | BM25 + vector 从 per-turn 改为 per-topic（检索对象替换） |
| `ca/store.py` | 新增 `topic_summary` 表读写方法；`query_embedding` 读写 |
| `ca/config.py` | 新增 topic 分割阈值、topic 摘要开关等环境变量 |
| `tests/` | 测试用例更新 |

---

## 验证方法

1. **话题分割** — 9 个历史 DB 回归，turn_plan.topic_group 与预期一致
2. **检索迁移** — 模拟 BM25 + vector 在 per-topic 上的命中分布，对比原 per-turn 分布
3. **三级定级** — 用历史 query_embedding（新增记录后）验证半径 r 的 L2/L1/L0 分界合理性
4. **topic 摘要** — 端到端观察空闲线程生成的话题摘要内容质量
5. **缓存命中** — 同话题在多次 assemble() 中是否输出相同 target_level（可观测）
