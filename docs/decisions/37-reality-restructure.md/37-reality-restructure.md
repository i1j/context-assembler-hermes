---
trace:
  forward:
    - ca/reality.py
    - ca/inject.py
    - ca/a_stage.py
    - ca/store.py
    - tests/unit/test_reality_prompts.py
---

# 决策 37：CA Reality 重构技术方案

> 状态：设计定稿（2026-08-02），**prompt 设计已完成（另起会话，2026-08-02）**，接入待 reprocess 步骤
> 相关决策：35（strand 多事务摘要）、36（theme wiki 生成）、34（idle refinement）
> 设计溯源：OV L1 五段 → 旧 Fct 五字段 → OODA 四组 → reality.status（血缘链见 §3）
> Tests: `tests/unit/test_reality_prompts.py`（32 用例，prompt 三件套构建/解析）

## 一、背景与动机

### 1.1 现状链路（v6.5.x）

```
切换时注入（query_themes_by_semantics top-3, sim>0）
  → summarize 生成 strand（可选 theme_ref，已停用）
  → run_theme_merge：向量召回（0.70 门槛 top-3）+ decide（4B 宁分不并）+ theme_ref 直连
  → theme（title/overview/混合 ooda/key_facts/timeline）
```

### 1.2 问题清单（实测数据支撑）

| # | 问题 | 证据 |
|---|---|---|
| P1 | **theme 层基本没聚合** | tester 库：407 themes / 482 归并对，353 单 strand（87%），73% strand 孤悬；无 theme >10 strand |
| P2 | **余弦测不出"工作关联"** | 簇审计：向量近的 26 strand 分属 16 theme（含 5+ 真实不同工作线）；4B 实验"随机配对误归 3 例全是真实语义关联"（向量远但工作相关） |
| P3 | **混合 ooda 膨胀、事务边界丢失** | theme 167（6 strand）融合出 51 条 ooda（11+11+18+11）；"技能库更新"重复 5 次；无法追溯哪条来自哪个 strand |
| P4 | **0.70 门槛无校准** | strand kNN P50=0.40（sim 0.60），与 0.70 门槛脱节；为拍脑袋继承值 |
| P5 | **theme_ref 被 sim>0 注入污染** | 注入候选无门槛，4B 生成时误判（strand 125 幻觉越界先例）；theme_ref 整体停用 |
| P6 | **key_facts/ooda 冗余双存** | 同一事实两种措辞（过程态 ooda vs 确认态 key_facts），key_facts 实为块级共享（strand 1/2/3 完全相同） |

### 1.3 根本目的（一切设计的出发点）

**reality/theme 的存在价值：用户切换话题提问时，提供有效的背景数据作为上下文。**

由此推导：
- 粒度应匹配"切换粒度"（用户一起需要的），不是语义相似度
- 噪声 = 注入内容中与当前话题块无关的部分
- 检索/注入的有效性 = 端到端目标，归并只是中间手段

## 二、核心概念

| 概念 | 定义 | 与现状对应 |
|---|---|---|
| **strand** | 对话层的工作线记录（hdl + ooda 四组），事务原子 | 现状 strand（不变） |
| **reality** | 现实工作对象：多个语义独立但工作中有关联的 strand 的集合，跨块/跨会话演进 | 原 theme/话题 wiki 的升级 |
| **timeline** | hdl（当前状态锚点）的演变序列，每次更新前把旧 hdl 追加 | 原 timeline_json 的轻量化 |
| **主题（theme）** | reality 的共现聚合（用户一起需要的 reality 集），检索/注入单元 | 新概念（v7 层） |

**theme 的本质（用户确认）**：不是语义簇，而是"多个语义上独立、但工作中有关联的事物的集合"。工作关联 ≠ 语义相似（衔接/共现是语义/行为关系，向量测不出）。

## 三、设计溯源（血缘链）

```
OV L1 五段（Session Title / Current State / Task & Goals / Key Facts & Decisions / Files & Context）
  → 旧 Fct 五字段（core_change / new_materials / objective_facts / consensus / todo）   [参考 OV 删减]
    → OODA 四组（现象与问题 / 背景与约束 / 决策与方案 / 后续行动）                        [Fct-OODA 设计文档证实迁移映射]
      → strand.ooda（现状）
        → reality.current_status（本轮重构：对齐 OV Current State 结构）
```

> **溯源标注**：「旧 Fct 五字段参考 OV」基于用户记忆 + 结构同构推断（逐段对应，见 §4.3 映射）；原始"OV 摘要字段分析报告"未找到独立存档（`viking://user/sysadmin/memories/cases/OpenViking记忆系统配置分析.md` 为运维向配置分析，非字段血缘分析）。结构同构由两个实据确证：OV L1 实测样本（archive_001）+ Fct-OODA 设计文档迁移映射。

参考 Hindsight（observation 机制）：证据跟踪 / 演化保留 / 矛盾捕捉 → 由时间线覆盖（hdl 演变序列天然记录演化与转向），不引入 observation 式复杂结构。

## 四、数据模型（定稿）

### 4.1 reality

```json
{
  "reality_id": 1,
  "name": "压缩与持久化职责边界厘清",          // 固定事物名（不变；非 L0，L0 锚点是 hdl）
  "hdl": "已明确压缩不写state.db，双管道解耦实现中",  // 当前状态锚点（可改，随演进重写）

  "current_status": {                          // 当前详细状态（结构化，注入/检索/衔接判定用）
    "current_state": ["压缩结果已明确不写state.db", "双管道解耦实现中"],
    "key_facts":     ["2026-07-30: 确认压缩仅用于LLM输入", "采用原子替换保证一致性"],
    "goals":         ["验证in_place模式一致性", "消除双写"],
    "context":       ["plugins/ca_assembler/ca/theme.py"]   // 相关文件/资源，可空
  },

  "timeline": [                                // hdl 演变序列（轻量，一句话 × N）
    "决策做A功能",
    "完成A功能分析设计，进入代码开发",
    "实现双管道解耦，压缩不写state.db"
  ],

  "source_strands": [...],                     // 全部成员证据
  "centroid": [...],                           // embed(hdl + current_status) 均值，检索用
  "profile": "...", "created_at": ..., "updated_at": ...
}
```

### 4.2 字段职责

| 字段 | 性质 | 用途 |
|---|---|---|
| `name` | 固定（不变） | 标识 |
| `hdl` | 可改（每次归并重写） | **当前状态锚点**；旧值追加进 timeline 展现演变 |
| `current_status` | 当前唯一详细状态 | 注入 / 检索（embed） / 衔接判定（goals 是锚） |
| `timeline` | 追加（hdl 历史） | 演化/纠错回溯 |
| `source_strands` | 证据 | 详细历史反查原始 strand |

### 4.3 状态结构对齐 OV L1

| OV L1 段 | reality 字段 |
|---|---|
| Current State | current_status.current_state |
| Task & Goals | current_status.goals |
| Key Facts & Decisions | current_status.key_facts |
| Files & Context | current_status.context |
| Abstract（L0） | 由 hdl 承担（可改锚点，非 name） |

形态：四段全为列表（对齐 OV），4B 条理更新（逐列表增删改）。

## 五、判定机制

### 5.1 strand → reality：OODA 衔接判定（4B 语义）+ 向量负向排除（代码）

```
新 strand 与 candidate reality：
  ① 向量负向排除（代码级）：d(strand, reality.centroid) > 0.55 → 直接排除
     （用词完全不相关 → 肯定不是同一 reality；0.55 来自原型验证，见 §7）
  ② OODA 衔接判定（4B，仅对未排除的候选）：
     strand 的「现象与问题」是否承接 reality.current_status.goals？
     是否推进/更新 reality 的状态？
     → 是：归入（更新 current_status，hdl 重写，旧 hdl 入 timeline）
     → 否：新 reality（create）
```

### 5.2 reality → 主题：使用共现统计（行为观测，非语义判断）

聚合原则（用户确认）：**用户在提及这些事物时，往往需要同时进行同一主题中关联事物的操作**。

信号：
- 同块共现（同话题块 strand 归属的 reality 经常一起出现）
- 同切换命中（切换涉及的事物集重合）
- 同提问召回（提问命中的 reality 经常是同一组）

共现度高 → 同一主题。主题边界随用户行为演化（自适应）。

### 5.3 decide 策略：保持"宁分不并"

理由（用户确认）：碎片（new 过多）可由精炼轮合并修复（可逆）；**错并（工作无关混入）不可逆**——宁分不并是防"工作线断裂污染"的第一道防线。

## 六、原型验证结果（2026-08-02，winker expB 46 strand）

### 6.0 时间序流式全流程实验（2026-08-03，scripts/exp_reality_winker.py）

**方法**：46 strand 按 created_at 升序逐个喂入 → ① 向量负向排除（strand vs 已有 reality.centroid，d>0.55）→ ② 4B decide（goals 承接判定，top-3 候选）→ ③ merge（REALITY_MERGE_PROMPT）/ create（REALITY_CREATE_PROMPT）。

**结果**：

| 指标 | expB 旧链路（32 themes） | reality 流式（26 realities） |
|---|---|---|
| 聚合单元数 | 32 | 26 |
| 单 strand 单元 | 22（69%） | 15（57%） |
| 多 strand 单元 | 10 | 11 |
| 4B fallback | — | 0（40 decide + 20 merge + 26 create 全成功） |

**质量亮点**：strand 15→reality@4（HTTP 头溯源，d=0.128）、25/26→reality@24（LoRA）、47/48→reality@46（KV 显存，跨 expB theme 29/30/31 聚合）均与对照一致；timeline 逐次追加、key_facts 带日期、goals 承接锚完整（OV L1 映射落实）。

**发现的问题**：
- **Q1（已修）**：merge 输出 hdl 时 4B 把 prompt 字段说明抄进值（7/20 次命中「重写后的当前状态锚点：」前缀）→ `parse_reality_response` 增加 `normalize_reality_response` 前缀剥离（+5 测试用例）。
- **Q2（待定）**：reality@1 大杂烩——strand 1/2/6/8/9 五条不同工作线并入（key_facts 8 条、goals 12 条膨胀）。根源：首个 reality 是单 strand 种子，goals 越攒越宽 → decide 承接面滚雪球。
- **Q3/Q4（待定）**：strand 20→reality@14（d=0.482，expB 中独立）、strand 42→reality@11（d=0.418，expB 中独立）——边界区（d 0.41~0.48）误并，decide 被膨胀 goals 误导。
- **Q5（待定）**：strand 2/9→reality@1（d=0.449/0.471 高距离并入）。

**根因**：0.55 排除阈值在"单 strand 种子 reality"场景过宽（原型验证的是已知分组 centroid，非运行时种子）；decide 的 goals 承接判定在 goals 膨胀后失效。候选修复：A 收窄阈值 0.45 / B decide 判定强化（承接 + 可验证推进）/ C 精炼轮合并治理 / D 前缀清洗（已做）。Q2-Q5 需用户决策。

### 6.0.1 防膨胀改造（2026-08-03，对齐 OV WM 三层防线）

**问题补充**（26 reality 实测）：四字段全部膨胀——current_state 7 条超 2-5、key_facts 8 条超"最多 5"、goals 12 条无上限、context 10 条无上限；create 时 4 个 reality（@27/@28/@41/@45）current_status 四段全空（4B 只输出 name/hdl）。

**改造**（对齐 OV `ov_wm_v2_update.yaml` + `session.py` 三层防线，TDD 24 用例）：
1. **prompt 层（字段语义）**：merge prompt 逐字段声明——current_state=快照允许缩小（已解决条目必须删除）；key_facts=按主题合并（保留日期/数值锚点）；**goals=默认 KEEP（原样保留，显式标记完成才移除）**；context=过滤去重（同资源合并为一条）
2. **服务端监控（第 2 层）**：`build_size_warnings()` 统计 reality 各段条数，超限（state>5/facts>5/goals>8/ctx>8）注入 `<section_size_warnings>` 到 merge prompt（对齐 OV `_build_wm_section_reminders`）
3. **代码守卫（第 3 层）**：`guard_merge_consolidation()`——Layer 1 trivial_shrink（新条数<旧 50% 拒绝）、Layer 2 锚点覆盖<70% 拒绝（`extract_lexical_anchors` 中文适配：日期/数字+单位/路径/ASCII 专名）；拒绝后 `salvage_new_items()` 提取真新增 APPEND、`throttle_append()` 超限节流（cap 5）
4. **create 兜底**：`fill_current_status_fallback()`——current_status 全空 → 从 strand ooda 填充（current_state←决策与方案、goals←后续行动、key_facts←changes）

**阈值来源**：实验实测分布（正常 reality 各段 P90 4-6 条）+ OV 粒度缩放（会话级 25 → 单对象 5-8）。

### 6.0.2 防膨胀第二轮实验（2026-08-03，改造后重跑 46 strand）

**效果（对比 6.0 改造前）**：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| reality 数 | 26 | 31 |
| 单 strand 比例 | 57% | 70%（过碎回升，见下） |
| goals 最大条数 | 12（@1） | **4**（@1，全 reality ≤5） |
| key_facts 最大 | 8 | 6 |
| context 最大 | 10 | 5 |
| current_state 最大 | 7 | 6 |
| create 全空 reality | 4 | 5（**兜底修复前实测**，见 D） |

**结论**：
- ✅ **字段膨胀全面收敛**：goals 12→4、context 10→5、key_facts 8→6——三层防线（prompt 语义 + size warnings + 代码守卫）生效；merge prompt 的 goals 默认 KEEP 语义 + 显式标记完成直接消除滚雪球（Q2 根因修复）。
- ⚠️ **create 全空 reality 未清零（D 修正）**：第二轮实测仍有 5 个全空（@7/@28/@31/@33/@41）。根因：`fill_current_status_fallback` 触发条件「current_status 缺失」，但这 5 个是**键存在但四段全空**的空结构 dict（truthy），`not (cs or {})` 漏过。修复（`any(values)` 判断 + 空结构识别）在第三轮验证。
- ⚠️ **过碎回升 57%→70%**：guard 拒绝（trivial_shrink / low_anchor_coverage）→ salvage 后保守回退旧值，部分 merge 被拒绝转 new（strand 37 fallback）；+goals 默认 KEEP 使 decide 的承接判定更保守。**这与 Q2-Q5 的阈值/判定策略纠缠**——防膨胀与过碎是张力两面，精炼轮合并（方向 C）是收敛出口。
- **边界误并减少**：strand 20「事项批准」这次 NEW 独立（上次误并 reality@14）；strand 32 独立（上次误并 reality@31）。decide 判定在四字段语义清晰后更准。

**与 §6.3 量化记录的呼应**：132/42/38 三数（分组/decide 倾向压制合并量）——本次 goals KEEP 语义进一步强化了"宁分不并"侧，过碎是预期代价，需精炼轮（可逆）平衡。

### 6.0.3 数据质量审计 + 第三轮实验（2026-08-03）

**审计发现问题（两轮数据）**：

| # | 问题 | 证据 | 修复 |
|---|---|---|---|
| A1 | strand 10 归错 reality（GGUF 加载入模型协同而非 GGUF 家族） | 距离证据 d(10,@11)=0.210 < d(10,@1)=0.357 | decide 候选展示成员 hdl + 家族一致性规则（prompt） |
| A2 | timeline 重复（reality@35 第 3/4 条相同） | fallback 路径只 append old_hdl 但 hdl 不重写 → 下次重复 | `append_timeline_hdl` 防重（与末条相同跳过） |
| B1 | name 直接抄 strand hdl（@20/@38/@43） | name==hdl==strand hdl | create prompt 强化 name≠hdl（固定事物名 vs 状态锚点） |
| B2 | context 混入函数名/描述（@11 get_gguf_models() 等） | 4 个 reality 非路径条目 | merge prompt context 加「仅路径/URL，禁函数名/描述」 |
| D | 文档断言「create 全空 4→0」未实测 | 第二轮实测仍 5 个全空 | 空结构 dict 识别修复 + 第三轮验证 |

**第三轮结果（46 strand，全修复后）**：

| 指标 | R2 | R3 |
|---|---|---|
| reality 数 | 31 | 33 |
| 单 strand 比例 | 70% | 75% |
| create 全空 | 5 | **0** ✅ |
| timeline 重复 | 1 | **0** ✅ |
| goals 最大 | 5 | 4 ✅ |
| context 最大 | 5 | 5（残留 3 处非路径，4B 不完全遵守） |
| hdl 前缀 | 无 | 无 ✅ |
| fallback | 1 | 0 |

**R3 结论**：
- ✅ **A2/B1/B2/D 全部修复**：timeline 防重、create 全空清零（D 验证）、name 抄 hdl 从 3→1、context 污染 7→3。
- ⚠️ **A1 部分修复（时间序本质）**：strand 10 仍归 reality@1（d=0.357），尽管与 reality@11 更近（d=0.210）——**根因是时间序**：strand 10 先于 strand 11 到达，GGUF 家族 reality 尚未建立，decide 时无同族候选。家族一致性规则无法解决"候选不存在"的问题；需精炼轮后期迁移（§7 后续步骤）或接受（strand 10 与 1/2 的模型文件主题确有相关性）。
- ⚠️ 过碎继续上升（70%→75%）：多重保守叠加（goals KEEP + 家族一致性 + 宁分不并），精炼轮合并是收敛出口。

### 6.1 向量负向排除成立

| 距离分布 | 中位 | 范围 |
|---|---|---|
| 同事物组内 | 0.332 | 0.178~0.500 |
| 跨事物/对照组 | ~0.60 | 0.48~0.76 |
| 污染样本（strand 593 混入 theme 57） | 0.632 | 0.589~0.676 |

结论：组内 vs 跨组分隔清晰（0.33 vs 0.60），**向量距离正确识别污染样本**（593 被自动标为外来户）——"向量负向排除"（d>0.55 直接排除）在数据上有效。

### 6.2 单一词条生成（4B）

theme 167（6 strand）/ theme 57（5 strand）→ 4B 生成单一描述词条（定义 + 状态 + 关键词）：
- 质量高（连贯、含当前状态与关键词）
- 对比现状：51 条混合 ooda → 一段 ~120 字（注入 token 大减）

### 6.3 decide 保持宁分不并的代价（量化记录）

4B 分组任务（132 对）vs decide 任务（38 对）——prompt 倾向压制合并量。sim 放宽（0.70→0.45）显著减碎（单 strand 79%→65%），支持"sim 是过碎主因"判断；prompt 倾向是第二因素（未来精炼轮合并能力就位后可回调，三个数为调参依据：132/42/38）。

## 七、重构步骤

### 第一步：重新聚合 strand（本次执行）

```
输入：482 strand（hdl + ooda + centroid，strand_summaries 现成）
流程：
  ① 向量粗分组（负向排除，d>0.55 不相连）→ 候选对
  ② 4B 衔接判定（OODA 承接，见 §5.1）→ reality 归属
  ③ 每个 reality：4B 生成 current_status（对齐 OV L1 结构）+ hdl（锚点）
  ④ timeline 初始化（首个 hdl）
输出：reality 集合（reality_id/name/hdl/current_status/timeline/source_strands/centroid）
```

- 聚合/生成 **prompt 设计：另起会话**（本会话只定数据模型与判定机制）
- 向量排除阈值 0.55 为原型验证值，全量重建后按分布复核

### 后续步骤（另行规划）

- 主题层：reality → 主题共现聚合（§5.2）
- graphify 图：主题/关联关系入图（节点 = reality/主题，边 = 衔接/共现/归属），社区发现定主题边界
- 检索/注入：提问 → 定位 → 图邻域扩展（替代余弦 top3；embedding 降级为入口定位）
- 噪声评估：注入利用率（行为信号，注入后 strand 归入率）
- 精炼轮重构：社区质量检测 + 高噪声 reality 拆分（从 strand 重新提炼）

## 八、关键决策记录

| 决策 | 内容 | 理由 |
|---|---|---|
| D1 | 抛弃余弦聚合 theme + 余弦 top3 注入 | 工作关联（衔接/共现）不是向量性质（P2 证据） |
| D2 | 向量仅作负向排除（d>0.55 直接排除） | 用词完全不相关 → 肯定不同 reality（必要不充分） |
| D3 | reality 两级：事物（OODA 演进）+ 主题（共现聚合） | 粒度匹配切换粒度；语义/工作关联分离建模 |
| D4 | timeline 仅记 hdl 演变（一句话序列） | 演化/证据/纠错被时间线覆盖，不引入 observation 式结构 |
| D5 | current_status 结构化（state/facts/goals/context） | 对齐 OV L1；goals 作衔接判定锚；4B 条理更新 |
| D6 | name 固定 / hdl 可改（L0 锚点） | name 不可改不能做状态锚点 |
| D7 | decide 保持宁分不并 | 碎片可逆（精炼轮合并）、错并不可逆（工作线断裂污染） |
| D8 | 噪声 = 注入内容与话题块无关部分 | 直接度量根本目的 |
| D9 | theme_ref 停用 | 被 sim>0 注入污染；sim 调高后（其他会话）再评估恢复 |

## 九、待定项

1. 主题共现聚合的统计实现细节（窗口/权重）
2. 噪声评估的触发与阈值（先跑基线）
3. graphify 图 schema（Phase 2）
4. 全量重建的向量排除阈值复核（0.55 是否普适）
5. **聚合/生成 prompt（另起会话设计）**：✅ 已完成（2026-08-02）——`ca/reality.py` 三件套（REALITY_CREATE_PROMPT / REALITY_MERGE_PROMPT / REALITY_DECIDE_PROMPT），OV L1 五段映射 + OV update 语义（见 §4.3 / 本文件 Tests 字段）
6. **timeline 条目是否带时间戳**：现设计为纯 hdl 字符串序列（无 at）——"记录下它展现演变"的时间维度靠什么？（a）条目带 at；（b）按 source_strands 时间推断。倾向 (b) 保持轻量，待定
7. **timeline 与归并的证据关联**：纯 hdl 序列丢失"哪次归并产生该 hdl"（哪些 source_strands）——需要时如何关联？（a）timeline 条目带 source_strands（稍重）；（b）按追加顺序 ↔ strand 归并顺序对齐。待定
8. **重建范围**：§7 输入写 tester 库 482 strand——是否三 profile（tester/sysadmin/winker）都重建？还是 tester 先行验证？
9. **衔接判定的"宁分不并"显式规则**：§5.1 ② 只写"否：新 reality"，未显式声明"不确定 → 新 reality"（宁分不并）——补进 prompt 设计约束（D7 一致）：✅ 已落实（2026-08-02，REALITY_DECIDE_PROMPT 显式"不确定 → new"，测试 test_dont_merge_explicit_rule）
10. **旧 themes 表处置**：重构后旧 theme（407 个）保留作对照还是废弃？数据迁移路径（reality 从 strand 重建，旧 theme 不迁移——需确认）
11. **存储 schema**：§4 为 JSON 模型，未给 SQLite 表定义（reality / reality_strand_map / timeline 存储形态）——重构实现前需定稿

## 十、验证计划

- 重建后：reality 数量分布（vs 旧 407 theme / 87% 单 strand）
- 注入利用率基线（主题块 strand 归入注入 reality 比例）
- 抽样云端审查（对照 reality 归属 vs 语义 ground truth）
- 新旧结构对比报告（过碎修复量化）
