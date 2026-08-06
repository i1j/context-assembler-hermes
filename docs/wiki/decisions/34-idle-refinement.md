# 34-idle-refinement.md

## 背景

### 现状

CA 管线目前已覆盖 L1（话题摘要生成）、L2（话题→wiki 归并）、L3（wiki→graphify 同步）。但 L2 和 L3 **仅在 session start 触发一次**，用户连续多轮对话期间积累的话题摘要永不被精炼。

### 遗留问题

v7（28-topic-summarization-v4.md）明确将以下任务贴上「推迟到空闲期自我改进」标签：

1. **Entry 内精炼** — 去冗余、纠错、矛盾合并
2. **Entry 删除** — 过时条目清理
3. **Fct↔Wiki 一致性验证** — source 数据与 entry 内容的交叉检查

当前系统运行周期是这样的：

```
用户消息 → E-stage → F-stage → L1 话题摘要 → (存储)
                                                    ↓ (只在下一次 session start)
                                          L2 wiki merge → L3 graphify sync
                                                    ↓ (从不)
                                          L4 内精炼 ← 这就是本设计要加的空缺
```

### 决策

新增 **L4 空闲精炼管线**：一个运行在后台守护线程中的周期性自我维护流程，在上次精炼以来的跨会话新对话轮数达到阈值后触发精炼轮次。

**定位**：例行的自我维护，以保证数据健康运行，不处理外部突发事件。

## 设计原则

1. **增量触发** — 不是定时跑，而是「新内容够了才跑」。阈值基于全量跨会话新增对话轮之和
2. **不碰活跃会话** — 跳过当前插件实例绑定的活跃 session 的 source 数据
3. **幂等** — 同一批数据跑两次精炼结果一致（第二次 no-op）
4. **可观测** — 每轮精炼写入 `refinement_meta` 表，记录做了什么、改了哪些
5. **可中断** — L4 是 daemon 线程，不影响主消息处理。`stop()` 可干净退出
6. **渐进交付** — Phase 1 只做基础设施 + 内精炼 + 交叉验证。Phase 2+ 再扩展

## 存储架构

### refinement_meta 表（ca_topics.db 新增）

```sql
CREATE TABLE IF NOT EXISTS refinement_meta (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    refined_at        REAL    NOT NULL,          -- 精炼轮次完成时间戳
    last_refined_turn INTEGER NOT NULL DEFAULT 0, -- 本轮精炼基于的全局最大轮次
    global_turn_max   INTEGER NOT NULL DEFAULT 0, -- 本轮扫描到的全局最大轮次
    tasks_run         TEXT    NOT NULL DEFAULT '[]', -- 本轮执行的任务名称列表
    entries_reviewed  INTEGER DEFAULT 0,         -- 本轮审核的 entry 数
    entries_modified  INTEGER DEFAULT 0,         -- 本轮实际修改的 entry 数
    entries_split     INTEGER DEFAULT 0,         -- 本轮拆分的 entry 数
    fcts_cross_checked  INTEGER DEFAULT 0,       -- 交叉验证的 topic 数
    inconsistencies    INTEGER DEFAULT 0,         -- 发现的 Fct↔Wiki 不一致数
    associations_added INTEGER DEFAULT 0,         -- 新增关联数
    graphify_synced   INTEGER DEFAULT 0,         -- 是否触发 graphify 同步 (0/1)
    duration_sec      REAL    DEFAULT 0,          -- 本轮耗时
    status            TEXT    NOT NULL DEFAULT 'completed' -- completed|aborted
);
```

### entry 健康标记（topic_wiki 表新增列）

```sql
-- 新增列（v5.13+）
ALTER TABLE topic_wiki ADD COLUMN health_score REAL DEFAULT 1.0;   -- [0, 1]
ALTER TABLE topic_wiki ADD COLUMN flagged_for_review INTEGER DEFAULT 0;  -- 0|1
ALTER TABLE topic_wiki ADD COLUMN reviewed_at REAL;
ALTER TABLE topic_wiki ADD COLUMN topic_count INTEGER DEFAULT 0;   -- 映射的 topic 数（缓存）
ALTER TABLE topic_wiki ADD COLUMN last_reviewed_turn INTEGER DEFAULT 0;  -- 上次精炼时的全局 turn
ALTER TABLE topic_wiki ADD COLUMN open_items_resolved INTEGER DEFAULT 0; -- 已结项的 open_items 计数值
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
  │   └── else → 继续休眠
  │
  └── 回顶端
```

### 精炼轮次（单轮）

```
start_refinement_cycle()
  │
  ├── 1. 并发会话保护
  │   └── 扫描活跃 session 列表，排除其 source_strands 中的 entry
  │
  ├── 2. Entry 内精炼（每 entry 1 次 4B）
  │   ├── 枚举 topic_wiki 中 eligibility 的 entry
  │   │   └── 条件：(source_strands 非空) AND (last_reviewed_turn < last_refined_turn OR IS NULL)
  │   ├── 对每个 entry:
  │   │   ├── 读 entry 当前所有字段
  │   │   ├── 读该 entry 映射的 strand_summaries（v6.4: source_strands → strand_id → session_id；strand→turns 解析后读 Fct）
  │   │   ├── 构造 4B prompt: entry 原内容 + 新 topic 摘要
  │   │   ├── call_llm → 返回: {overview, changes[], key_facts[], open_items[]}
  │   │   ├── 与原内容对比差异
  │   │   └── 有差异 → upsert_wiki_entry + 重 embed centroid
  │   └── 记 refinement_meta.entries_modified
  │
  ├── 3. Fct↔Wiki 交叉验证（每 entry 1 次 4B）
  │   ├── 枚举 entry.source_strands 中所有 session_id
  │   ├── 对每个 session:
  │   │   ├── 读该 session 的 turn_stream → 取最新 Fct 行
  │   │   ├── 与 entry 的关键 facts 做 4B 对比
  │   │   ├── 不一致 → 修正 entry + 记 inconsistency
  │   │   └── 一致 → 跳过
  │   └── 记 refinement_meta.inconsistencies
  │
  ├── 4. 僵尸清理（规则，无 4B）
  │   ├── 读 entry.centroid_json → 空或无法 parse → 重新 embed(overview)
  │   ├── 读 entry.source_strands（v6.4 起，替代旧 source_ids）→ JSON parse 失败 → 清空
  │   └── source_strands 中引用的 session DB 不存在 → 从 source_strands 移除
  │
  ├── 5. 更新 entry 元数据
  │   ├── topic_count = 查 wiki_strand_map 按 entry_id 统计（v6.4 起，替代旧 wiki_topic_map）
  │   ├── health_score = 综合计算（见下方评分）
  │   └── flagged_for_review = 1 when health_score < 0.3
  │
  ├── 6. 如有改动的 entry → 触发 L3 graphify 增量同步
  │   └── run wiki_to_graph.py（全量重算，增量后续优化）
  │
  └── 7. 写 refinement_meta 记录
```

### Entry 健康评分

```python
def _compute_health_score(entry: dict, topic_count: int) -> float:
    score = 1.0
    # 来源数量：3+ source 为好
    n_sources = len(entry.source_strands or {})
    if n_sources >= 3:
        score *= 1.0
    elif n_sources == 2:
        score *= 0.9
    elif n_sources == 1:
        score *= 0.7
    else:
        score *= 0.3  # 孤立
    # 最后更新距今天数：> 30 天降分
    days_since_update = (time.time() - entry.updated_at) / 86400
    if days_since_update > 30:
        score *= 0.8
    # centroid 有效
    if not entry.centroid_json or entry.centroid_json == "null":
        score *= 0.5
    # topic_count：太少（1-2）意味着证据不足
    if topic_count <= 2:
        score *= 0.7
    # 有 changes 有 facts
    changes = json.loads(entry.changes_json or "[]")
    facts = json.loads(entry.key_facts_json or "[]")
    if not changes and not facts:
        score *= 0.3  # 空壳
    return round(min(max(score, 0), 1), 3)
```

## 配置项（ca/config.py 新增）

```python
# ── L4 空闲精炼 ──
REFINEMENT_ENABLED: ClassVar[bool] = False
REFINEMENT_CHECK_INTERVAL: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_CHECK_INTERVAL", "120"))
REFINEMENT_MIN_NEW_TURNS: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_MIN_NEW_TURNS", "50"))
REFINEMENT_MAX_DURATION: ClassVar[float] = float(
    os.getenv("CA_REFINEMENT_MAX_DURATION", "300"))
REFINEMENT_INTERNAL_REFINE: ClassVar[bool] = True
REFINEMENT_CROSS_VALIDATE: ClassVar[bool] = True
REFINEMENT_HEALTH_SCORE: ClassVar[bool] = True
REFINEMENT_MAX_ENTRIES_PER_CYCLE: ClassVar[int] = int(
    os.getenv("CA_REFINEMENT_MAX_ENTRIES_PER_CYCLE", "5"))
# Graphify 同步（精炼末尾触发）
# 独立开关，与现有 _run_wiki_merge 无关
REFINEMENT_GRAPHIFY_SYNC: ClassVar[bool] = True
```

## 集成点

### 1. LStageMixin — lstage.py 扩展

```python
class LStageMixin:
    def __init__(self):
        self._refinement_daemon: Optional[threading.Thread] = None
        self._refinement_stop = threading.Event()
    
    def start_idle_refinement(self):
        if self._refinement_daemon and self._refinement_daemon.is_alive():
            return
        self._refinement_stop.clear()
        self._refinement_daemon = threading.Thread(
            target=self._idle_refinement_loop,
            daemon=True,
            name="CA-L4-Refinement",
        )
        self._refinement_daemon.start()
    
    def stop_idle_refinement(self, timeout=5.0):
        self._refinement_stop.set()
        if self._refinement_daemon:
            self._refinement_daemon.join(timeout=timeout)
```

### 2. CA 引擎 — ca/__init__.py hook

在 `_run_session_start_cleanup()` 末尾（L2+L3 之后）新启动 L4 daemon：

```python
if Config.REFINEMENT_ENABLED:
    engine.start_idle_refinement()
```

### 3. 并发会话保护

LStageMixin 维护一个活跃 session 列表，L4 提取活跃 session_id 集合：

```python
# LStageMixin 新增
_active_sessions: set[str] = set()

def mark_session_active(self, session_id: str):
    self._active_sessions.add(session_id)

def mark_session_inactive(self, session_id: str):
    self._active_sessions.discard(session_id)
```

每轮精炼从 `entry.source_strands`（v6.4 起，替代旧 source_ids）检查是否引用了活跃 session，如有则跳过该 entry。

## 错误处理

| 场景 | 行为 |
|------|------|
| 4B 调用超时 | 跳过该 entry，不影响其他 entry |
| 所有 4B 均失败 | 本轮 no-op，记 status='aborted'，等待下一轮 |
| daemon 线程异常退出 | `start_idle_refinement()` 下次重新启动 |
| 进程关机 | daemon=True 自动退出，不阻塞主进程 |
| SQLite 锁竞争（与主线程冲突） | 超时 3 秒放弃该 entry |

## 性能约束

1. **MAX_ENTRIES_PER_CYCLE=5**（默认）：单轮最多精炼 5 个 entry，防止 4B 调用堆积
2. **单轮 MAX_DURATION=300s**：超时标记 aborted，下一轮继续
3. **CHECK_INTERVAL=120s**：最小休眠间隔，避免空跑
4. **每 entry 4B 调用 1 次内精炼 + 1 次交叉验证**（Phase 1）— 一轮最多 10 次 4B 调用

## 修订记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-07-30 | 初始设计：Phase 1 基础设施 + 内精炼 + 交叉验证 |
