---
trace:
  forward:
    - ca/refinement.py
    - ca/fact_linking.py
    - ca/lstage.py
    - ca/store.py
---

# 34-idle-refinement.md

## 背景

### 现状

CA 管线目前已覆盖 L1（话题摘要生成）、L2（话题→reality 归并）、L3（reality→graphify 同步）。但 L2 和 L3 **仅在 session start 触发一次**，用户连续多轮对话期间积累的话题摘要永不被精炼。

**v2（2026-08-07，决策 41 后 reality 化修订）**：精炼对象从 `topic_wiki` entry 迁移为 **`realities` 表 reality**（决策 37/38/40/41：reality 是唯一聚合单元，theme 层退役）。`ca/refinement.py` 代码已 reality 化（候选读取 realities 表、健康评分用 strand_to_reality 计数、回写走 `update_reality`），但设计文档 v1 未同步——本文档为 reality 版精炼轮设计。

**最近 reality 精炼经验（2026-08-05，flash 全量 + 手工精炼轮 179→140→111）**：
- **宁并不分**（用户纠正，2026-08-05）：精炼轮与生成阶段**相反**——生成防错并（不可逆）、精炼轮纠过碎。边界模糊**倾向合并**；保留拆裂 = 精炼轮失职；只有确非同一工作线（同领域不同任务）才不并
- **两阶段重构**：决策层（紧凑 ref + 新 strand → 只输出 s2r/discarded/affected）+ 内容层（affected 分批 ≤10 生成详情）
- **goals 承接锚**：member_count≥6 只接受明确承接 goals 的 strand（防大 reality 吞新 strand，实测 max 172→34）
- **向量粗筛**：全量紧凑 ref 超 context → embed_batch 候选 ref（每 strand vs reality name+hdl 云 top-30）
- **合并后必须详情重生成**（hdl/current_status 与全成员同步，timeline 旧 hdl append）
- **timeline 归代码维护**（决策 37 §4.2）：勿信 LLM 返回的 timeline，merge 时恢复旧值 + hdl 更新 append 旧 hdl

### 遗留问题

v7（28-topic-summarization-v4.md）明确将以下任务贴上「推迟到空闲期自我改进」标签：

1. **Reality 归并审查** — 过碎 reality 合并（宁并不分）、错分 strand 重归属、过度合并（多工作线揉杂）拆分
2. **Reality 内精炼** — 去冗余、纠错、矛盾合并（hdl/current_status/key_facts/goals）
3. **Fct↔Reality 一致性验证** — source 数据与 reality 内容的交叉检查
4. **僵尸清理** — 死 source 移除、空 centroid 重 embed

当前系统运行周期是这样的：

```
用户消息 → E-stage → F-stage → L1 话题摘要 → (存储)
                                                    ↓ (只在下一次 session start)
                                          L2 reality merge → L3 graphify sync
                                                    ↓ (从不)
                                          L4 精炼轮 ← 本设计的空缺
```

### 决策

新增 **L4 空闲精炼管线 v2**：一个运行在后台守护线程中的周期性自我维护流程，在上次精炼以来的跨会话新对话轮数达到阈值后触发精炼轮次。

**二维定位（v2.2，用户 2026-08-07 澄清）**：
- **方向 A（主）——热路径遗留补做**：热路径（L1 摘要 / L2 归并）速度优先，不便用模型做详细数据处理。精炼轮在空闲期补做这些「为了速度而跳过」的详细工作（深度去冗余、跨块归并、一致性核查）
- **方向 B（辅）——全局视野纠偏**：热路径只有单 session 局部视野；精炼轮拥有跨 session 全库视野，用于历史数据优化与纠偏（宁并不分归并、错分 strand 重归属、过碎 reality 合并）

**核心职责 = 纠过碎（宁并不分）+ 内容精炼 + 一致性维护**。

## 处置架构：代码 → 本地 4B → 云端大模型 三级（v2.2 新增）

精炼轮的任务按成本/能力分为三级处置，**默认走最便宜的可胜任层级，只有本级做不了/做不好才升级**——节省云端 token，同时不显著降低精炼质量（flash pilot 已验证云端 deepseek-chat 质量基线；本地 qwen3-4b 为热路径同款模型）。

```
┌─ L1 代码处理（零 token，结构化兜底）
│   规则/统计/DB 操作：候选生成、质量扫描、僵尸清理、健康评分、
│   s2r 重映射执行、timeline 维护、graphify 同步
│   ↓ 需要语义判定时
├─ L2 本地 4B（qwen3-4b-instruct，热路径同款，便宜）
│   承接/延续判定、内精炼、Fct↔Reality 交叉验证
│   ↓ 4B 失败/低置信/超 context/需要全局语义融合时
└─ L3 云端大模型（deepseek-chat，flash pilot 已验证）
    详情重生成（内容层）、两阶段重构决策层（全量紧凑 ref）、
    大 reality 深度融合、4B 修复失败兜底
```

### 任务 → 处置层级映射

| Step | 任务 | 处置层级 | 升级条件（→L3） |
|------|------|----------|-----------------|
| 1 | 并发会话保护 | L1 代码 | — |
| 1.5 | 新 strand 质量扫描 | L1 代码 | — |
| 2a | 归并候选生成（四源） | L1 代码 | — |
| 2b | 承接判定 | L2 4B | 歧义/大 reality（member≥6）/goals 承接不明确 |
| 3 | 内容层详情重生成 | L3 云端 | 默认 L3（深度语义融合，flash 验证） |
| 4 | Reality 内精炼 | L2 4B | 4B 修复后质量仍不达标（low_density 复查） |
| 5 | Fct↔Reality 交叉验证 | L2 4B | 发现矛盾但 4B 无法裁决 |
| 6 | 僵尸清理 | L1 代码 | — |
| 7 | 健康评分 | L1 代码 | — |
| 8 | 知识子图重建 | L1 代码 | 重建 + 社区发现为 L1；共现记录已接线（record_block_cooccurrences@__init__.py:837），历史块缺口可选回填 |
| 8.5 | 事实关联建边 | L1 代码 + L2 4B | 信号 B/C 为 L1/L2（B 承接判定 L2 4B，C OV 检索 L1）；**信号 A bigram v2.7 阈值重定/降级（不得按 ≥2 建边）**；不上 L3 |

**升级原则**：L2 是默认语义层（省 token）；L3 只在三类情况触发——①任务本质需全局语义融合（详情重生成、两阶段重构）；②L2 输出置信度低/解析失败重试仍败；③上下文超 L2 窗口（向量粗筛仍超 → 升 L3）。**L1 永不做语义判定**（结构化提取与兜底是代码职责，LLM 做语义融合——用户编码分工原则）。

## 设计原则

1. **增量触发** — 不是定时跑，而是「新内容够了才跑」。阈值基于全量跨会话新增对话轮之和；**另设过碎信号触发**（单 strand reality 累积数，见触发节）
2. **不碰活跃会话** — 跳过当前插件实例绑定的活跃 session 的 source 数据
3. **幂等** — 同一批数据跑两次精炼结果一致（第二次 no-op）
4. **可观测** — 每轮精炼写入 `refinement_meta` 表，记录做了什么、改了哪些
5. **可中断** — L4 是 daemon 线程，不影响主消息处理。`stop()` 可干净退出
6. **渐进交付** — Phase 1 只做基础设施 + 内容精炼 + 交叉验证。Phase 2 加归并审查。Phase 3 加两阶段重构全量
7. **宁并不分（精炼轮铁律，2026-08-05 用户纠正）** — 与生成阶段「宁分不并」相反。精炼轮纠过碎，边界模糊倾向合并；保留拆裂 = 失职。仅同领域不同任务（工作对象不同）不并
8. **timeline 归代码维护（决策 37 §4.2）** — LLM 返回的 timeline 不可信，merge 时代码恢复旧值 + hdl 更新 append 旧 hdl
9. **member 权威列表 = strand_to_reality 反推** — LLM 返回的 member_strands 会漏 strand，persist 用 s2r 反推
10. **三级处置（v2.2）** — 代码→本地 4B→云端大模型，默认走最便宜可胜任层级，升级仅限：任务本质需全局融合 / L2 低置信或解析失败 / 超 L2 窗口。L1 永不做语义判定

## 存储架构

### refinement_meta 表（ca_topics.db 已有，v2 扩展字段）

```sql
CREATE TABLE IF NOT EXISTS refinement_meta (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    refined_at        REAL    NOT NULL,          -- 精炼轮次完成时间戳
    last_refined_turn INTEGER NOT NULL DEFAULT 0, -- 本轮精炼基于的全局最大轮次
    global_turn_max   INTEGER NOT NULL DEFAULT 0, -- 本轮扫描到的全局最大轮次
    tasks_run         TEXT    NOT NULL DEFAULT '[]', -- 本轮执行的任务名称列表
    entries_reviewed  INTEGER DEFAULT 0,         -- 本轮审核的 reality 数
    entries_modified  INTEGER DEFAULT 0,         -- 本轮实际修改的 reality 数
    entries_split     INTEGER DEFAULT 0,         -- 本轮拆分的 reality 数
    entries_merged    INTEGER DEFAULT 0,         -- 本轮合并的 reality 数（v2 新增）
    s2r_remapped      INTEGER DEFAULT 0,         -- 本轮重归属的 strand 数（v2 新增）
    strand_quality_flagged INTEGER DEFAULT 0,    -- 本轮质量扫描标记的 strand 数（v2.1 新增）
    fcts_cross_checked  INTEGER DEFAULT 0,       -- 交叉验证的 topic 数
    inconsistencies    INTEGER DEFAULT 0,         -- 发现的 Fct↔Reality 不一致数
    associations_added INTEGER DEFAULT 0,         -- 新增关联数
    graphify_synced   INTEGER DEFAULT 0,         -- 是否触发 graphify 同步 (0/1)
    duration_sec      REAL    DEFAULT 0,          -- 本轮耗时
    status            TEXT    NOT NULL DEFAULT 'completed' -- completed|aborted
);
```

### reality 精炼标记（realities 表已有列，决策 41 schema 已含）

```sql
-- realities 表（已存在，不再 ALTER）
--   health_score          REAL DEFAULT 1.0    -- [0, 1]
--   flagged_for_review    INTEGER DEFAULT 0   -- 0|1
--   reviewed_at           REAL
--   topic_count           INTEGER DEFAULT 0   -- 映射的 strand 数（strand_to_reality 计数）
--   last_reviewed_turn    INTEGER DEFAULT 0   -- 上次精炼时的全局 turn
```

## 时序流程

### 空闲循环

```
L4 daemon thread (lazy start, daemon=True)
  │
  ├── 休眠 120s (REFINEMENT_CHECK_INTERVAL)
  │
  ├── _should_run_refinement()
  │   ├── 读 refinement_meta → 取最近一条的 last_refined_turn
  │   ├── 遍历所有 ca_cache/{session_id}.db → 统计全局 max_turn 之和
  │   ├── if (global_max_turn - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS:
  │   │      → start_refinement_cycle()
  │   ├── elif 单 strand reality 数 >= REFINEMENT_SINGLE_STRAND_TRIGGER (v2):
  │   │      → start_refinement_cycle()   # 过碎信号触发归并审查
  │   └── else → 继续休眠
  │
  └── 回顶端
```

### 精炼轮次（单轮）

```
start_refinement_cycle()
  │
  ├── 1. 并发会话保护
  │   └── 扫描活跃 session 列表，排除其 source_strands 中的 reality
  │
  ├── 1.5 新 strand 质量扫描（规则，无 4B；v2.1 新增）
  │   ├── 扫描上次精炼以来新建的 completed strand
  │   ├── 信号2 密度下限：ooda 条目 <3 → low_density
  │   ├── 信号3 超长上限：ooda >15 或单条目 >300 字 → oversize（提问云降权）
  │   ├── 产出：strand_id → 信号集（轮次内计算、轮次内消费，不落 schema）
  │   └── 消费：low_density/oversize → 并入 Step 4 内精炼修复候选
  │       记 refinement_meta.strand_quality_flagged
  │
  ├── 2. Reality 归并审查（v2 核心新增；Phase 2 起，每轮限 REFINEMENT_MAX_MERGES_PER_CYCLE）
  │   ├── 候选生成（五源，宁并不分取向）：
  │   │   ├── a. 单 strand reality 全量（过碎主源，27% 占比）
  │   │   ├── b. 同 session 连续 strand 拆裂（同块/相邻块分属多 reality）
  │   │   ├── c. 向量预筛：reality name+hdl 两两 cos≥0.72（**v2.7 降级为冷启动兜底源**——
  │   │   │      flash 实测 cos≥0.72 仅 8 对，产出极稀；仅当 a/b/d/e 全空时启用，不当常规候选源）
  │   │   ├── d. 跨块 hdl 相同/近似（跨 session/块的同 hdl strand 组；v2.1 新增，
  │   │   │      实测 4 组 11 条全中——比 embedding 更强的同工作线信号，补 flash
  │   │   │      「embedding 预筛不足」缺口；含未归属 strand 如 661）
  │   │   └── e. graphify 社区同簇（v2.3 新增：知识子图重建后跑社区发现，
  │   │          同社区 reality 语义相近 → 归并强候选；前置依赖：悬挂边修复 +
  │   │          社区计算 + 历史共现回填（接线已存在），见「graphify 数据链前置修复」）
  │   ├── 对每个候选 reality：
  │   │   ├── embed 预筛 top-2 承接对象（跨库 name+hdl 云）
  │   │   ├── 构造决策层 prompt：紧凑 ref（name+hdl+goals 前2+member_count，
  │   │   │      全量超 context 时向量粗筛 top-30）
  │   │   ├── 4B 判定：承接/延续（同工作线）→ 合并；同领域不同任务 → 不并
  │   │   ├── goals 承接锚：member_count≥6 只接受明确承接 goals 的 strand
  │   │   └── 输出：s2r 重映射计划（merge target + 迁移 strand 列表）
  │   ├── 执行合并（代码，非 LLM）：
  │   │   ├── s2r 重映射（事务）+ 双端 source_strands 重算
  │   │   │   （含被并入 reality 也要重算为空再删——两次合并均漏，靠事后 DELETE 修复）
  │   │   ├── 空壳 reality DELETE（合并后 0 成员）
  │   │   └── 备份 DB（.bak_pre_refine / .bak_pre_refine2）
  │   └── 记 refinement_meta.entries_merged / s2r_remapped
  │
  ├── 3. 内容层详情重生成（v2；仅对归并审查 affected 的 reality）
  │   ├── affected reality 分批 ≤10
  │   ├── 内容层 prompt：全部成员 ooda → hdl/current_status 与全成员同步
  │   ├── timeline 代码维护：恢复旧值 + hdl 更新 append 旧 hdl（勿信 LLM timeline）
  │   └── member 权威列表 = s2r 反推（勿信 LLM member_strands）
  │
  ├── 4. Reality 内精炼（每 reality 1 次 4B；**v2.7 标注：默认关闭**）
  │   ├── ⚠️ 与 config 对齐：REFINEMENT_INTERNAL_REFINE 默认 False（M5a：internal_refine
  │   │   职责已由归并审查 Step 2 + 详情重生成 Step 3 吸收）——本 Step 仅显式启用时执行
  │   ├── 枚举 realities 中 eligibility 的 reality
  │   │   └── 条件：(source_strands 非空) AND (last_reviewed_turn < last_refined_turn OR IS NULL)
  │   ├── 对每个 reality:
  │   │   ├── 读 reality 当前字段（name/hdl/current_status/timeline）
  │   │   ├── 读该 reality 映射的 strand_summaries（source_strands → strand_id）
  │   │   ├── 构造 4B prompt: reality 内容 + 新 topic 摘要
  │   │   ├── call_llm → 返回: {hdl, key_facts, goals, changes}
  │   │   ├── 与原内容对比差异
  │   │   ├── 有差异 → update_reality + 重 embed centroid + 提问云（如有 query_text）
  │   │   └── 无差异 → 仅更新 last_reviewed_turn
  │   └── 记 refinement_meta.entries_modified
  │
  ├── 5. Fct↔Reality 交叉验证（每 reality 1 次 4B）
  │   ├── ⚠️ v2.7 实现缺口：_CROSS_VALIDATE_PROMPT 仍是 theme 时代 entry 语义
  │   │   （entry_title/entry_facts）——需 Reality 化（reality name/hdl/current_status 替代）
  │   ├── 枚举 reality.source_strands 中所有 session_id
  │   ├── 对每个 session:
  │   │   ├── 读该 session 的 turn_stream → 取最新 Fct 行
  │   │   ├── 与 reality 的 key_facts 做 4B 对比
  │   │   ├── 不一致 → 修正 reality + 记 inconsistency
  │   │   └── 一致 → 跳过
  │   └── 记 refinement_meta.inconsistencies
  │
  ├── 6. 僵尸清理（规则，无 4B）
  │   ├── 读 reality.centroid_json → 空或无法 parse → 重新 embed(name+hdl)
  │   ├── 读 reality.source_strands → JSON parse 失败 → 清空
  │   └── source_strands 中引用的 session DB 不存在 → 从 source_strands 移除
  │
  ├── 7. 更新 reality 元数据
  │   ├── topic_count = 查 strand_to_reality 按 reality_id 统计（决策 41 已用）
  │   ├── health_score = 综合计算（见下方评分 v2）
  │   └── flagged_for_review = 1 when health_score < 0.3
  │
  ├── 8. 知识子图重建（v2.3 升级：原「graphify 增量同步」废弃）
  │   ├── 触发：本轮有 reality 改动（merge/内精炼/重归属）→ 全量重建知识子图
  │   ├── 重建内容（对齐 realities 表为唯一权威）：
  │   │   ├── reality 节点：reality_{id}（label: [知识] {name}）
  │   │   ├── merged_into 边：topic_{session}_S{strand} → reality_{id}（含补建 topic 节点）
  │   │   ├── 清理：被并入 reality 的僵尸节点 / theme_ 残留 / 悬挂边
  │   │   └── 重建后跑 graphify 社区发现 → 供下一轮 Step 2e 候选源
  │   ├── 实现：wiki_to_graph.py 的 build_wiki_subgraph() 已读 realities（决策 41 迁移），
  │   │   会建 reality 节点 + topic 节点 + merged_into 边 + trace 边
  │   │   └ 缺口：merge_into_main_graph 只增不删 → 需补「知识子图一致性清理」
  │   │   （删 DB 已不存在的 reality 节点/theme_ 残留/悬挂边；实测 reality_420 图有 DB 无、
  │   │   9 个 theme_ 残留、592 条悬挂边）——清理后重建，reality 为唯一权威
  │   └── 记 refinement_meta.graphify_synced
  │
  ├── 8.5 事实关联建边（v2.4 新增：reality 间 + reality↔OV 语义关联）
  │   ├── 边类型规范（relation 命名）：
  │   │   ├── shares_topic  — reality↔reality 共享工作对象（弱信号，仅候选线索）
  │   │   ├── depends_on    — reality 承接/依赖另一 reality（强，L2 判定）
  │   │   ├── continues     — reality 延续另一 reality 工作线（强，L2 判定）
  │   │   └── references_ov — reality 引用 OV 数据条目（语义检索命中）
  │   ├── 信号 A：共享 bigram（L1 规则，零 4B；**v2.7 修正：阈值需重定/降级**）
  │   │   ├── reality name/hdl 两两提取中文 bigram，过滤泛词
  │   │   ├── ⚠️ 实测：完整泛词表下「共享 ≥2 非泛词」仅 14 对（窄泛词表 62 对大半是
  │   │   │      泛词残留如『清理/与清/能库』），共享 ≥3 的仅 1 对还是 id 混入假象——
  │   │   │      原阈值拍脑袋，**不得按 ≥2 建边**；提高阈值（≥3）并验证精度后启用，
  │   │   │      否则降级为「候选线索不落图」（不建 shares_topic 边）
  │   ├── 信号 B：承接/引用动词（L2 4B 判定）
  │   │   ├── reality 内容含「基于/承接/详见/延续/参照」动词（实测 11 条）
  │   │   ├── 4B 判定：depends_on / continues / 无关联
  │   │   └── 强边：合并影响分析优先用（邻居查询）
  │   ├── 信号 C：reality↔OV 项目数据语义检索（L1 API 调用，零 4B）
  │   │   ├── reality name/hdl 作 query → OV /api/v1/search/find
  │   │   ├── top-1 命中且相似度 ≥ 阈值 → references_ov 边（node=ov_{resource_id}）
  │   │   ├── ⚠️ v2.7 标注：相似度阈值无先验——先做小样本验证（10 reality 检索看命中
  │   │   │      合理性）再定阈值，不拍脑袋
  │   │   └── 替代 theme 时代 wiki_associations 关键词子串匹配（134K 条 relation 全 related，粗糙冻结）
  │   └── 记 refinement_meta（新增 facts_linked 计数，复用 graphify_synced 触发重建）
  │
  └── 9. 写 refinement_meta 记录
```

### 阶段划分（v2.6 新增：数据修复前置，用户 2026-08-07 确认）

精炼轮执行分两阶段，**数据修复必须先于事实调查**——事实调查（建边/检索）依赖数据质量，脏数据上建边 = 噪声边：

```
阶段 1 数据修复（Step 1.5 + Step 4 + Step 6 + Step 7）
  ├── Step 1.5 质量扫描 → 标记 low_density/oversize strand
  ├── Step 4 内精炼 → 修复标记 strand + timeline overview 空值（F-4 根因修复）
  ├── Step 6 僵尸清理 → 死 source/空 centroid
  └── Step 7 健康评分 → 低分 reality 标记（后续轮修复）
  判定：本轮无 flagged reality/strand 剩余 → 进入阶段 2；否则继续修复（≤3 轮）

阶段 2 事实调查（Step 8 + Step 8.5）
  ├── Step 8 知识子图重建（数据修复的图谱化：悬挂边/僵尸/theme 残留清理）
  └── Step 8.5 事实关联建边（依赖阶段 1 的干净数据）
  判定：图重建后社区质量达标（孤立节点 < 30%）→ 事实调查有效
```

**F-4 timeline overview 空值（数据缺陷根因修复）**：实测 8/8 dict 条目 overview 全空——先查 `run_reality_merge` timeline_entry 构造根因（4B 未返回 timeline_overview 还是字段映射漏），修根因而非补数据（简单修根因原则）。归入 Step 4 内精炼的修复范围。

### Step 8.5 扩展（v2.6：信号 D/E 新增 + 不采用注记）

**不采用注记（防未来重复提出）**：
- **F-2 工具实体建边（uses_tool）**：工具名「出现」≠「使用」（"排查 git 问题"非"用 git 干活"），词面匹配精度不可靠，4B 判定成本高价值低——不符合「容易准确生成的才加」
- **F-5 OV 资源类型化**：依附 references_ov 落地（其相似度阈值未定），不独立——暂缓
- **F-1 reality↔代码文件引用（信号 D，v2.6 曾纳入后撤销）**：代码关联的**权威源是 OV 文档 frontmatter**（`trace: forward/backward` + `source_files`，`wiki_to_graph.py:_parse_trace_frontmatter` 已在用），reality 文本正则解析路径是重复劳动且低精度（实测 29/98 可解析 + 描述文本意图误判）。**正确权威链**：reality → OV 项目数据（references_ov 边）→ OV 文档 frontmatter → 代码。reality 关联 OV 项目数据（设计/决策/测试报告），代码关联由 OV 文档自行维护，CA 不重复建边

**references_ov 关联范围（v2.6 修正）**：信号 C 的目标是 OV 项目数据条目——设计文档（design/）、决策文档（decisions/）、测试/调试报告（testing/）等。reality 通过 references_ov 关联到 OV 条目，代码关联链由 OV 文档 frontmatter 承接到 `wiki_to_graph.py` 的 trace 边（已实现）。

### 图论工具与知识图谱构建（v2.4 新增）

**工具选型（已调研 2026-08-07）**：

| 工具 | 能力 | 精炼轮用途 | 状态 |
|------|------|-----------|------|
| graphify 内置 | community 社区发现、get_neighbors、shortest_path、query_graph BFS/DFS、god_nodes | Step 2e 归并候选（社区）、合并影响分析（邻居/最短路径）、核心节点识别 | ✅ 已有，**零新依赖** |
| networkx | PageRank、中心性、连通分量、稠密度 | 进阶分析：关键 reality 排序、孤立子图检测（**已实证**：116 节点知识子图跑通连通分量/PageRank/度中心性/greedy 社区，3.6.1） | ✅ 已装（`/usr/bin/python3 -m pip install --user --break-system-packages networkx`，2026-08-07） |
| OV /api/v1/search/find | 语义检索 | Step 8.5 信号 C：reality↔OV 建边 | ✅ 已有 |

**知识图谱构建路线（三步渐进）**：
1. **结构层**（v2.3 已设计）：reality 节点 + merged_into 边 + 社区发现 → 知识子图骨架
2. **关联层**（v2.4 新增）：shares_topic/depends_on/continues/references_ov 边 → 事实关联补全
3. **分析层**（图论工具消费）：
   - 社区 = 归并候选源（Step 2e，已接入）
   - god_nodes = 核心 reality（跨领域枢纽，精炼轮优先精炼对象）
   - shortest_path = 影响面分析（merge 前查关联链）
   - networkx PageRank（可选）= 关键 reality 排序（高频被引用者）

**执行顺序依赖（2026-08-07 实证）**：networkx 对 116 节点知识子图跑通全部算法，但当前仅 3 条 reality 间边、95% 孤立节点——**图分析依赖先有边**，必须按 结构层（v2.3 修复悬挂边/清理）→ 关联层（v2.4 建边）→ 分析层 顺序执行，否则分析空转。僵尸节点（reality_420）会污染度中心性/PageRank 结果，一致性清理是前置条件。

**约束**：关联层全部 L1 代码/API（零 4B）或 L2 4B 判定；不上 L3 云端——事实关联判定是结构化信号 + 轻量语义，4B 足够（省 token，符合三级处置原则）。

### graphify 数据链前置修复（v2.3 新增：精炼轮候选源 e 与 Step 8 重建的前提）

2026-08-07 审计（tester 生产库）发现 graphify 数据链状态如下（**v2.3 修正版：共现接线无断链，原「零调用」结论系 grep 函数名错误**）：

| # | 项 | 实测证据 | 状态/处置 |
|---|------|----------|-----------|
| ① | 共现记录接线 | `record_block_cooccurrences`（store.py:1469）**已由 `__init__.py:837` 在 run_reality_merge 后调用**——热路径正常记录（3 行记录 created_at 均为 8/6-8/7 迁移后新数据） | ✅ 已接线，无需修复 |
| ①b | 历史共现缺口 | 452 块中 39 块跨 reality ≥2（应记共现），但历史块（flash 全量重跑/迁移脚本直写 s2r）**未经过热路径 → 漏记** | 🟠 迁移脚本补记（可选）；精炼轮 Step 8 重建可顺带回填 |
| ② | 悬挂边 | graph.json 592/592 条 merged_into 边 source（topic_ 节点）**不存在于图中**——sync 只加边不建节点 | Step 8 重建时补建 topic 节点；或 sync 增加节点补建 |
| ③ | 社区失效 | 125 个知识节点 community **全为 0**——社区发现未作用于知识层 | 知识子图重建后跑 graphify 社区发现 |

**修复后 graphify 对精炼轮的三个价值**：
1. **Step 2e 社区候选源**：同社区 reality 语义相近 → 归并强候选（比向量预筛强——flash 实测 cos≥0.72 仅 8 对，社区是结构信号）
2. **S 匹配分真实数据**：共现边接线后，决策 38 的 S 匹配分热路径不再空转
3. **合并影响分析**：merge 前查被并入 reality 的邻居/社区，评估影响面

### Reality 健康评分 v2（单 strand 过碎信号化）

```python
def _compute_health_score(reality: dict, topic_count: int) -> float:
    score = 1.0
    # 成员数量：单 strand = 过碎信号（精炼轮归并审查候选）
    if topic_count >= 3:
        score *= 1.0
    elif topic_count == 2:
        score *= 0.85
    elif topic_count == 1:
        score *= 0.6   # v2 从 0.7 下调：单 strand 是归并审查优先对象
    else:
        score *= 0.3   # 孤立/空壳
    # 最后更新距今天数：> 30 天降分
    days_since_update = (time.time() - reality.updated_at) / 86400
    if days_since_update > 30:
        score *= 0.8
    # centroid 有效
    if not reality.centroid_json or reality.centroid_json == "null":
        score *= 0.5
    # 有 key_facts 或 goals
    cs = reality.current_status or {}
    if not cs.get("key_facts") and not cs.get("goals"):
        score *= 0.3   # 空壳
    return round(min(max(score, 0), 1), 3)
```

## 配置项（ca/config.py 新增/更新）

```python
# ── L4 空闲精炼（v2）──
REFINEMENT_ENABLED: ClassVar[bool] = False  # 总开关（默认停用，显式启用）
REFINEMENT_CHECK_INTERVAL: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_CHECK_INTERVAL", "120"))
REFINEMENT_MIN_NEW_TURNS: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_MIN_NEW_TURNS", "50"))
REFINEMENT_SINGLE_STRAND_TRIGGER: ClassVar[int] = int(   # v2 新增：过碎信号触发
    os.getenv("CA_REFINEMENT_SINGLE_STRAND_TRIGGER", "20"))
REFINEMENT_MAX_DURATION: ClassVar[float] = float(
    os.getenv("CA_REFINEMENT_MAX_DURATION", "300"))
REFINEMENT_MERGE_REVIEW: ClassVar[bool] = False   # v2 新增：归并审查开关（Phase 2）
REFINEMENT_MAX_MERGES_PER_CYCLE: ClassVar[int] = int(  # v2 新增：单轮归并数上限
    os.getenv("CA_REFINEMENT_MAX_MERGES_PER_CYCLE", "3"))
REFINEMENT_DETAIL_REGEN: ClassVar[bool] = True   # v2 新增：合并后详情重生成
REFINEMENT_INTERNAL_REFINE: ClassVar[bool] = False  # 默认 False：internal_refine 职责已由归并审查/详情重生成吸收（M5a）
REFINEMENT_CROSS_VALIDATE: ClassVar[bool] = True
REFINEMENT_HEALTH_SCORE: ClassVar[bool] = True
REFINEMENT_MAX_ENTRIES_PER_CYCLE: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_MAX_ENTRIES_PER_CYCLE", "5"))
# Graphify 同步（精炼末尾触发）
REFINEMENT_GRAPHIFY_SYNC: ClassVar[bool] = True
```

## 集成点

### 1. IdleRefinementDaemon — ca/refinement.py（已独立于 LStageMixin）

```python
class IdleRefinementDaemon:
    def start(self): ...   # 懒启动单一线程（幂等）
    def stop(self, timeout=5.0): ...  # 设置停止事件，join
    # v2 新增：
    def _run_merge_review(self, conn, active_sessions): ...   # Step 2 归并审查
    def _regen_reality_details(self, conn, affected_ids): ... # Step 3 详情重生成
```

### 2. CA 引擎 — ca/__init__.py hook

在 `_run_session_start_cleanup()` 末尾（L2+L3 之后）新启动 L4 daemon：

```python
if Config.REFINEMENT_ENABLED:
    engine.start_idle_refinement()
```

### 3. 并发会话保护

每轮精炼从 `reality.source_strands` 检查是否引用了活跃 session，如有则跳过该 reality。

## 错误处理

| 场景 | 行为 |
|------|------|
| 4B 调用超时 | 跳过该 reality，不影响其他 reality |
| 所有 4B 均失败 | 本轮 no-op，记 status='aborted'，等待下一轮 |
| 归并判定 4B 解析失败 | temperature 0.2→0.1 重试一次（flash 输出随机漂移） |
| daemon 线程异常退出 | `start_idle_refinement()` 下次重新启动 |
| 进程关机 | daemon=True 自动退出，不阻塞主进程 |
| SQLite 锁竞争（与主线程冲突） | 超时 3 秒放弃该 reality |
| s2r 重映射中途失败 | 事务回滚（备份 .bak_pre_refine 兜底），不产生半合并态 |

## 性能约束

1. **MAX_ENTRIES_PER_CYCLE=5**（默认）：单轮最多精炼 5 个 reality，防止 4B 调用堆积
2. **MAX_MERGES_PER_CYCLE=3**（v2 默认）：单轮最多执行 3 组合并（每组合并含决策层 1 次 4B + 详情重生成 ≤10/批）
3. **单轮 MAX_DURATION=300s**：超时标记 aborted，下一轮继续
4. **CHECK_INTERVAL=120s**：最小休眠间隔，避免空跑
5. **向量粗筛**：决策层全量 ref 超 context 时 embed_batch 候选 top-30（勿全量展开）
6. **内容层分批 ≤10**：详情重生成避免 max_tokens 截断

## 修订记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-07-30 | 初始设计：Phase 1 基础设施 + 内精炼 + 交叉验证（topic_wiki entry 版） |
| v2 | 2026-08-07 | reality 化修订（决策 41）：对象 topic_wiki→realities；新增归并审查（宁并不分，2026-08-05 经验）+ 两阶段重构 + goals 承接锚 + 向量粗筛 + 详情重生成；健康评分单 strand 降分；触发条件加过碎信号；timeline 代码维护 |
| v2.1 | 2026-08-07 | 新增 Step 1.5 新 strand 质量扫描（规则层零 4B：密度下限/超长上限）；归并审查候选源三源→四源（跨块 hdl 相同/近似，实测 4 组 11 条全中，补 embedding 预筛缺口）；refinement_meta 加 strand_quality_flagged |
| v2.2 | 2026-08-07 | 定位澄清（用户）：方向 A 热路径遗留补做（主）+ 方向 B 全局视野纠偏（辅）；新增处置架构——代码→本地 4B→云端大模型三级，任务→层级映射表 + 升级条件；设计原则补第 10 条 |
| v2.3 | 2026-08-07 | graphify 集成：Step 2 候选源四源→五源（e. graphify 社区同簇）；Step 8 升级为知识子图全量重建（废弃只增不删的增量 sync，清理僵尸/theme 残留/悬挂边）；新增「graphify 数据链前置修复」清单（**修正版**：共现接线已存在 record_block_cooccurrences@__init__.py:837，仅历史块漏记可选回填；悬挂边 592 条 + 社区全 0 待修） |
| v2.4 | 2026-08-07 | 事实关联建边：新增 Step 8.5（shares_topic/depends_on/continues/references_ov 四类边；信号 A 共享 bigram 实测 62 对 / 信号 B 承接动词 11 条 L2 判定 / 信号 C OV 语义检索）；新增「图论工具与知识图谱构建」节（graphify 内置零依赖 + networkx 可选 + OV search API；三步渐进：结构→关联→分析） |
| v2.5 | 2026-08-07 | Hindsight 借鉴联动（决策 42 v1）：图路检索（R-1）消费 8.5 事实关联边；心智模型层（R-3）消费社区反思产出——精炼轮产出从「reality 事实」扩展至「跨 reality 洞察」 |
| v2.6 | 2026-08-07 | **整合度修正（用户驱动）+ 数据修复前置**：①阶段划分——数据修复（Step 1.5/4/6/7）先于事实调查（Step 8/8.5）；②F-4 timeline overview 空值根因修复归入 Step 4；③Step 8.5 信号 C 扩为 reality↔OV 项目数据关联；④不采用注记（F-2 工具实体 / F-5 OV 类型化 / **F-1 代码文件引用——代码关联权威源是 OV 文档 frontmatter，CA 不重复建边**）；⑤决策 42 v2：R-1 改注入拣选落点、R-2 改生长序建构约束、R-3 心智模型删除 |
| v2.7 | 2026-08-07 | **系统性审视修正（用户「还有哪些不合适」驱动）**：①信号 A bigram 阈值拍脑袋（完整泛词表重测 62→14 对，≥3 仅 1 对假象）——不得按 ≥2 建边，提阈值或降级候选线索不落图；②Step 4 内精炼标注默认关闭（对齐 config INTERNAL_REFINE=False，M5a 职责已吸收）；③Step 5 prompt 未 Reality 化实现缺口标注（_CROSS_VALIDATE_PROMPT entry 语义）；④候选源 c 向量预筛降级冷启动兜底（flash 实测 8 对产出极稀）；⑤references_ov 相似度阈值标注小样本验证后定（不拍脑袋） |
