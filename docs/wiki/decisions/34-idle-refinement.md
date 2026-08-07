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

**定位**：例行的自我维护，以保证数据健康运行，不处理外部突发事件。**核心职责 = 纠过碎（宁并不分）+ 内容精炼 + 一致性维护**。

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
  ├── 2. Reality 归并审查（v2 核心新增；Phase 2 起，每轮限 REFINEMENT_MAX_MERGES_PER_CYCLE）
  │   ├── 候选生成（三源，宁并不分取向）：
  │   │   ├── a. 单 strand reality 全量（过碎主源，53% 占比）
  │   │   ├── b. 同 session 连续 strand 拆裂（同块/相邻块分属多 reality）
  │   │   └── c. 向量预筛：reality name+hdl 两两 cos≥0.72（字面相似桶，仅线索非结论）
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
  ├── 4. Reality 内精炼（每 reality 1 次 4B）
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
  ├── 8. 如有改动的 reality → 触发 L3 graphify 增量同步
  │   └── sync_realities_to_graph（决策 41 旁路）
  │
  └── 9. 写 refinement_meta 记录
```

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
