---
title: L4 空闲精炼管线
slug: idle-refinement
category: architecture
version_introduced: v5.14
status: 设计完成（v2 reality 化，2026-08-07）
decisions: [idle-refinement]
depends_on: [l-stage, store, topic_summary, embedding, reality]
updated: 2026-08-07
source_files: ["ca/refinement.py", "ca/store.py", "ca/reality.py"]
---

## 问题

L2 reality merge + L3 graphify sync 仅在 session start 触发。累积的对话轮在会话间从未被精炼，导致：

- realities 可能包含冗余、矛盾、陈旧的信息（hdl/current_status/key_facts/goals）
- 单 strand reality 过碎（生产库 111 reality 中 27% 单 strand；flash pilot 库曾 53%）无归并机制纠偏
- source 数据（Fct）与 reality 内容可能脱节
- 没有健康度评分来标记低质量 reality

## 设计方案

L4 是 CA 的空闲期自我维护管线，实现为插件级 `IdleRefinementDaemon`
（`ca/refinement.py`，由 plugin 在 session-start 懒启动），**不是**消息处理路径的一部分，
也不依附 LStageMixin（LStageMixin 仅保留引擎生命周期管理）。

**v2（2026-08-07）**：精炼对象为 `realities` 表 reality（决策 41 reality 化）。
核心职责从「内容精炼」扩展为「**归并审查（宁并不分）+ 内容精炼 + 一致性维护**」。

**二维定位（v2.2）**：
- 方向 A（主）：热路径遗留补做——L1/L2 速度优先跳过的详细数据处理，空闲期补做
- 方向 B（辅）：全局视野纠偏——跨 session 全库视野做历史数据优化（宁并不分归并、错分重归属）

**三级处置（v2.2）**：代码（L1，零 token）→ 本地 4B（L2，qwen3-4b，默认语义层）→ 云端大模型（L3，deepseek-chat，全局融合/兜底）。默认走最便宜可胜任层级，升级仅限任务本质需全局融合 / L2 低置信或解析失败 / 超 L2 窗口。

### 触发

- 休眠 `REFINEMENT_CHECK_INTERVAL`（默认 120 秒）后检查触发条件
- 条件 A：`(全量 session 的 max_turn 之和 - last_refined_turn) >= REFINEMENT_MIN_NEW_TURNS`
- 条件 B（v2）：单 strand reality 数 >= `REFINEMENT_SINGLE_STRAND_TRIGGER`（过碎信号）
- 达到任一阈值 → 执行一轮精炼；未达到 → 继续休眠

### Phase 1 精炼任务（v2）

| # | 任务 | 类型 | 说明 |
|---|------|------|------|
| 1 | 并发会话保护 | 安全 | 跳过活跃 session 的 source 数据 |
| 1.5 | 新 strand 质量扫描 | 规则 | 零 4B：密度下限（ooda<3）/超长上限（ooda>15、单条目>300 字）→ 标记，并入内精炼修复候选 |
| 2 | Reality 归并审查（Phase 2） | 4B 决策层 | 宁并不分：单 strand（27%）/同 session 拆裂/跨块 hdl 相同/graphify 社区 四常规源 + 向量预筛（冷启动兜底，v2.7）→ 承接判定 → s2r 重映射 |
| 3 | 内容层详情重生成 | 4B 内容层 | 仅 affected reality，分批 ≤10，hdl/current_status 与全成员同步；**生长序约束（v2.6）**：成员 strand 按 turns 升序输入 |
| 4 | Reality 内精炼（默认关闭，v2.7） | 4B | 与 config 对齐（INTERNAL_REFINE=False，M5a 职责已吸收）；仅显式启用时执行 |
| 5 | Fct↔Reality 交叉验证 | 4B | 重读 Fct → 4B 对比 → 修正不一致 |
| 6 | 僵尸清理 | 规则 | 空 centroid → 重 embed；死 source → 移除 |
| 7 | 健康评分 | 规则 | 综合评分 → 低分标记 `flagged_for_review` |
| 8 | 知识子图重建（v2.3） | 脚本 | 全量重建（reality 权威）：补建 topic 节点、清僵尸/theme 残留/悬挂边、重建后跑社区发现供 Step 2e |
| 8.5 | 事实关联建边（v2.4→v2.6） | 规则+4B | shares_topic（bigram 共享，实测 62 对）/depends_on、continues（承接动词 L2 判定）/references_ov（**reality↔OV 项目数据**：设计/决策/测试报告）→ 知识图谱关联层。代码关联不建边——权威源是 OV 文档 frontmatter（trace/source_files），CA 不重复 |

**阶段划分（v2.6，数据修复前置）**：阶段 1 数据修复（Step 1.5 质量扫描 / Step 4 内精炼含 F-4 timeline overview 空值根因 / Step 6 僵尸清理 / Step 7 健康评分）→ 阶段 2 事实调查（Step 8 子图重建 / Step 8.5 建边）。脏数据上建边 = 噪声边，修复必须先于调查。

### 关键铁律（2026-08-05 手工精炼轮 179→140→111 经验）

- **宁并不分**：精炼轮与生成阶段相反——纠过碎，边界模糊倾向合并；仅同领域不同任务不并
- **两阶段重构**：决策层（紧凑 ref + s2r 输出）+ 内容层（affected 分批详情）；决策层全量超 context → 向量粗筛 top-30
- **goals 承接锚**：member_count≥6 只接受明确承接 goals 的 strand（防大 reality 吞新 strand）
- **合并后必须详情重生成**：hdl/current_status 与全成员同步；timeline 代码维护（旧 hdl append，勿信 LLM timeline）
- **member 权威 = strand_to_reality 反推**；执行合并含被并入 reality 重算为空再删（勿漏）
- **质量信号分层（v2.1）**：生成冗余（块内重复）与归并需求（跨块重复）是两类问题——块内重复才是 strand 质量检查对象（实测 0 组），跨块 hdl 相同是归并审查候选源（实测 4 组 11 条），勿混入 Step 1.5
- **graphify 数据链（v2.3 修正版）**：共现记录已接线（`record_block_cooccurrences`@`__init__.py:837` 在 run_reality_merge 后调用）——历史块（flash 全量/迁移直写 s2r）漏记可选回填，非断链；知识子图以 realities 为唯一权威全量重建（增量 sync 只增不删 → 实测 592 条悬挂边 + reality_420 僵尸节点）
- **知识图谱三层（v2.4）**：结构层（reality 节点 + merged_into + 社区）→ 关联层（shares_topic/depends_on/continues/references_ov）→ 分析层（社区=归并候选、god_nodes=核心 reality、shortest_path=影响面）。关联层 L1 代码/API + L2 4B，不上 L3。**执行顺序依赖**：图分析（networkx 3.6.1 已装并实证）依赖先有边——当前 116 节点仅 3 条 reality 间边、95% 孤立，须先修悬挂边/清理（v2.3）再建边（v2.4）

## 存储

- `refinement_meta` 表（ca_topics.db）：记录每轮精炼信息（v2 新增 entries_merged/s2r_remapped 字段）
- `realities` 表（决策 41 schema 已含）：`health_score`、`flagged_for_review`、`topic_count`、`reviewed_at`、`last_reviewed_turn`

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `REFINEMENT_ENABLED` | False | 总开关（默认停用；启用需显式设置） |
| `REFINEMENT_CHECK_INTERVAL` | 120s | 守护线程休眠间隔 |
| `REFINEMENT_MIN_NEW_TURNS` | 50 | 新对话轮触发阈值（≈2× 平均会话长度） |
| `REFINEMENT_SINGLE_STRAND_TRIGGER` | 20 | v2 过碎信号触发阈值 |
| `REFINEMENT_MAX_DURATION` | 300s | 单轮最大耗时 |
| `REFINEMENT_MERGE_REVIEW` | False | v2 归并审查开关（Phase 2） |
| `REFINEMENT_MAX_MERGES_PER_CYCLE` | 3 | v2 单轮归并数上限 |
| `REFINEMENT_MAX_ENTRIES_PER_CYCLE` | 5 | 单轮最多精炼 reality 数 |

详见：[34-空闲精炼管线（v2 reality 化）](../decisions/34-idle-refinement.md)
