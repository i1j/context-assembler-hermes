# 决策 41：生产 reality 化迁移 + graphify 增量旁路改造

> 状态：✅ 已落地（2026-08-07）
> 日期：2026-08-07
> 用户决策：① graphify 重构方案 = 增量旁路改造（保留增量写入，改读 realities 表）② 数据基线 = 导入 flash_pilot 111 reality 按块键映射进生产

## 1. 目标与范围

theme 层退役，reality 层落地生产：

1. **数据迁移**：生产库建 `realities` + `strand_to_reality` 表；flash_pilot 111 reality + 837 s2r 按 (session_id, topic_id) 块键映射导入生产；`cooccurrence_events.reality_a/b` 从 theme_id 迁移为真 reality_id
2. **代码改造**：store.py 新增 reality CRUD；theme.py run_theme_merge → run_reality_merge（S 匹配分保留）；inject.py 改读 realities（提问云形心拣选）；graphify_sync.py → sync_realities_to_graph（reality_{id} 节点）；refinement.py L4 精炼 + wiki_to_graph.py 改读 realities；__init__.py 调用点切换
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
    current_status  TEXT    DEFAULT '{}',     -- {"goals":[...], "current_state":[...], "key_facts":[...], "context":[...]}
    timeline        TEXT    DEFAULT '[]',
    source_strands  TEXT    DEFAULT '{}',     -- 生产版：{"session_id": [strand_id,...]}（对齐 themes 格式）
    profile         TEXT    NOT NULL DEFAULT '',
    centroid_json   TEXT,                      -- embed(name+hdl+current_status) 均值（余弦 fallback 用）
    query_centroid_json TEXT,                  -- 提问云形心（成员 strand 块首提问向量均值，注入主拣选用）
    query_count     INTEGER DEFAULT 0,         -- 云成员数（增量更新用）
    health_score    REAL DEFAULT 1.0,          -- L4 精炼健康分（复用 theme 逻辑）
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

- reality_id 保持 flash 原值（1-178 稀疏，111 行）；sqlite_sequence 设 max+1（后续实时创建不冲突）
- source_strands 从 flash 的 `[strand_id...]`（flash 重跑空间）重写为 `{"session_id": [生产 strand_id...]}`（生产空间）
- **反向逐 strand 映射**（关键坑）：生产同块多 strand（93 块重复，150 多余行）→ dict 键映射只留最后一个 → 必须逐生产 strand 反查键
- **跨 reality 块语义匹配**：flash 同键多 strand 有 78 块分属不同 reality → 生产 strand 用 centroid 语义匹配（cos≥0.55，91.8% 成功率）；hdl bigram 字面匹配不适用（同工作线不同版本 j 仅 0.0-0.16）
- 无法映射的行（低分/新块）→ 宁缺勿错跳过（实时 merge 消化）

### 2.3 cooccurrence_events 迁移策略

- 现 9 行 reality_a/b 是 theme_id → 经 theme.source_strands → s2r → reality 集合（笛卡尔积去重，a<b 规范化）
- 多对一（theme 内多 strand 属同一 reality）：同对 reality 合并去重（UNIQUE 键重插）
- 无映射 theme（孤儿）→ 丢弃并记录（宁缺勿错，P5 先例）

### 2.4 注入字段适配（pick_injection_realities 读 realities）

| themes 字段 | realities 字段 |
|---|---|
| theme_id | reality_id |
| title | name |
| overview | （并入 current_status.state 摘要） |
| ooda.goals | current_status.goals |
| ooda.current_state | current_status.current_state |
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
- 4B 失败 → 代码兜底（_reality_code_fallback_create/merge：strand 数据构造最小 reality / 保留现有+并入现象）
- 防膨胀守卫：enforce_section_limits（决策 40，锚点优先截断）
- 提问云形心增量：_update_query_centroid（merge/create 时新 strand query_text 入云）
- 宁分不并（S>R → new），空注入冷启动逻辑保留
- **theme_ref 直连已停用**（v7 设计，theme.py:559 "v7: 无直连"）——归属完全由 S 匹配分决定；__init__.py:763 越界检查残留（无害）

### 2.6 graphify 增量旁路（sync_realities_to_graph）

- sync_themes_to_graph → sync_realities_to_graph：节点 `reality_{id}`、label `[知识] {name}`、merged_into 边（topic_{sid}_S{strand_id} → reality）
- sync_cooccurrences_to_graph：节点 id `theme_{a}` → `reality_{a}`，label 从 realities.name 读
- 调用点 __init__.py merge 后：sync_realities_to_graph + record_block_cooccurrences（真 reality_id）+ sync_cooccurrences_to_graph

## 3. 代码改动清单

| 文件 | 改动 |
|---|---|
| `ca/store.py` | 新增 create_reality/update_reality/load_all_realities/query_realities_by_semantics/insert_reality_strand_map；schema 建 realities+strand_to_reality 表（新库自动创建） |
| `ca/reality.py` | 新增 run_reality_merge 主流程 + _reality_code_fallback_create/merge + _update_query_centroid |
| `ca/inject.py` | 新增 pick_injection_realities（提问云形心主路径）+ _pick_by_4b + _bigram_jaccard；旧 pick_injection_themes 保留兼容 |
| `ca/graphify_sync.py` | sync_themes_to_graph → sync_realities_to_graph；cooc 节点 id reality_{a} |
| `ca/refinement.py` | 6 处 SQL 改读 realities（_load_refinement_candidates/_refine_single_entry/_update_entry_refinement_meta/_run_zombie_cleanup/_run_health_score）；health_score 逻辑不变 |
| `scripts/wiki_to_graph.py` | 全量子图构建改读 realities |
| `__init__.py` | 调用点：run_reality_merge / pick_injection_realities / sync_realities_to_graph / cooc 真 reality_id |
| `scripts/migrate_reality_prod.py` | 新增：建表 + flash 导入 + s2r 映射 + cooc 迁移 + 提问云形心（幂等，可重跑） |

## 4. 测试用例清单（节选）

- 迁移：s2r 映射正确性、realities 导入完整性、cooc 迁移、幂等重跑、序列自增
- store：reality CRUD 字段完整、query_realities_by_semantics 余弦排序 + exclude_session、s2r 幂等
- merge：S 匹配分候选路径（注入锚 S=0 必进）、merge/create 写 realities+s2r、4B 失败代码兜底、空注入冷启动
- inject：pick_injection_realities 读 realities、4B 失败距离兜底、首轮 recall 注入 reality
- graphify：sync_realities_to_graph 节点/边、cooc reality 边、幂等合并
- refinement：健康评分读 realities（同数据同分）、候选挑选排除活跃 session
- 回归：全量 pytest

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| flash reality current_status 结构与注入 prompt 期望字段不匹配 | 迁移后抽样核对；prompt 适配层容错 |
| centroid 重算成本（111 行 embed） | 迁移脚本一次性计算（本地 1024 维，分钟级） |
| L4 精炼改造量大导致行为漂移 | health_score 逻辑零改动（仅换表），同数据同分断言 |
| cooc theme→reality 多对一策略误并 | 合并去重 + 日志记录每对来源；抽样核对 |
| 生产迁移期间新数据写入 themes（旧链路） | 迁移与代码切换同 commit 原子落地；代码切换后 themes 冻结 |
| winker/sysadmin 数据基线缺失 | 仅同步代码；数据迁移脚本按 profile 参数化 |

## 6. 验证方法

1. 迁移后 DB 审计：realities 字段完整率、s2r↔source_strands 一致性（零不一致）、cooc 无孤儿
2. graph.json 重建验证：reality_{id} 节点数 = realities 行数、cooc 边 = cooccurrence_events 行数
3. 全量 pytest（787 passed）+ 端到端注入 mock 测试
4. 实机验证：新会话首轮注入日志确认读 realities

## 7. 执行顺序

阶段1 数据迁移（脚本 + 审计）→ 阶段2 store → merge → inject → graphify → refinement/wiki_to_graph → __init__（每步跑对应测试）→ 阶段3 全量回归 → 阶段4 文档/skill/sync

## 8. 执行结果（2026-08-07 已落地）

| 阶段 | 结果 |
|---|---|
| 1 数据迁移 | `scripts/migrate_reality_prod.py`：realities 111 行（name/hdl/current_status/timeline 111/111 完整）、s2r 574 条（440 直连 + 134 centroid 语义匹配，cos≥0.55 阈值，91.8% 成功率）、s2r↔source_strands 零不一致、cooc 迁移 2 对、提问云形心 97/111（1024 维）、centroid 补算 111/111；备份 `ca_topics.db.bak_pre_reality_20260807` |
| 2 代码改造 | store.py reality CRUD + schema 建表；reality.py run_reality_merge（S 匹配分保留 + reality prompt + 防膨胀守卫 + query_centroid 增量）；inject.py pick_injection_realities（提问云形心 d=1-cos 主路径 + θ_max=0.5 范围 + top-15 + 4B + jaccard 兜底）；graphify_sync.py sync_realities_to_graph + cooc reality_{id}；refinement.py L4 全链路 realities；wiki_to_graph.py 全量子图 realities；__init__.py 调用点全切 |
| 3 测试 | 全量 787 passed, 1 skipped, 1 xfailed（recall 测试 mock 切 pick_injection_realities；wiki 子图测试 realities） |
| 4 graph.json | graphify update 重建代码图 → 全量 sync：111 reality 节点 + 574 merged_into 边 + 2 cooc 边（节点数=表行数完全对齐） |

**遗留**：
- themes 表冻结（历史归档，不再写入）；theme_strand_map 停用
- 92 个无归属 strand（60 新块 + 32 低分）→ 实时 merge 消化
- 14 个 reality 无生产成员/形心（flash 成员全映射失败）→ 冷启动排除
- build_wiki_associations（wiki_associations 表）仍读 themes（关联缓存，冻结后无害）
- winker/sysadmin：代码经 sync-ca.sh 同步；数据基线待各自 flash_pilot.db（winker 早期版 184KB / sysadmin 无）
