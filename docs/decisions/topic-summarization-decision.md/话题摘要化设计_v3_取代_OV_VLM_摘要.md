---
> ⚠️ **历史文档（已过时）**：本文为话题摘要设计早期草稿（v3/v4），正式实现见
> `wiki/decisions/28-topic-summarization-v4.md`（v5.19/v8 已实装）。
> **v6.4 起摘要单元已从话题块重构为 strand**——文中 topic_summaries/单话题块结构均已废弃，
> 当前 schema 为 strand_summaries/wiki_strand_map/topic_wiki.source_strands。
> 仅作历史参考，勿据此实现。
---

### topic_wiki + wiki_topic_map 表（L2）

```sql
CREATE TABLE IF NOT EXISTS topic_wiki (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL DEFAULT '',      -- 固定标识，创建时 4B 生成，永不改
    overview      TEXT    NOT NULL DEFAULT '',      -- 动态概要，每次 merge 更新（语义检索锚点）
    centroid_json TEXT,                              -- 每次 merge 后重新 embed(overview + key_facts)
    changes_json  TEXT    DEFAULT '[]',
    key_facts_json TEXT   DEFAULT '[]',             -- 注入给 LLM 的唯一内容
    open_items_json TEXT  DEFAULT '[]',
    source_ids    TEXT    DEFAULT '{}',              -- {"session_id": [topic_id, ...]}
    created_at    REAL,
    updated_at    REAL
);

CREATE TABLE IF NOT EXISTS wiki_topic_map (
    session_id  TEXT NOT NULL,
    topic_id    INTEGER NOT NULL,
    entry_id    INTEGER NOT NULL,
    PRIMARY KEY (session_id, topic_id)
);
```

### 写入策略

写前占位 + 4B 后 UPDATE。崩溃时骨架存在，可恢复。

## L2: Wiki 归并

### 触发

`on_session_start` → CA-Cleanup 后台线程 → `_run_wiki_merge()`。扫描未归入 wiki 的 completed 话题。

### 流程

```
_run_wiki_merge():
  ├── find_unmerged_topics(profile)
  ├── 话题间聚簇（cosine >= 0.75）
  ├── 簇内部 4B 合并（只改 overview/changes/key_facts，不改 title）
  ├── 簇 vs 现有 wiki entry（cosine >= 0.75 匹配）
  │     ├─ 匹配 → 4B 合并（保留原 title 不变）
  │     └─ 不匹配 → 创建新 entry（4B 生成 title，永不改）
  ├── upsert_wiki_entry()
  ├── insert_wiki_topic_map()
  ├── 重 embed centroid(overview + key_facts) → update_wiki_centroid()
  └── L3: wiki_to_graph.py（含 timeline + 关键词匹配 trace 边）
```

## 注入格式

只输出 key_facts，作为纯列表，无 title/overview/changes：

```xml
<wiki_carryover>
- 连接池耗尽→级联超时是事故主因
- 30s 超时在 batch 场景不够，需独立配置
</wiki_carryover>
```

其他字段通过 graphify 节点 metadata 查询，不注入 LLM 上下文。

## 召回策略（pre_llm_call 同步）

语义检索锚点为 **overview**（动态概要），在每次 merge 时重新 embed centroid：
- centroid = embed(overview + key_facts)
- query_wiki_by_semantics() 匹配的是合并后的 overview，精度更高
- 不退回时间排序——无语义匹配时空返回

## Graphify 关键词匹配

`wiki_to_graph.py` 从 OV API 读取设计/架构文档的 title，与 wiki entry title 做关键词重叠匹配：

- 提取各 title 的中英文关键词（`re.findall(r'[\w\u4e00-\u9fff]+', title)`）
- 重叠 ≥ 2 且重叠比例 ≥ 30% → 生成 `trace` 边（wiki entry → OV 文档）
- confidence_score = 0.85（关键词匹配，语义匹配更高）

在图中追溯路径：
```
wiki entry → trace → OV 文档（设计/架构）
             trace → 代码文件（通过 frontmatter trace.forward）
             trace → 上游决策（通过 frontmatter trace.backward）
```
