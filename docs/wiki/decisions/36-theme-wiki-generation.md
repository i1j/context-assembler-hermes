# 决策：Wiki Theme 生成重构（v6.5，2026-08-01）

## 背景

v6.4 strand 链路落地后，wiki 层仍是**纯规则 + 向量**，无 4B 参与：

| 环节 | v6.4 现状 | 问题 |
|------|----------|------|
| entry 创建 | title = strand hdl 直继承 | 无 4B 生成，标题即 hdl |
| overview | **恒空**（实测 117/117） | 无任何 theme 级语义产物 |
| 归并 | 0.70 余弦 + 字符级 Jaccard 去重 | 无语义融合，93% entry 单 strand |
| 注入 | key_facts-only（`_format_wiki_carryover`） | 无当前状态，信息量低 |

**决策**：参考 strand 生成链路（prompt → 4B → 宽松解析 → 代码兜底），重构 wiki theme 生成。
theme 的定位 = **切换话题时为云端大模型提供背景参考**——数据结构围绕"注入什么"设计，而非记录归并过程。

## 已固化决策（用户 M1-M9 确认，2026-08-01）

1. **归并决策 4B 参与** + 数据模型彻底重构。
2. **同步归并**：theme merge 在 strand 生成时同步执行（`_run_topic_summarize` 尾部）；精炼轮本会话不深挖。
3. **两段式**：先快速归类 strand → theme（向量 0.70 候选），再按 theme 4B 处理。
4. **双 prompt**：首次 create 用结构化组织（类 `_format_turns_for_prompt`）；后续 merge 用融合式（类 `REFINE_SUMMARY_PROMPT`）。
5. **取代** refinement 的 `_REFINE_INTERNAL_PROMPT`（internal_refine 停用；删除留待精炼轮会话）。
6. **中文 + 预算**：输出中文；生成预算 `TOPIC_SUMMARY_MAX_CHARS=4000`；注入预算单 theme 2000 字符。
7. **阈值保留**：0.70 余弦 + `wiki_meta` 自校准（精炼轮 calibrate 继续反推）。
8. **schema 彻底重构**：`themes`/`theme_strand_map` 替代 `topic_wiki`/`wiki_strand_map`，不向前兼容；旧数据备份后丢弃。
9. **TDD**：prompt 构建 / 解析 / 决策矩阵 / 注入裁剪 / 同步接入全覆盖。

## 三级信息分层（核心原则）

theme 数据模型按云端大模型消费方式分层：

| 层级 | 内容 | 载体 | 获取方式 |
|------|------|------|---------|
| **一级（直接注入）** | 当前详细状态：title + overview（摘要）+ **OODA 四组** + key_facts + open_items | themes 表直接字段 | 切换话题时注入 `<wiki_carryover>` |
| **二级（查 wiki 全文）** | 演变时间线（仅 overview 条目，按主题块追加，不设上限）+ 全量 changes | timeline_json / changes_json | 需细究时读取 |
| **三级（图谱）** | 关联节点链接（代码节点 / OV 文档） | wiki_associations + graphify 图 | 查询 theme wiki / graphify |

> **v6.5.2 修正（2026-08-01 实测 140/144 不一致）**：`overview` ≠ `timeline_json` 末条。两者是 4B 独立生成的两个字段——`overview` = 跨时点综合摘要（注入用，P1 优先级）；timeline 末条 `overview` = 该时点状态概述（`timeline_overview`，二级追溯）。原注释「overview = timeline 末条」不准确，以本修正为准。

- **历史不存完整快照**：完整状态只需要当前的（`ooda_json` 覆盖式更新）；历史追溯经 `theme_strand_map` → `strand_summaries`（完整 ooda）。
- **时间线仅 overview**：`{seq, topic_id, turns, session_id, overview}`，按主题块先后追加。

## 归并语义（单向否决）

- **strand 宽进**（宁多勿少）→ **theme 窄出**（宁分不并）→ 系统收敛。
- 向量 0.70 只产候选（≥ 阈值）；**4B 在候选中选一或全拒新建**；4B 不主动拉入 <0.70。
- 例外（真跨 theme 关联被 0.70 误杀）留精炼轮处理。
- 归并方式一行记录：`theme_strand_map.method`（vector | llm | fallback）。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS themes (
    theme_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL DEFAULT '',      -- 一级：锚点（4B 生成，重大转向才更新）
    overview      TEXT    NOT NULL DEFAULT '',      -- 一级：当前摘要（= timeline 末条）
    ooda_json     TEXT    DEFAULT '{}',             -- 一级：当前详细状态（OODA 四组，覆盖式）
    key_facts_json TEXT   DEFAULT '[]',
    open_items_json TEXT  DEFAULT '[]',
    timeline_json TEXT    DEFAULT '[]',             -- 二级：演变时间线（仅 overview 条目）
    changes_json  TEXT    DEFAULT '[]',             -- 二级：全量去重 changes
    centroid_json TEXT,                              -- 召回锚点：embed(overview + ooda + key_facts)
    source_strands TEXT   DEFAULT '{}',             -- 历史追溯指针 {"session_id": [strand_id,...]}
    profile       TEXT    NOT NULL DEFAULT '',
    -- 精炼轮兼容列（v5.14）
    health_score REAL DEFAULT 1.0, flagged_for_review INTEGER DEFAULT 0,
    topic_count INTEGER DEFAULT 0, last_reviewed_turn INTEGER DEFAULT 0,
    reviewed_at REAL, open_items_resolved INTEGER DEFAULT 0,
    updated_at REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS theme_strand_map (
    session_id TEXT NOT NULL, strand_id INTEGER NOT NULL,
    theme_id INTEGER NOT NULL, method TEXT DEFAULT 'vector',
    PRIMARY KEY (session_id, strand_id)
);
```

## 注入优先级（互信息量降序，读者 = 云端大模型）

| 优先级 | 条目 | 理由 |
|--------|------|------|
| P0 | title | 定位锚点，永不裁剪 |
| P1 | overview | 连贯叙述 = 最快建立主题认知的载体 |
| P2 | OODA 决策与方案 | 核心进展 |
| P3 | OODA 现象与问题 | 当前痛点 |
| P4 | OODA 背景与约束 | 为什么这么做 |
| P5 | OODA 后续行动 | 下一步计划 |
| P6 | key_facts | 与决策/现象重叠，信息增量低 |
| P7 | open_items | 与后续行动重叠，信息增量最低 |

裁剪：超预算从 P7 起整段移除；同级内先缩条数（全部→5→3→1）再缩长度（200→120→80）。

## 与 v7.0 theme-based Fct 的关系

本决策是 **theme 层中间步**：Fct 层维持现状（strand 粒度），theme 聚合层 4B 化。
远期 v7.0（Fct changes 按 theme 组织，each theme 自己的 OODA loop）仍需改 Fct 输入钩子。

## 实施状态

- **✅ 已实装（v6.5，2026-08-01）**：TDD 全量 603 passed。
  - `ca/theme.py`：CREATE/MERGE/DECIDE prompt + `format_theme_input` + `run_theme_merge`（两段式）+ 代码兜底 + `parse_theme_response`
  - `_run_topic_summarize` 尾部同步归并（M2）；`_run_wiki_merge` 移除（批量路径）
  - `_format_wiki_carryover` 重构（当前详细状态 + P0-P7 优先级裁剪 + 排版）
  - store：`themes`/`theme_strand_map`/`create_theme`/`load_all_themes`/`update_theme`/`insert_theme_strand_map`/`query_themes_by_semantics`
  - reprocess Step 3 适配 v6.5（按主题块分组 run_theme_merge）
  - refinement 停用（`REFINEMENT_ENABLED`/`REFINEMENT_INTERNAL_REFINE` 默认 false，schema 连锁待下次会话适配）
  - 测试：`test_theme_carryover`(20) / `test_theme_prompts`(15) / `test_theme_decide`(20) / `test_theme_merge`(13) / `test_theme_store`(10)
- 旧数据：`ca_topics.db.bak_v64pre_v7_20260801`（备份参考），验收后丢弃。

## v6.5.x 链路经验（迁移自 AGENTS.md，改前必读）

### merge 策略补充（Centroid 主分配 + 自校准）
- **Centroid 余弦主分配（非 Jaccard）**；Jaccard 仅用于短文本（~300 字）Fct 相邻轮话题分割。
- 自校准阈值：`wiki_meta` KV 表存 `merge_threshold`，初始 0.70（宁分不并），精炼轮 calibrate 反推 `(intra_min+inter_max)/2` 持久化。v6.5 已迁移 theme 级（`ca/theme.py run_theme_merge` 使用）。

### strand 生成时参考 theme（v6.5.3，实验验证后实施）
- 切换时注入的 3 个候选 theme（`plugin._candidate_themes`）随话题块**快照进 pending**（防异步 summarize 覆盖），`summarize_topic_chunk(candidate_themes=...)` 在 prompt 加「候选主题参考」段，4B 生成 strand 时输出可选 `theme_ref`；`run_theme_merge` 对有效 theme_ref 直接归并（跳过向量+decide）。
- **两道校验**：① theme_ref 必须 ∈ 候选（`__init__.py` 丢弃越界，实验发现 4B 会幻觉输出不在候选的 theme_id，如 strand 125→ref=1）；② run_theme_merge 再验 theme 存在。
- 实验（31 次 4B 调用）：正例命中 85%（11/13）、假相似拒绝 83%（5/6）、随机配对"误归"3 例全是真实语义关联（4B 发现当前系统漏合并）——归并倾向精准不贪婪。开销：summarize 输入 +10%（3 候选 × 400 字符），净时间 +2~5%。
- ⚠️ **v7 已停用 theme_ref**（决策 37 P5/D9：被 sim>0 注入污染）——本机制为 v6.5.3 历史经验，改前确认当前版本是否启用。

### HERMES_PROFILE 推导链（v6.5.2 实测）
- 运行时 gateway 只注入 `HERMES_HOME` 不注入 `CA_HERMES_PROFILE` → 旧实现恒 'default' → 运行时 strand/theme 全标错 profile（tester DB 混入 14 strands + 10 themes default）。
- 修复：`Config._detect_profile()` = CA_HERMES_PROFILE env → HERMES_HOME basename → default；`query_themes_by_semantics` 加 `WHERE profile=?`（原参数是死参数）+ `exclude_session_id` 排除当前 session（防 FAR 自注入）。
- **gateway 进程不热加载插件代码**——修改 theme.py/store.py 后运行时仍走旧逻辑，须重启 gateway 才生效（实测修复后运行时 theme changes 仍为 0）。

### reprocess merge-only 必须按 turn 时间顺序（先至后）处理主题块（2026-08-01 实测）
- `find_unmerged_strands` 默认 `ORDER BY created_at`，但 reprocess 批量生成导致 strand.created_at = 写入时间（8/1）≠ turn 真实时间（7/26-8/1，偏差可达 5.3 天）→ 主题块乱序 → 跨时归并错误/漏归并。
- 修复：`scripts/reprocess_old_sessions.py` Step 3 用 `_attach_turn_timestamps` 从 per-session turn_stream.written_at 取主题块锚点（fallback created_at），块按 min(ts) 升序、组内按 ts 升序。验证：133 themes 顺序与 written_at 零违规，后发生 strand 正确归并进先建 theme。

### 接入点与失败兜底
- **接入点**：`_run_topic_summarize` 尾部（strand 写库 + centroid 后）同步调用 `run_theme_merge`——每主题块 1 次决策调用（轻量）+ 每命中 theme 1 次 merge 调用 + 每个新 strand 1 次 create 调用。
- **4B 失败兜底**：merge 调用失败（None）→ 代码兜底合并（保留向量+决策结论，仅追加 timeline_overview）；仅 `merge:false`（明确否决）→ 转 create。
- **create 缺 overview（v6.5.1）**：4B 返回 ooda 但 overview 空（实测 2.2%）→ 重试一次；重试仍缺 → `_pad_overview_from_ooda` 拼接「决策与方案+现象与问题」（≤3 条）。4B 完全失败（None）不重试（走 hdl fallback）。

### schema 与读取铁则
- **schema（不向前兼容）**：`topic_wiki`/`wiki_strand_map` 废弃 → `themes`（title/overview/ooda_json/key_facts/open_items/timeline_json/centroid/source_strands + 精炼轮兼容列）/`theme_strand_map`（method: vector|llm|fallback）。
- **load_all_themes 必须解析 centroid**（json 字符串 → list），否则 `find_theme_candidates` 的 `isinstance(centroid, list)` 全跳过 → 所有 strand 走 create（假性零合并）。已修复（store.py）。
- **精炼轮**：schema 重构后 refinement 曾未适配 themes（读 topic_wiki 会失败），`REFINEMENT_ENABLED`/`REFINEMENT_INTERNAL_REFINE` 默认 false。**v6.5.4 已适配**：refinement.py 7 处 SQL + upsert_wiki_entry 全部改为 themes/theme_strand_map（entry→theme 语义），停用开关不变，启用即可运行。

### graphify 增量同步（v6.5.3）
- `ca/graphify_sync.py` 适配 themes（原 `_graphify_incremental` 读旧表 topic_wiki 已失效）。每次 theme create/merge 后 `run_theme_merge` 返回 `theme_ids` → `_run_topic_summarize` 尾部调 `_graphify_incremental`（带 `_graph_lock` 防并发写）。节点 `theme_{id}`、边 `topic_{sid}_S{strand_id} → theme`（merged_into，与 wiki_to_graph 全量格式一致，幂等）。
- **注意**：增量只补新 theme——schema 变更/批量重跑后需全量同步一次（`sync_themes_to_graph(all_ids, graph_path)` 或 wiki_to_graph.py 适配）。

### wiki 全量重建链路（v6.5.4）
- `scripts/wiki_to_graph.py` 主查询 `FROM topic_wiki` → `FROM themes`（节点 id `theme_{id}` 对齐增量，幂等）；`store.build_wiki_associations` 读 themes + 写入 `wiki_associations.theme_id`（旧库 `entry_id` 列由 `_migrate_wiki_associations_column` 自动 RENAME）。旧 topic_wiki/wiki_strand_map/topic_summaries 死函数（18 个）已从 store.py 清除。

### trace 边匹配策略（v6.5.4 重写）
- OV 文档追溯用 **中文 bigram + log-IDF 加权**（原 `\w+` 整串匹配对中文无分词能力 → trace 恒 0）。规则：bigram 重叠中 `idf≥2.5` 的有效词 ≥2 且加权分 ≥5.0（`话题`2.55/`设计`2.80 达标，`机制`2.17/`数据`2.38 排除）。TRACE_SOURCES 指向 `design/ca-ov-topic-submit.md`（原 wiki/architecture/10-ca-ov-topic-submit.md 已不存在）。
