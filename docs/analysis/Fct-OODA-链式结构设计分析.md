# Fct 多 OODA 链式数据结构设计分析

## 一、现状：单 OODA + 按"轮"归类

### 当前数据流

```
Hermes 对话轮次 (turn_stream)
       │
       ▼
F-stage 异步 LLM 摘要
       │
       ▼
turn_cache(turn_index, l1_text=FctJSON)
       │
       ▼
A-stage 话题分割：逐轮比较 adjacent 推测话题归属
       │
       ▼
turn_plan(turn_index, topic_group)
```

### 当前 Fct JSON 结构

```json
{
  "core_change": "【已实施】C-stage 与 A-stage 均正常工作",
  "new_materials": ["⚠️ 问题1: 配置警告", "⚠️ 问题2: RecursionError"],
  "objective_facts": ["内置工具名无对应摘要方法"],
  "consensus": ["不需要额外修复"],
  "todo": ["【已实施】C-stage 正常运行", "【计划】排查 L-stage"]
}
```

### 结构缺陷

| 问题 | 表现 |
|------|------|
| **证据与行动脱耦** | new_materials[0] 与 todo[0] 仅靠位置对齐，50% 错配 |
| **多线程被压平** | 问题1 和问题2 完全不同、不同生命周期，被混进同一个 dict |
| **生命周期混杂** | 问题1 的 done（已解决）和问题2 的 planned（待处理）绑死在同一轮 |
| **话题切割粗糙** | 按"轮"切，发现链A休眠时整轮分裂，恢复时整轮重合并 |
| **空值歧义** | `todo` 缺失 = 原始无内容 还是 继承无变化？无法区分 |

---

## 二、多 OODA 链式结构

### 核心变更：从"一轮一 Fct"到"一轮多链，每条续写"

```
turn_stream (Elm 层)
       │
       ▼
F-stage Step 1: 链路由
  输入: 新 Elm + 活跃链列表（各链最新 Fct）
  输出: [{chain_id, elm_segment}, ...]  ← LLM 语义判断归属
  调用: 1 次 LLM / 轮
       │
       ▼
F-stage Step 2: 逐链续写 OODA
  输入: {前轮链 Fct} + {elm_segment}
  输出: {新 OODA 条目: observation, orientation, decision, action}
  调用: N 次 LLM / 轮（N=本轮匹配的链数）
       │
       ▼
ooda_entries (独立表)
  ooda_id | session_id | chain_id | turn_index |
  observation | orientation | decision | action |
  created_at
       │
       ▼
A-stage: 按链注入，不按轮
  活跃链 → 取最新 OODA → 注入上下文
```

### 续写模式 vs 独立生成模式

```
【独立生成 — 当前】
  轮 T5 Elm ────LLM───→ Fct_T5（从头看 Elm，从头写 OODA）
  轮 T6 Elm ────LLM───→ Fct_T6（从头看 Elm，从头写 OODA）
  → 每次 1 次 LLM 调用
  → Fct 之间无显式关联

【续写 — 改进】
  轮 T5 Elm + 前轮链A Fct ──LLM──→ 链A OODA 续写
  轮 T5 Elm + 前轮链B Fct ──LLM──→ 链B OODA 续写
  → 每次 1+N 次 LLM 调用（N=匹配链数）
  → 每条 OODA 都关联到前轮上下文
```

### 续写的优势

1. **语义级链匹配**：LLM 理解"查阅 plan.md"→"开始读文件"是同一链，即使 CJK 重叠为 0
2. **链内上下文不丢失**：续写时 LLM 看到前轮 Fct，能准确判断"进展"还是"新发现"
3. **自然处理休眠/恢复**：从链视角，如果链 A 在 T3 休眠、T10 恢复，续写模式的 LLM 能看到链 A 的 T3 Fct

### 新 Fct 结构

```json
{
  "chain_id": "chain_A",
  "observation": "L-stage RecursionError 排查发现是函数调用深度问题",
  "orientation": "需修改递归边界条件",
  "decision": "在下轮提交修复",
  "action": "计划修改 _backfill_dialogue_history"
}
```

`action: null` = Elm 中无行动信号（原始空）。
`action: ""` 或 `action: "一致"` = 行动与前轮 Fct 一致（继承无变化）。
`action: "..."` = 有具体更新内容。

### 表结构

```
turn_stream (Elm 层 —— 只存原生 Hermes 消息)
  session_id | turn | seq | role | content
  ─────── PK ───────┘

ooda_entries (Fct 层 —— 独立表，按链组织)
  ooda_id | session_id | turn_index |
  chain_id | observation | orientation | decision | action |
  created_at
  ─── PK ──┘

chain_groups (链信息)
  chain_id | session_id | status(active/suspended/completed) |
  last_updated | keyword_tags
```

### turn_cache / turn_plan 表的退位

`turn_cache` 原存 Fct(Fct) + Hdl(Hdl) 可以删除——Elm 在 turn_stream，Fct 在 ooda_entries，Hdl 可在链级派生。
`turn_plan` 存 topic_group——链 ID 自然给出话题归组，不需要独立表。

---

## 三、LLM 驱动的链匹配（续写模式）

### 流程细节

```
新 Elm 到达 F-stage
       │
       ▼
Step 1: 路由（1 次 LLM 调用）
  输入: 新 Elm 摘要 + 活跃链列表（各链最新 OODA，~200 字/条）
  输出: [{chain_id: "A", elm_snippet: "..."},
         {chain_id: null, topic: "新话题", elm_snippet: "..."}]
  LLM 纯分类判断，不生成 OODA 内容，输出极短
       │
       ▼
Step 2: 续写（N 次 LLM 调用，独立并行）
  输入: {前轮链 Fct} + {elm_snippet}
  输出: 新 OODA 条目（observation, orientation, decision, action）
  
  每条续写只看到本链前轮 Fct + 对应 Elm 片段
  不看整轮 Elm，不看其他链
  N 条调用并发出，不串行
       │
       ▼
Step 3: 持久化
  写入 ooda_entries
  更新 chain_groups（last_updated, status）
```

### 时间预算（续写并行）

```
时间轴:
路由 ─────0.3s──────────┐
                        ├─── 续写链A ───0.6s───┐
                        ├─── 续写链B ───0.6s───┤  ← 并行
                        ├─── 新链C ─────0.6s───┤
                        └─── 续写链D ───0.6s───┘
                                                ↓
                                              写入

墙钟时间 = 路由(0.3s) + max(续写各链)(0.6s) = 0.9s
```

| 指标 | 当前模式 | 续写模式（并行） |
|------|:-------:|:--------------:|
| LLM 调用/轮 | 1 次 | 1 + N 次并发出 |
| 墙钟耗时/轮 | ~1.5s | **~0.9s** ✅ |
| 最差（N=4） | 1.5s | ~1.0s |
| 用户间隔 | ≥30s | ≥30s ✅ |

路由比当前 Fct 快得多（纯分类，输出仅 50-100 token）。续写只看本链片段（~300 token + 前轮 Fct ~400 token），比当前看整轮 Elm 快。**续写并行不叠加时间，总墙钟反而比当前更快**。

### 链状态机（简化版）

| 状态 | 条件 | 行为 |
|------|------|------|
| `active` | 最近 3 轮内有新 OODA | 参与路由决策，参与上下文注入 |
| `suspended` | 连续 3 轮路由未命中 | 不参与路由，历史保留 |
| `completed` | 最新 OODA 的 action 含完结关键词 | 释放链累积器 |
| `resumed` | suspended 的链被路由再次命中 | 恢复 active，带历史注入 |

**completed 判定**：LLM 在写 action 时如果标志了完结（如"修复完成""问题闭合""无需处理"），链匹配器标记为 completed。无关键判断——链匹配器不主动判断完结，而是信任 LLM 输出的 OODA 信号。

---

## 四、路由 LLM 的上下文优化

路由步骤的核心挑战：**活跃链的摘要给多少？**

如果会话有 20 条活跃链，每条给 400 字摘要，路由 LLM 输入达到 8000 字，消耗大量 token 且可能超出小模型的上下文窗。

### 优化方案：3+N 竞争

```
活跃链按"最后活跃轮次"排序，取 Top 3 作为路由候选。
【默认】先与 Top 3 候选做语义匹配
【未匹配】剩余链做一次批量排查（一条 prompt 问"这些链中是否有相关的"）

3 条候选 + 剩余 N 条批量 = 路由 LLM 输入可控在 2000 token 以内
```

### 路由 LLM 输出格式

```json
{
  "matches": [
    {"chain_id": "A", "relevance": "high", "elm_snippet": "排查RecursionError发现..."},
    {"chain_id": null, "relevance": "low", "elm_snippet": null}
  ],
  "new_topics": [
    {"topic": "OODA结构讨论", "elm_snippet": "我觉得现在这个结构有问题..."}
  ]
}
```

路由成功后，续写 LLM 只接收到匹配链的前轮 Fct + 相关 Elm 片段——不需要看整个 Elm。

---

## 五、上下文注入的变化

### 当前：按轮选话题组

```
A-stage planner: 选活跃对话轮 → 按 topic_group 整轮注入 Fct
问题: 链 C 在 T3 已休眠，但 T4-T5 的话题组还带着它
```

### 改进：按链提供 Fct

```
A-stage planner: 选活跃 OODA 链 → 注入链内最新 OODA
优点: 
  每条链独立判断是否注入
  休眠链自动过滤，不稀释上下文
  恢复链带历史 OODA 注入
```

### 对比效果

```
轮:       T1────T2────T3────T4────T5────
链A修复:  ████████████░░░░░░░░░░░░░░░░░░  completed，不注入
链B排查:  ██████████████████████████████  active，一直注入
链C优化:  ░░████░░░░░░████████░░░░░░░░░░  suspended → resumed
                                          ↓
当前注入: T1链A链B → T2链A链B链C → T3链A链B链C → T4链B → T5链B链C
改进注入: 链B(最新)+链C(最新)   ← 只取活跃链，按链输出
```

---

## 六、与旧数据的兼容

旧 ca_cache/*.db 中的 turn_cache 数据通过迁移脚本转为新结构，但迁移后数据格式不同：

| 版本 | 特征 | 处理方式 |
|------|------|---------|
| **v5.0 旧版** | Fct 为 5 字段 JSON，无 `observation/orientation` | 按 heuristic 拆分：`new_materials→observation`，`todo→action`，`core_change 剩余→orientation+decision`。标记 `fct_version: v5.0`，A-stage 用旧路径读取 |
| **v5.1 新版** | OODA 结构，`chain_id` 在生成时写入 | 标准路径 |

旧版数据在话题分割时降级为逐轮匹配，不参与链匹配。

---

## 七、可行性评估

| 模块 | 可行性 | 风险 | 缓解措施 |
|------|:-----:|------|---------|
| LLM 路由（Step 1） | ★★★★ | 活跃链过多时上下文超长 | 3+N 竞争策略 |
| LLM 续写（Step 2） | ★★★★★ | 续写需要看到前轮 Fct，输入变长 | 只给本链 Fct，约 500 token |
| 时间预算 | ★★★★ | 2-5s/轮，异步跑 | F-stage daemon 独立运行 |
| 链匹配准确性 | ★★★★★ | LLM 语义判断 > CJK 启发式 | 测试确认 |
| 跨 session 链 | ★★ | 需全局 ooda_entries 表 | Phase 3，初期 session 内 |
| 旧数据迁移 | ★★★ | v5.0 格式不同 | 降级处理，不断链匹配 |
| Null/"" 区分 | ★★★★★ | 提示词说清楚就行 | LLM 输出后校验 |

---

## 八、改动清单

| 层 | 文件 | 改动 | 程度 |
|----|------|------|:----:|
| **路由提示词** | `prompts.py` | 新增 ROUTING_PROMPT，替代 FCT_GENERATION_PROMPT 的第一阶段 | 新增 |
| **续写提示词** | `prompts.py` | 新增 CONTINUE_OODA_PROMPT，替代 FCT_GENERATION_PROMPT 的主体 | 新增 |
| **解析器** | `post_process.py` | `parse_v1_markdown_xml()` → 分别解析路由输出和续写输出 | 重写 |
| **链匹配器** | `chain_matcher.py`（新增） | LLM 路由调用 + 链状态机 | 新增 |
| **表结构** | `store.py` | 新增 `ooda_entries` + `chain_groups` 表 | 新增 |
| **F-stage** | `__init__.py` | 路由 → 续写 → 写入链路 | 重构 |
| **A-stage** | `__init__.py` + `a_planner.py` | 从"选轮"改"选链" | 重构 |
| **话题分割** | `__init__.py` | 删除 `_compute_topic_groups` | 删除 |
| **清理工具** | `clean_increment()` | 适配 OODA 格式，区分 null/"" | 适配 |
| **迁移脚本** | 新增 `migrate_v5.py` | turn_cache → ooda_entries | 新增 |
| **测试** | `tests/` | 路由测试 + 续写测试 + 链状态测试 | 新增 |

### 不变的部分

- `turn_stream` 表结构不变（Elm 层不动）
- E-stage 写入逻辑不变（仍然写 turn_stream）
- Hermes 插件对外接口不变（`CaAssembler` 注册的 hooks 不换）
- embeddings 的计算路径不变

---

## 九、总结：此设计解决的根因

| 问题 | 旧方案根因 | 新方案解决 |
|------|----------|-----------|
| todo 空值歧义 | todo 缺失 = 无法区分初始/继承 | `action: null` vs `action:""` 由 LLM 在提示词约束下区分 |
| 多线程混在一起 | 一轮一个 Fct 压平所有发现 | 每条链独立续写，LLM 路由决定归属 |
| 休眠链污染上下文 | 话题按"轮"割，休眠链仍随轮注入 | 按链注入，suspended 自动过滤 |
| CJK 匹配假阴性（12%） | 短"查阅plan.md"没有 CJK 与下轮重叠 | LLM 做语义级链归属，理解"查阅"→"阅读中"的关系 |
| 跨会话话题断裂 | 话题组按 session 隔开 | 链 ID 全局，OODA 续写跨 session（Phase 3） |
| 累积器膨胀 → 高频碰撞 | 段内所有字段 CJK 并集 | 每条链独立累积器，completed 释放 |
| 单步生成串话 | 路由+续写合一步，注意力分散 | 路由纯分类，续写独立并行，互不干扰 |
| 续写时间叠加 | 串行调用 N 次续写 | `max(续写)` 不叠加，墙钟反降 |

---

## 十、设计演进全过程

### 问题发现链路

| # | 发现的问题 | 验证方法 | 结论 |
|---|-----------|---------|------|
| 1 | 精确字符串 todo 重叠 0% 命中 | 6 会话 99 对 S→S | → 改为 CJK 字符重叠 ≥3 |
| 2 | Jaccard 独立合并(0.07)过度合并 | 旧算法 97/99 合并，话题失去区分度 | → 删除该路径，仅用 todo 链 |
| 3 | todo=[] 无法区分初始为空 vs 继承为空 | 分析 62 个 todo MISSING 案例 | → `action: null` vs `""` 由 LLM 区分 |
| 4 | W2/W3 窗口扩展虚警率过高 | W3 的 FPR=29.7%，F1 从 93.1% 降至 86.8% | → 不扩展窗口，改用 LLM 语义路由 |
| 5 | 段累积器膨胀 → 高频 CJK 碰撞 | 183 字符段内 → T33→T34 完全不同话题被合并 | → 每条链独立累积器，完成释放 |
| 6 | 多线程混入一轮 Fct | new_materials[0]↔todo[0] 50% 位置错配 | → 多条独立 OODA + 独立链 |
| 7 | Fct 绑死在回合级 | 链休眠时整轮碎片化，恢复时整轮重合并 | → ooda_entries 独立表，按链注入 |
| 8 | CJK 启发式假阴性(12%) | 短 todo 如"查阅 plan.md"无 CJK | → LLM 语义级路由 |
| 9 | 续写串行时间叠加 | 路由+续写 N 次 = N+1 倍时间 | → 续写并行，墙钟 = 路由 + max(续写) |

### 当前设计状态

#### ✅ 已解决

- todo 空值歧义
- 多线程混压
- 休眠链污染上下文
- CJK 假阴性
- 累积器膨胀
- `_state`/`stage_tag` 删除（OODA 完整度自然表达）
- 续写串行时间

#### ⏳ 待验证（后续试验）

- qwen3-4b 路由准确性（宁漏勿错策略是否有效）
- qwen3-4b 续写时前轮 Fct 的参考能力（能否正确写"续写"而非"重写"）
- 链状态机在真实数据中的表现（active/suspended 切换时机）
- 旧数据迁移后的质量（降级匹配 vs 新链匹配）

#### ❌ 不做的事

- Embedding 预过滤（多一个服务故障点，时间节省可以忽略）
- 合一步路由+续写（注意力不够，4B 模型容易串话）
- 跨 session 链匹配（Phase 3，初期限制在 session 内）
- W2/W3 窗口扩展（FPR 从 0% 暴增至 29.7%）
- `_state` / `stage_tag` 字段（冗余，OODA 完整度自身已表达）
- 遍历历史 Fct 做匹配（只需最新 OODA 作为签名）
- `null` vs `""` 显示分离（LLM 按 prompt 输入自然区分）
