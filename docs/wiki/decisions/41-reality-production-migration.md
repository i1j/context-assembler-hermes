# 决策 41：生产 reality 化迁移 + graphify 增量旁路改造（测试计划）

> 状态：待评审（L 级门禁）
> 日期：2026-08-07
> 用户决策：① graphify 重构方案 = 增量旁路改造（保留增量写入，改读 realities 表）② 数据基线 = 导入 flash_pilot 111 reality 按块键映射进生产

## 1. 目标与范围

theme 层退役，reality 层落地生产：

1. **数据迁移**：生产库建 `realities` + `strand_to_reality` 表；flash_pilot 111 reality + 837 s2r 按 (session_id, topic_id) 块键映射导入生产；`cooccurrence_events.reality_a/b` 从 theme_id 迁移为真 reality_id
2. **代码改造**：store.py 新增 reality CRUD；theme.py run_theme_merge → run_reality_merge（S 匹配分保留）；inject.py 改读 realities；graphify_sync.py → sync_realities_to_graph（reality_{id} 节点）；refinement.py L4 精炼 + wiki_to_graph.py 改读 realities；__init__.py 调用点切换
3. **themes 表**：冻结不再写（历史归档保留），theme_strand_map 停用
4. 测试 + 文档 + winker/sysadmin 同步

**不在范围**：flash 重跑管线（flash_reprocess.py）改造；注入 prompt 语义重设计（仅字段适配）；graphify 全量重建机制改动

## 2. 设计决策

### 2.1 realities 表 schema（生产版 = flash schema + 维护字段）

```sql
CREATE TABLE realities (
    reality_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    hdl             TEXT,
    current_status  TEXT    DEFAULT '{}',     -- {"goals":[...], "state":[...]}
    timeline        TEXT    DEFAULT '[]',
    source_strands  TEXT    DEFAULT '{}',     -- 生产版：{"session_id": [strand_id,...]}（对齐 themes 格式）
    profile         TEXT    NOT NULL DEFAULT '',
    centroid_json   TEXT,                      -- embed(name+hdl+current_status+timeline) 均值
    health_score    REAL,                      -- L4 精炼健康分（复用 theme 逻辑）
    flagged_for_review INTEGER DEFAULT 0,
    topic_count     INTEGER DEFAULT 0,
    reviewed_at     REAL,
    last_reviewed_turn INTEGER DEFAULT 0,
    created_at      REAL,
    updated_at      REAL
);
CREATE TABLE strand_to_reality (
    strand_id   INTEGER NOT NULL,             -- 生产 strand_summaries.strand_id
    reality_id  INTEGER NOT NULL,
    PRIMARY KEY (strand_id)
);
```

### 2.2 数据迁移映射（flash → 生产）

```
flash.strands(strand_id → session_id, topic_id)     -- 470 块键
flash.strand_to_reality(strand_id → reality_id)      -- 837 行
生产 strand_summaries(session_id, topic_id → 生产 strand_id)  -- 660 行

映射：flash strand_id → (session_id, topic_id) → 生产 strand_id → 生产 s2r
```

- reality_id 保持 flash 原值 1-111；sqlite_sequence 设 112（后续实时创建不冲突）
- source_strands 从 flash 的 `[strand_id...]`（flash 空间）重写为 `{"session_id": [生产 strand_id...]}`（生产空间）
- 无法映射的行（flash strand 在生产无对应块键）→ 日志记录，不静默丢弃
- 生产 43 个新块（flash 后新增）无 reality 归属 → 待实时 merge

### 2.3 cooccurrence_events 迁移策略

- 现 9 行 reality_a/b 是 theme_id → 经 s2r 映射到 reality_id
- 多对一（theme 内多 strand 属同一 reality）：同对 reality 合并去重（UNIQUE 键重插）
- 无映射 theme（孤儿）→ 丢弃并记录（宁缺勿错，P5 先例）

### 2.4 注入字段适配（pick_injection_themes 读 realities）

| themes 字段 | realities 字段 |
|---|---|
| theme_id | reality_id |
| title | name |
| overview | （并入 current_status.state 摘要） |
| ooda.goals | current_status.goals |
| ooda.current_state | current_status.state |
| ooda.hdl | hdl |
| key_facts / open_items | timeline 摘要 |

注入 prompt 候选格式同步调整（reality 定义：工作线 + current_status + hdl + timeline 摘要）。

### 2.4b 注入端拣选增强：reality 提问云形心散度距离（2026-08-07 实测定稿）

**方案**：动态计算用户提问到各 reality 提问云形心的散度距离，距离范围拣选 → 排序截断 → 4B 判断。

```
pick_injection_realities(query, q_emb, profile, ...):
  ① 提问云：每 reality 成员 strand 的块首提问向量集（summarize 快照 query_text，
     存量回查 state.db turns[0] 定位——tester 505/505 可连）
  ② 形心 C_r = 成员提问向量均值；增量维护 = (旧形心×n + 新提问)/(n+1)
  ③ 动态计算 d(q, C_r) = 1 - cos(q_emb, C_r)（全部 realities）——主度量
  ④ 范围预筛：d ≤ θ_max（安全网，默认 0.5）
  ⑤ 距离升序排序 → 截断 top-15（MAX_CANDIDATES 预算，4B 输入可控）
  ⑥ 4B 拣选 top-3（工作关联判定，空注入允许）；4B 失败 → 距离 top-3 兜底
  ⑦ 冷启动：新 reality 无提问云 → 排除（空注入路径，宁缺勿错）
  ⑧ embed 失败/不可用 → 退化 jaccard bigram 字符兜底（同域 top-3 0.335，字面兜底；
     字符重叠≠语义相关（跨域 0.031 实证），仅作失败兜底不做语义判定）
```

**度量决策（2026-08-07 用户确认）**：主度量 = 形心（1−cos，语义级，top-3 0.393）；jaccard 仅作 embed 失败时字符兜底（同域可用 0.335，但不做语义判定——字符重叠与工作关联正交）。

**实测数据（tester 111 reality / 655 提问，统一留一口径，可评 430 归属对）**：

| 度量 | 归属对 | 非归属对 |
|---|---|---|
| cos 中位 | 0.634 | 0.503 |
| d=1-cos 中位 | 0.366 | 0.497 |

| 方案（top-3 覆盖，留一） | 数值 |
|---|---|
| **提问云形心距离** | **0.393** |
| KMeans(k=20) 聚合（留一） | 0.293（原 0.703 为含自身虚高，2.4 倍偏置实证） |
| 单提问检索（cos 历史提问聚合） | 0.370 |

| 距离范围召回（归属 reality 在 θ 内） | θ=0.4 | θ=0.45 | θ=0.5 |
|---|---|---|---|
| 归属对召回 | 0.721 | 0.858 | 0.930 |
| 范围内候选均值（111 库） | 20.3 | 38.5 | 57.7 |

**参数定稿**：θ_max=0.5（安全网，范围召回 93%）；排序 top-15 截断（4B 输入均值 14.9，覆盖 top-15 归属 71.9%）；4B 做最终 top-3。θ_max 可调（召回 vs 4B 负担），4B 预算由 top-K 控制。

**实现要点**：
- realities 表新增 `query_centroid_json` 列（形心向量）+ `query_count`（云成员数，增量更新用）
- summarize 时快照块首提问 query_text（省每次连 state.db；存量回查已验证可回溯）
- 单成员 reality（32 个）：形心=唯一提问向量，正常运行
- 实验脚本：scripts/exp_query_cloud_centroid.py（分布）/ exp_query_cloud_centroid_topk.py（候选规模）/ exp_query_cloud_compare.py（三方案留一对比）

### 2.5 merge 链路（run_reality_merge）

- 保留 theme.py run_theme_merge 的两段式骨架：S 匹配分候选（决策 38，find_s_candidates 不动）→ 4B 决策 → merge/create
- prompt 换 reality 版：build_create_reality_prompt / build_merge_reality_prompt / build_reality_decide_prompt（reality.py 已有，决策 40 产物）
- 写库：realities upsert + strand_to_reality；字段：name/hdl/current_status/timeline/source_strands + centroid（embed name+hdl+cs+timeline）
- 4B 失败 → 代码兜底（reality.py 已有 _code_fallback 系？无则复用 theme 兜底改造）
- 宁分不并（S>R → new），空注入冷启动逻辑保留

### 2.6 graphify 增量旁路（sync_realities_to_graph）

- sync_themes_to_graph → sync_realities_to_graph：节点 `reality_{id}`、label `[知识] {name}`、merged_into 边（topic_{sid}_S{topic_id} → reality）
- sync_cooccurrences_to_graph：节点 id `theme_{a}` → `reality_{a}`，source_location 已写 reality_id（对齐）
- 调用点 __init__.py:810-827 保持不变（函数签名对齐：reality_ids）

## 3. 代码改动清单

| 文件 | 改动 |
|---|---|
| `ca/store.py` | 新增 create_reality/update_reality/load_all_realities/query_realities_by_semantics/insert_reality_strand_map（从 theme 版改造）；record_block_cooccurrences/query_cooccurrences 不变（表字段已 reality 语义） |
| `ca/theme.py` | run_theme_merge → run_reality_merge（S 匹配分保留，prompt 换 reality 版，写 realities+s2r） |
| `ca/inject.py` | pick_injection_themes 读 realities（字段映射 + prompt 候选格式） |
| `ca/graphify_sync.py` | sync_themes_to_graph → sync_realities_to_graph；cooc 节点 id reality_{a} |
| `ca/refinement.py` | 6+ 处 SQL 改读 realities（_pick_candidates/_update_entry_refinement_meta/_run_health_score/清理逻辑）；health_score 逻辑不变 |
| `scripts/wiki_to_graph.py` | 全量子图构建改读 realities |
| `__init__.py` | 调用点：run_reality_merge / sync_realities_to_graph / cooc 真 reality_id |
| `scripts/migrate_reality_prod.py` | 新增：建表 + flash 导入 + s2r 映射 + cooc 迁移（幂等，可重跑） |

## 4. 测试用例清单

### 4.1 迁移脚本（新增 tests/store/test_migrate_reality.py）
- M1 s2r 映射正确性：flash 470 块键全映射，抽样核对 (session_id, topic_id) 一致
- M2 realities 导入完整性：111 行、字段非空率（对齐 flash 100% 标准）、source_strands 重写正确
- M3 cooc 迁移：9 行 theme 对 → reality 对；多对一合并；孤儿丢弃
- M4 幂等：重跑不产生重复行
- M5 序列：新 reality 自增从 112 开始

### 4.2 store（tests/store/ 或 tests/unit/）
- R1 create_reality/update_reality/load_all_realities 字段完整
- R2 query_realities_by_semantics 余弦排序 + exclude_session（复用 theme 测试改造）
- R3 insert_reality_strand_map 幂等

### 4.3 merge（tests/unit/test_theme_merge.py 改造 → test_reality_merge.py）
- M-1 S 匹配分候选路径不变（注入锚 S=0 必进）
- M-2 merge 写 realities（4B mock 返回 → 断言 realities 行 + s2r 行）
- M-3 create 写 realities（宁分不并）
- M-4 4B 失败 → 代码兜底写 realities
- M-5 空注入冷启动退化余弦

### 4.4 inject（tests/plugin/test_plugin.py 改造）
- I-1 pick_injection_themes 读 realities（mock 4B → 返回 reality 候选）
- I-2 4B 失败 → 余弦 fallback（query_realities_by_semantics）
- I-3 首轮 recall 注入 reality（字段映射后 _format_wiki_carryover 格式）

### 4.5 graphify（tests/ 中 graphify 相关）
- G-1 sync_realities_to_graph 节点 reality_{id}/label/边
- G-2 cooc 边 reality_{a}→reality_{b}
- G-3 幂等合并（重跑不重复）

### 4.6 refinement（tests/ 中 refinement 相关改造）
- F-1 健康评分读 realities（逻辑等价断言：同数据同分）
- F-2 精炼候选挑选排除活跃 session

### 4.7 回归
- 全量 pytest（当前 40 passed 基线 → 目标全绿）
- 行为等价：注入链路端到端 mock 测试

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| flash reality current_status 结构与注入 prompt 期望字段不匹配 | 迁移后抽样 5 reality 人工核对字段；prompt 适配层容错 |
| centroid 重算成本（111 行 embed） | 迁移脚本一次性计算（embed 本地 1024 维，~分钟级） |
| L4 精炼改造量大导致行为漂移 | health_score 逻辑零改动（仅换表），同数据同分断言 |
| cooc theme→reality 多对一策略误并 | 合并去重 + 日志记录每对来源；抽样核对 |
| 生产迁移期间新数据写入 themes（旧链路） | 迁移与代码切换同 commit 原子落地；迁移脚本先跑，代码切换后 themes 冻结 |
| winker/sysadmin 数据基线缺失（sysadmin 无 flash_pilot.db） | 仅同步代码；数据迁移脚本按 profile 参数化，winker 用其 flash_pilot.db（184KB 早期版），sysadmin 待定 |

## 6. 验证方法

1. 迁移后 DB 审计：realities 111 行字段完整率、s2r 全覆盖（strand_summaries 660 中 flash 覆盖部分）、cooc 迁移无孤儿
2. graph.json 重建验证：reality_{id} 节点数 = realities 行数、cooc 边 = cooccurrence_events 行数
3. 全量 pytest + 端到端注入 mock 测试
4. 实机验证（可选）：新会话首轮注入日志确认读 realities

## 7. 执行顺序

阶段1 数据迁移（脚本 + 审计）→ 阶段2 store → merge → inject → graphify → refinement/wiki_to_graph → __init__（每步跑对应测试）→ 阶段3 全量回归 → 阶段4 文档/skill/sync

## 8. 执行结果（2026-08-07 已落地）

| 阶段 | 结果 |
|---|---|
| 1 数据迁移 | `scripts/migrate_reality_prod.py`：realities 111 行（name/hdl/current_status/timeline 111/111 完整）、s2r 574 条（440 直连 + 134 centroid 语义匹配，cos≥0.55 阈值，91.8% 成功率）、s2r↔source_strands 零不一致、cooc 迁移 2 对、提问云形心 97/111（1024 维）、centroid 补算 111/111；备份 `ca_topics.db.bak_pre_reality_20260807` |
| 2 代码改造 | store.py reality CRUD（create/load/update/insert_s2r/query_by_semantics）+ schema 建表；reality.py run_reality_merge（S 匹配分保留 + reality prompt + 防膨胀守卫 + query_centroid 增量）；inject.py pick_injection_realities（提问云形心 d=1-cos 主路径 + θ_max=0.5 范围 + top-15 + 4B + jaccard 兜底）；graphify_sync.py sync_realities_to_graph + cooc reality_{id}；refinement.py L4 全链路 realities；wiki_to_graph.py 全量子图 realities；__init__.py 调用点全切 |
| 3 测试 | 全量 787 passed, 1 skipped, 1 xfailed（recall 测试 mock 切 pick_injection_realities；wiki 子图测试 realities） |
| 4 graph.json | graphify update 重建代码图 → 全量 sync：111 reality 节点 + 574 merged_into 边 + 2 cooc 边（节点数=表行数完全对齐） |

**遗留**：
- themes 表冻结（历史归档，不再写入）；theme_strand_map 停用
- 92 个无归属 strand（60 新块 + 32 低分）→ 实时 merge 消化
- 14 个 reality 无生产成员/形心（flash 成员全映射失败）→ 冷启动排除
- build_wiki_associations（wiki_associations 表）仍读 themes（关联缓存，冻结后无害）
- winker/sysadmin：代码经 sync-ca.sh 同步；数据基线待各自 flash_pilot.db（winker 早期版 184KB / sysadmin 无）
