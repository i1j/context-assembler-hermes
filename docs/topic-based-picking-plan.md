# 话题拣选重构方案（v4.6.0 — 已实现）

## 背景

当前 `_compute_turn_plan` 对每个对话轮独立做出 L0/L1/L2 决策，存在三个问题：

1. **上下文不稳定** — 同一话题的相邻轮次在多次 `assemble()` 调用中可能因检索噪声产生不同级别，缓存命中率低
2. **检索升级的有效性** — 全量检索升级（0-3轮/次）在 9 个历史会话中实际从未触发
3. **工具轮过度膨胀** — 工具轮 L1 摘要（~228 词/条）远高于 L0（~14 词/条），tool_head 特权浪费大量预算

---

## 第一层：拣选（Pick）— 话题分割 + 三级定级（✅ 已实现）

### 话题分割规则

仅依赖 L1 JSON 的 5 个已有字段（`core_change`, `new_materials`, `objective_facts`, `consensus`, `todo`），不需要额外 embedding 或 LLM。

```
对于相邻对话轮 A → B：

R1: 字段存在性检测（= BG / 实义分类）
  [new_materials, objective_facts, consensus, todo] 四字段全空 → BG轮
  连续 BG → 合并
  BG↔实义 → 分裂

R2: todo 链 + 关键词 Jaccard（双实义轮）
  词袋定义：中文用单字，英文词用原词，union 全部 5 字段
  （设计稿含中文二元组，实测用单字已可，按需切换）
  J = Jaccard(A 全字段 ∩ B 全字段)
  todo_重叠 = A.todo ∩ (B.core_change ∪ B.new_materials)

  todo_重叠 ≥ 1 AND J ≥ 0.03 → 首次合并（进入链）
  todo_重叠 ≥ 1 AND J ≥ 0.04 → 链内扩展（已在链中）
  不满足条件 → 分裂
```

**验证结果：** 跨 2 会话 19 轮实测，所有 BG/实义边界正确识别，Jaccard 合并条件因会话话题自然交替未触发（预期行为）。

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
- RRF 融合规则：完全不变（代码复用 `_rrf_fuse`）
- 短 query 自动补偿：`_dynamic_allocation` 按 BM25 命中数分配两路比例，无需词汇长度判断

**实现位置：** `ca/retrieval.py` — `TopicRetriever` 类（v4.6.0 新增）

### 对话轮三级定级

核心新概念：**topic 半径 r**，统一单轮与多轮 topic 的度量

```
r = min(max_intra, nearest_centroid_dist / TOPIC_RADIUS_WEIGHT)

max_intra = topic 内所有轮到形心的最大距离
  - 多轮 topic：按 embedding 计算
  - 单轮 topic：精确为 0.0（避免浮点精度 ~1e-16 伪距离）
nearest_centroid_dist = 本 topic 形心到最近异 topic 形心的距离（所有 topic）
TOPIC_RADIUS_WEIGHT = 2.0（默认，可配置）
```

**最小半径保护：** 当 `r <= 0` 时设为 `0.05`，防止自查询时 d≈0 落入 L0。

**定级规则：**

```
对非 tail、非 BG 的实义 topic：
  ┌─ BM25 + vector → RRF → retrieved_topics
  │
  ├─ 内球 (vec_dist ≤ r/2)         → L2   ← 当前核心语义话题
  ├─ 外球 (vec_dist ≤ r)           → L1   ← 历史相关话题（基线）
  ├─ retrieved 但球外 (vec_dist > r) → L1  ← BM25 捞回
  └─ 非 retrieved 且球外           → L0   ← 远距离话题降级
```

| 条件                  | Level | 含义                                    |
| --------------------- | ----- | --------------------------------------- |
| tail 区               | L2    | 尾区保护，不变                          |
| BG 类 topic           | L0    | 固定降级，不参与检索                    |
| `d ≤ r/2`           | L2    | 内球 — 当前核心（含 d=0 自查询）      |
| `d ≤ r`             | L1    | 外球 — 基线（不依赖检索命中）          |
| `d > r` 且检索命中 | L1    | BM25 捞回                              |

> **设计稿与实际差异：** 不等式用 `≤` 而非 `<`。浮点边界场景下 `d=0`（自查询）需落入 L2，单次 topic max_intra=0 时 r=0.05（最小保护），d=0 ≤ 0.025 → L2。详见下方"实际实现细节"。

### 工具轮定级

| 条件                  | 新方案                 |
| --------------------- | ---------------------- |
| tail                  | L2（不变）             |
| 父 topic 为 L2        | **L1**（topic_boost） |
| retrieved             | L1（不变）             |
| 其余（含原 head）     | **L0**（新基线） |

**topic_boost 规则（新增）：** 当父对话轮所属 topic 整体被评为 L2 时，该 topic 下的所有工具轮自动升为 L1。这个机制不依赖检索预算——核心话题的工具轮信息价值高，应优先保留。

### 数据结构变更

`turn_plan` 表已有 `topic_group` 列，`TurnPlanEntry` 已有同名 field，无需 schema 变更。

新增 `query_embedding` 记录（schema v3→v4）：

与现有 `l0_embedding`/`l1_embedding` 一致，以 BLOB 列存入 `turn_cache`。
每条对话轮对应一次 `embed(user_message)`，一一映射。

写入时机：`assemble()` 在 `embed(user_message)` 后立即写入当前对话轮的 `query_embedding`。方便事后复盘回放。

### 移除的旧机制

| 移除项                     | 原因                                 |
| -------------------------- | ------------------------------------ |
| C-stage 话题边界检测       | 话题分割由 assemble() 统一实时计算   |
| `_pre_upgrade_tools`       | 工具轮不再预选，全走 topic 级决策    |
| `_current_topic_id`        | 话题 id 在 assemble() 中实时分配     |
| `_topic_lock`              | 无并发写 topic_id                    |
| `_available_budget` tool_head 参数 | 不再区分 head 工具               |

---

## 实际实现细节（相对于原始设计稿的偏差）

| 项目                        | 设计稿                            | 实际实现                                         |
| --------------------------- | --------------------------------- | ------------------------------------------------ |
| 半径公式                    | `r = min(max_intra, 最近邻/2)`  | `r = min(max_intra, nearest/TOPIC_RADIUS_WEIGHT)`（系数可配置） |
| 单轮 max_intra              | 未明确                            | 精确为 `0.0`（避免浮点精度失真）                 |
| 最小半径                    | 无                                | `r <= 0` 时设为 `0.05`                           |
| 定级比较                    | `d < r/2` / `d < r`           | `d ≤ r/2` / `d ≤ r`（浮点边界稳健）          |
| Jaccard 中文分词            | 字符二元组                        | 单字（二元组按需，代码中 `_jaccard_tokens` 已含二元组逻辑但注释） |
| 工具轮绑定                  | 无                                | 父 topic L2 → 工具轮 L1（topic_boost）           |
| TopicRetriever              | 在 retrieval.py 中改现有方法      | 独立类 `TopicRetriever`                           |
| RRF 融合                    | 「规则完全不变」                  | 完全不变，复用 `_rrf_fuse`                       |

---

## 第二层：压缩（Compress）— 空闲时话题摘要，递归压缩（⏳ 未实现）

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

### 替换机制

```
assemble() 中，对每个 topic：
  1. 如果 topic 被选为 L0（远距离降级）
  2. 且 topic 有已生成的话题摘要
  3. 则用 1 条 topic_summary 替代 topic 内所有对话轮的 L0 内容
```

### 递归压缩

```
T_l0: 原始对话话题 ──→ 话题A, 话题B, 话题C, ...
                         │
T_l1: 一级话题摘要 ──→ 摘要A, 摘要B, 摘要C, ...
                         │
T_l2: 二级摘要 ────→ [摘要A+B], [摘要C+D], ...
```

### 状态

> ⏳ **未实现** — 留待 v4.7.0 讨论。
> 第一层（话题分割 + 三级定级）已独立上线运行。第二层需要：
> - `topic_summary` 表设计与迁移
> - 空闲线程安全扫描已封存 topic
> - 摘要替换逻辑在 assemble() 中集成
> - 确认生成路径（OpenViking 或 L-stage LLM）

---

## 自适应参数环路（⏳ 未讨论）

### 动机

话题分割的阈值（Jaccard 0.03/0.04）、半径 r 的计算方式、BM25/vector 的融合权重——这些参数在当前是静态的。但不同会话的语义分布不同。

### 设计草案

```
空闲时（异步）：
  1. 采集前一会话的 topic 分布统计
  2. 构造观察报告
  3. 提交 LLM（或 OpenViking）评估
  4. 应用建议（经安全检查）
  5. 记录调整历史
```

| 参数                                 | 调整范围              | 说明                           |
| ------------------------------------ | --------------------- | ------------------------------ |
| Jaccard 首次合并阈值                 | 0.02~0.05             | 话题分割入口敏感度             |
| Jaccard 链内扩展阈值                 | 0.03~0.06             | 链内紧致度                     |
| r 的最近邻权重系数                   | 1.0~3.0（当前为 2.0） | 半径公式中最近邻距离的权重系数 |
| BM25 命中阈值 `BM25_HIT_THRESHOLD` | 1~10                  | BM25 主导切换点                |
| 工具轮 L0 基线是否保留               | 开/关                 | 极端长会话可考虑 L-1           |

### 状态

> ⏳ **未讨论** — 不纳入 v4.6.0 范围。

---

## 完整决策管线（v4.6.0 已实现部分）

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
│    _compute_topic_groups()           │
│    → {turn → topic_id}              │
│    → topic_data（形心/半径/分类）    │
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
│    BG topic → Config.TOPIC_BG_LEVEL │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 4. BM25 + Vector 检索（per-topic） │
│    TopicRetriever.retrieve()        │
│    BM25(topic_agg_text, query)      │
│    vector(query_emb, topic_centroid)│
│    → RRF → retrieved_topics         │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 5. 三级定级（半径 r）              │
│    _grade_topics_by_radius()        │
│    d = 1 - cos(query_emb, centroid) │
│    r = min(max_intra, nearest/WEIGHT│
│                                    │
│    d ≤ r/2  → L2                    │
│    d ≤ r    → L1                    │
│    retrieved → L1（BM25 捞回）      │
│    其余     → L0                    │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 6. 工具轮绑定（topic_boost）       │
│    父 topic L2 → 工具轮 L1          │
│    工具尾区 > topic_boost            │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│ 7. 构建上下文消息                  │
│    _compute_turn_plan_v2()          │
│    write_turn_plan(topic_group)     │
│    build_messages_from_plan()       │
│    deduplicate()                    │
└─────────────────────────────────────┘
```

> 步骤 0-5 在 assemble() 的 `topic_seg` / `topic_retrieval` 阶段完成。
> 步骤 6 整合在 `_compute_turn_plan_v2()` 中。
> 第二层的空闲线程（步骤 8）未实现。

---

## 未变更项

| 模块                         | 状态                       |
| ---------------------------- | -------------------------- |
| C-stage (process_turn_async) | 不变（仅移除 topic 检测）  |
| L-stage 回填                 | 不变                       |
| BM25 + vector 检索结构       | 不变（检索对象改为 per-topic） |
| RRF 融合逻辑                 | 不变                       |
| 去重 (deduplicate)           | 不变                       |
| 消息组装 (build_messages)    | 不变                       |
| Tail 保护                    | 不变                       |
| 存储层 core schema           | topic_group 已有；新增 query_embedding 列 |

## 新增变更（实际实现）

| 文件                | 变更                                                                                     |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `ca/__init__.py`  | 新增 `_compute_topic_groups()`、`_grade_topics_by_radius()`、`_compute_turn_plan_v2()`；修改 `assemble()` 管线；移除 `_pre_upgrade_tools` 等 3 个方法 + 5 个字段 |
| `ca/retrieval.py` | 新增 `TopicRetriever` 类（per-topic BM25+vector+RRF）                                     |
| `ca/store.py`     | schema v3→v4（query_embedding 列）；新增 `read_turn_l1_fields()`、`read_topic_l1_texts()` 等 5 个辅助方法 |
| `ca/config.py`    | 新增 5 个话题配置项（TOPIC_JACCARD_ENTRY/CHAIN/RADIUS_WEIGHT/MAX_UPGRADE/BG_LEVEL）       |
| `ca/stats.py`     | 新增 `topic_count` / `topic_retrieved_count` 统计字段                                     |
| `tests/`          | conftest.py psutil 可选导入；test_v440.py pre_upgrade 测试→pre_upgrade_removed + topic_boost 测试 |

## 验证方法

1. **话题分割** — 2 个历史 DB 回归（18轮+1轮），BG/实义边界正确，Jaccard 合并未触发（预期）
2. **检索迁移** — `TopicRetriever` 单元测试通过
3. **三级定级** — 历史会话实测：首轮自查询 d≈0 → L2；末轮查询 d>r → L0；单轮 topic max_intra=0.0 + 最小半径保护
4. **topic_boost** — 单元测试验证父 topic L2 时工具轮升 L1
5. **缓存命中** — 同话题在多次 assemble() 中输出相同 target_level（通过 turn_plan 可观测）
