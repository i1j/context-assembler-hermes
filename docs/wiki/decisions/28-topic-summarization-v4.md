## 背景

CA 插件当前在 topic_switch 时将已完成话题的 Elm（原始消息）打包为 Markdown 提交到 OpenViking，由 OV 的 VLM 管线提炼摘要和结构化记忆。但 OV recall 注入 `<memory-context>` 的质量不满足需求（目录存根、跨 profile 混杂、过时内容、对话无关）。

**决策**：由 CA 自身独立完成话题摘要管线，取代 OV Memory Provider 的角色。
- 摘要直接存入 CA 共享数据库（`ca_topics.db`）
- `<topic_carryover>` 块取代 `<ca-recall>` / `<memory-context>` 注入 conv_history
- 召回从本地 DB 查询，不再依赖 OV `/api/v1/resources/find`

## 层级结构

```
话题（Topic）— 跨会话聚合
  └── 话题块（Topic Chunk）— 单会话中的连续话题段落 ← 摘要作用单位
        ├── 轮次 N:  OODA（fin_1）→ 归入话题块 A
        └── 轮次 N+1: OODA（fin_1）→ 继续话题块 A

topic_switch 触发 → 异步摘要该话题块全部 Fct
                       → 一次 4B 调用 → 结构化摘要存入共享 DB
```

## 设计原则

- **取代 OV Memory Provider**：不再依赖 OV 做话题级摘要和 recall。CA 自身完成生成、存储、召回全链路
- **异步生成**：topic_switch 时标记 pending，post_llm_call 启动后台线程。紧前话题块不会被召回，无需同步阻塞
- **Fct 主输入**：输入以各轮 Fct（OODA）为主，**不包含 Elm**。Elm 不作为标准输入传入 4B
- **4B 边界明确**：只做 4B 能稳定产出的操作（文本合并、受限提取、事实总结），不涉及因果推理/反事实/元认知
- **共享存储**：所有话题摘要存入 `ca_topics.db`（跨会话共享），非 per-session DB
- **不绑 OV 格式**：独立设计存储结构和注入格式，不为搜索/发现优化
- **注入 conv_history**：摘要以 `<topic_carryover>` 块注入 conv_history 的 system 区

## 存储架构

```
~/.hermes/ca_cache/
  ├── sess_a.db           ← turn_stream（per-session，不变）
  ├── sess_b.db           ← turn_stream（per-session，不变）
  └── ca_topics.db        ← strand_summaries 表（共享！）
```

### strand_summaries 表（v6.4 重构，不向前兼容）

> v6.4：摘要单元从话题块（topic）→ strand（事务/工作线）。旧 `topic_summaries` 表废弃，
> 旧数据随 DB 重建丢弃。一个话题块内由 4B 识别 2-6 个 strand，各写一行。

```sql
CREATE TABLE IF NOT EXISTS strand_summaries (
    strand_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    topic_id      INTEGER NOT NULL,      -- 所属话题块（detect 窗口标识）
    profile       TEXT    NOT NULL DEFAULT '',
    hdl           TEXT,                  -- strand 名称（4B 生成，不用 title）
    turns         TEXT    NOT NULL DEFAULT '[]',  -- JSON array [7,8,9]
    ooda_json     TEXT    DEFAULT '{}',  -- {"现象与问题":[...],...}
    changes_json  TEXT    DEFAULT '[]',  -- 扁平聚合（各 ooda 组之和）
    key_facts_json TEXT   DEFAULT '[]',
    centroid_json TEXT,                  -- embed(hdl + ooda 内容)
    status        TEXT    NOT NULL DEFAULT 'pending',  -- completed | skip
    created_at    REAL
);

CREATE TABLE IF NOT EXISTS wiki_strand_map (   -- strand → wiki entry 归并映射
    session_id    TEXT    NOT NULL,
    strand_id     INTEGER NOT NULL,
    entry_id      INTEGER NOT NULL,
    PRIMARY KEY (session_id, strand_id)
);
-- topic_wiki 增加 source_strands 列（替代 source_ids）:
-- {"session_id": [strand_id, ...]}
```

## 时序（三段式触发）

```
┌───── Topic A ────┐ ╔══════ Topic B ══════╗ ╔══ Topic C ═══╗
Turn 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17
                    ↑ topic_switch          ↑ topic_switch
                    (pre_llm_call)          (pre_llm_call)

pre_llm_call — topic_switch 检测到切换
  ├─ 标记 Topic A pending（_pending_topic_summarize）
  ├─ **同步 recall**：查 ca_topics.db 已有话题 → <topic_carryover> 注入
  └─ 继续处理 Topic B

post_llm_call — 触发后台摘要
  ├─ _summarize_topic_chunk_async 线程启动（daemon）
  ├─ 等 F-stage 完成（wait_for_pending）
  ├─ 读 Fct  → 一次 4B 调用
  ├─ 规则处理：hdl, open_items
  └─ 写入 ca_topics.db

on_session_reset（/new）— flush 最后一个话题
  ├─ 如果当前话题有未提交 pending → 同 post_llm_call 流程
  └─ 清理 pending 状态
```

### 非干净退出保护：新会话 turn 1 扫描

**问题**：如果用户直接关终端 / Ctrl+D / 进程杀掉，不经过 `/new`，`on_session_reset` 不会触发，最后一个话题块永不落盘。当前 `_fire_ov_submit` 有同样的问题。

**解决方案**：

```
_on_pre_llm_call_v5, turn == 1 时:
  1. 从 ca_topics.db 查出上次会话的最后一个 topic_id
  2. 如果该 topic_id 没有 completed 摘要 → 标记 pending
  3. 在 post_llm_call 中异步补做
```

补做时打开该 session 的 `ca_cache/{session_id}.db` 读 Fct，与普通摘要流程相同。

### 触发汇总

| 触发 | 行为 | 同步/异步 |
|------|------|----------|
| topic_switch (pre_llm_call) | 标记 pending | 同步（微秒级） |
| post_llm_call | 后台线程: 等F-stage → 4B → 写 ca_topics.db | **异步** |
| on_session_reset (`/new`) | flush 最后一个话题 | 异步 |
| 新会话 turn 1 (pre_llm_call) | 扫描上次会话末话题 → 缺则标记 pending | 异步 |

## 输入

topic_switch 时从 turn_stream 收集该话题块已落盘的 Fct 行：

```
输入:
  └─ 该话题块各轮的 Fct[]（每行 = changes + hdl + stage_tag，
     不含 Elm，不含 assistant thought/tool 中间行）
```

输入不包含：
- Elm（原始消息文本）
- assistant 的 thought/tool 中间行
- tool_call 结果（除非必要配置值，但属特殊情况）

**修订说明**：v3 版本错误地列出了 user Elm 作为输入源之一。v4 修正：user_requests 字段改由**规则**从 Fct.changes 中提取（依赖 Fct 结构中已有的 user_request 子字段，跨轮去重，最多 3 条）。

## 输出

> v6.4 重构：摘要单元从话题块 → **strand**（事务/工作线）。一个话题块内 4B 识别 2-6 个 strand，
> 每个 strand 写入 `strand_summaries` 表独立一行（不向前兼容，旧 topic_summaries 数据丢弃）。
> A-stage 评级机制不动（沿用话题块 grade）。

### L0 — 元数据（不经过 LLM，CA 本地存储）

| 字段 | 来源 | 说明 |
|------|------|------|
| `strand_id` | 规则 | strand_summaries 自增主键 |
| `session_id` | 元数据 | 产生该话题块的 session |
| `topic_id` | 元数据 | TopicGradeManager 分配的话题块 ID |
| `profile` | 元数据 | Hermes profile 隔离 |
| `status` | 规则 | completed / **skip**（根据 4B consumable 判定 + 代码兜底，详见「空洞内容保护」） |

### L1 — strand 载荷（post_llm_call 时一次 4B 调用，每条 strand 一行）

| 字段 | 操作 | 输入源 | 产出方式 |
|------|------|--------|---------|
| `hdl` | ✅ 需要 | 各轮 Fct | **4B**：strand 名称（短名，不用 title）。识别失败降级 = 块 hdl。**v6.4.3 规范**：基于该 strand ooda 内容总结为中文短名（≤30 chars），禁止代码符号名/英文标识符/文件名/函数名；fallback 路径超长自动截断（`_truncate_hdl`，优先标点切段）或降级首条 change 摘要。详见 35 号决策「hdl 命名规范」 |
| `turns` | ✅ 需要 | 各轮轮次号 | **4B**：strand 出现在哪些轮（用 `# 轮次 N` 标注提取） |
| `ooda` | ✅ 需要 | 各轮 Fct.changes[] | **4B**：strand 独立 OODA 四组（现象/背景/决策/后续） |
| `changes[]` | 扁平聚合 | strand 各 ooda 组 | **代码**：strand ooda 四组 concat（0.95 Jaccard 去重） |
| `key_facts[]` | 提炼结论 | 各轮 Fct | **4B**：从 changes 中提炼"确认了什么"的事实结论 |
| `open_items[]` | ✅ 不需要 | stage_tag | **规则**：strand "后续行动"组 concat（为空保留规则提取） |
| `consumable` | **实质性判定** | 各轮 Fct.changes[] | **4B**：布尔字段（块级），判定话题块是否有实质新内容值得入库 |

**4B 调用量**：每个话题块 **1 次调用**（输出全部 strands）。prompt 要求 "Identify 2-6 distinct work strands… When in doubt, split into separate strands"（宁多勿少）。
**num_predict**：`TOPIC_SUMMARY_MAX_TOKENS=4096`（v6.4.2 独立配置，勿复用 F-stage 的 L1_MAX_TOKENS=2048——多 strand 输出会被截断）。

### 空洞内容保护（v6.4 块级判定）

**问题**：部分话题块被检测但无实质新内容，导致空/无意义标题的 topic 被写入 `completed` 并最终生成空洞 wiki entry。

**决策**：在 `_run_topic_summarize` 写入 strand 前增加块级质量检查。4B 输出中新增 `consumable` 布尔字段，配合代码级兜底规则。

**判定逻辑**（`_run_topic_summarize` 中 steps 顺序，v6.4 块级——能识别出 strand 就不可能空洞）：

```
1) 4B 输出 consumable=false           → hollow
2) title 为空 / "无" / "无新增" / "无新内容"  → hollow  （代码兜底）
3) 所有 strand 的 ooda 全空 且 changes=[] 且 key_facts=[]  → hollow  （代码兜底）
```

**判定为 hollow 时的处理**：

```
write_strand_summary(session_id, topic_id, profile,
    hdl=f"skip: {原因}", status="skip", ooda_json="{}", ...)
```

**追溯语义**：

| status | 含义 |
|--------|------|
| `completed` | 已入库，可被 `find_unmerged_strands` 扫描 |
| `skip` | 已评估、无实质内容，跳过 wiki merge |

`find_unmerged_strands` 的 SQL 硬过滤 `WHERE status='completed'`，`skip` 记录不会被扫描。

### 注入格式（conv_history）

```python
{
    "role": "system",
    "content": "<topic_carryover from='{session_id}'>\n"
               "## {topic_title}\n"
               "--- 变更 ---\n"
               "- [已实施] 超时从60s调至30s\n"
               "- [已实施] 连接池从20扩展到50\n"
               "--- 用户需求 ---\n"
               "1. 在Linux机器上测试部署\n"
               "2. 连接池配置参数确认\n"
               "--- 分析结论 ---\n"
               "- 连接池耗尽→级联超时是事故主因\n"
               "- 30s 超时在 batch 场景不够，需独立配置\n"
               "--- 待办 ---\n"
               "- 监控超时命中率\n"
               "- 配置 batch 场景独立超时\n"
               "</topic_carryover>"
}
```

## 召回策略（pre_llm_call 同步）

topic_switch 时从 `ca_topics.db` 同步召回已有话题摘要：

**当前迭代**：按简单规则召回
- 同一 session 内：按 `created_at` 降序取最近 N 个（N=3）
- 跨 session：按 `created_at` 全局降序取最近 N 个
- 过滤：排除刚标记为 pending 的紧前话题块

**未来迭代**：话题块→话题聚合 + 向量检索
- 话题块摘要聚合为话题整体摘要（topic-level summary）
- 向量化召回（需 embedding 索引）

## 代码变更范围

| 组件 | 变更 | 估算 |
|------|------|------|
| `store.py` 新增 `topic_summaries` 表 + `SQLiteTopicStore` | 共享 DB 管理 + CRUD | 80 行 |
| `ca/__init__.py` 新增 `summarize_topic_chunk()` | 输入组装 + 4B 调用 + 输出解析 | 60 行 |
| `ca/__init__.py` 新增 `_summarize_topic_chunk_async()` | 后台线程包装 | 30 行 |
| `ca/__init__.py` 新增 `query_topic_summaries()` | 本地召回取代 `_ov_find_ca_topics` | 40 行 |
| `plugins/ca_assembler/__init__.py` 修改 | pending 标记 + recall 替换 + session_reset flush + turn 1 扫描 | 60 行 |
| 配置清理 | 移除 OV_TOPIC_DIR_PREFIX/OV_TOPIC_SESSION_PREFIX，OV_ENABLED 可改为新开关 | 10 行 |
| 测试 | 4B mock + 输出格式验证 + store CRUD + recall 逻辑 | 80 行 |

**总计**：~360 行新增/修改

## 与现有设计的关系

### 被取代

- **OV VLM 摘要管线对 topic 内容的处理**：不再由 OV 做话题级摘要。CA 自身生成、存储、召回。
- **`_fire_ov_submit` HTTP 上传**：替换为本地 `_summarize_topic_chunk_async`。
- **`_ov_find_ca_topics` HTTP 查询**：替换为本地 `query_topic_summaries()`。
- **`<ca-recall>` / `<memory-context>` 注入**：替换为 `<topic_carryover>`。

### 保留

- `ov_find_ca_topics` 的**切换触发点**（pre_llm_call 中的 topic_switch 逻辑）：保留，仅替换召回实现。
- F-stage 多 OODA 生成 `_topics` 结构：保留，作为 Fct 的增量信息源。
- `_on_session_reset` 的 pending flush 机制：保留，语义不变。
- TopicGradeManager 话题分割/定级：保留。

### 不影响

- Hermes state.db
- turn_stream schema
- E-stage 写即落盘
- F-stage 异步摘要生成
- A-stage conv_history 组装

## 4B 可行性总结

| 操作 | 是否因果推理 | 4B 可信度 | 依据 |
|------|-----------|----------|------|
| changes[] 汇总去重 | 否（文本合并） | ✅ **高** | multi-OODA 实测 10/10 |
| key_facts[] 提炼 | 否（事实总结） | ✅ **中高** | "确认了什么"而非"为什么"。需 5 样本实测 |
| **consumable** | **否（实质性判定）** | ✅ **中** | 4B 输出信号，代码兜底（标题+内容空检查），不单独依赖 |
| user_requests[] | — | ✅ **不调 4B** | v4 改为规则，从 Fct 的 user_request 字段提取 |
| timeline | 删除 | — | 本对话内天然存在，跨会话用话题块粒度即可 |
| rationale | 是（因果） | ❌ | 否决 |
| alternatives | 是（反事实） | ❌ | 否决 |

**待实测**：`key_facts` 的 4B 产出质量。5 个真实话题块样本即可确认。

## 修订记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v3 | 2026-07-27 | 初始设计，取代 OV VLM 摘要 |
| v4 | 2026-07-27 | 修订：输入改为纯Fct（去除Elm）；时序改为异步（不再同步阻塞）；存储改为共享DB `ca_topics.db`；user_requests 改为规则；新增 session 关闭问题 + turn 1 补缺方案；召回改为本地 query |
| v5 | 2026-07-29 | 新增 `consumable` 字段 + 空洞内容保护：4B 输出新增布尔判定，代码兜底检查标题/内容空，不达标则写 `status=skip` 而非 `completed`，跳过 wiki merge |
| v6 | 2026-07-29 | Merge 改为 Jaccard 召回 + 4B 判断（v5.12）：移除 centroid cosine 匹配，改用 `_jaccard_text`（阈值=TOPIC_JACCARD_ENTRY=0.02，与分割统一）。4B 一次调用完成"是否同话题"判断 + 归并 + `has_new_info` 标记。结果：MERGE（有增量） / SKIP（仅记映射） / CREATE（无匹配）。Embedder 维度锁定：`_EMBED_DIM=1024`，`_detect_dimension` 增加维度一致性验证。清理旧数据（17 条 32/48 维 topic_summaries + 4 条 topic_wiki）。 |
| v7 | 2026-07-29 | 决策：entry 内部增删改 + entry 删除推迟到"自我分析改进"流程（空闲时结合 graphify 循环执行）。`judge_and_merge_wiki` 不做内精炼，仅做判断+合并+输入级去重。 |
| v8 | 2026-07-31 | 注入预算 + 迭代提炼：取消 prompt 条数软上限作为体积控制的唯一手段（长话题 6+ 轮 avg 10.63 条被截断）；新增 `max_chars` 注入预算（默认 2000 字符）+ 输入分批记账 remaining + 最多 3 轮迭代融合（轮次喂完即完成，非必跑 3 轮）+ hdl 兜底（质量过滤：len≥8 且非无意义词）。代码层做预算判断与兜底，4B 做融合压缩。 |

## v8: 注入预算与迭代提炼 (2026-07-31)

### 动机

- prompt 软上限（changes≤8 / key_facts≤5）在**生成阶段**丢信息，且不可逆——topic_summaries 入库即截断。562 个 completed 话题注入字符分布：median 362 / p90 889 / max 2468；预算 2000 时仅 2.0% 超限。
- 体积控制是**代码层的确定性职责**，不应让 4B 在生成时承担（"代码处理结构化提取与兜底，LLM 处理语义融合"）。
- 超限话题的主因是**长条目**（4chg 5kf → 2468 字符），迭代融合（合并长条目、删低价值细节）有明确压缩空间。

### 机制

```
summarize_topic_chunk(turns_data, title="", max_chars=2000, max_rounds=3)
  │
  ├─ 规则层 / 代码兜底层：不变（hdl / user_requests / open_items / status /
  │     fallback_changes / fallback_key_facts，均基于全部轮次）
  │
  ├─ 输入分批（防御性）：_split_into_batches(turns_data, INPUT_BUDGET)
  │     输入字符超 INPUT_BUDGET（默认 12000）→ 切分为多批
  │     代码记账 remaining = 未提炼批次          ← "还剩哪些轮次"
  │     （当前数据 10 轮话题 ≈ 9k 字符，一次全喂 → remaining 空）
  │
  ├─ Round 1: 4B 提炼 batch[0] → S1（prompt 带预算指令"输出 ≤ max_chars 字符"）
  │
  ├─ Loop（最多 max_rounds=3，上限保护，非必跑）:
  │     终止条件 = remaining 空（轮次全部覆盖）→ 完成
  │     remaining 非空 → 下一轮: S + remaining.pop(0) → 融合提炼
  │         （融合 prompt: 已有摘要 + 新批次轮次 → 预算内合并，
  │           4B 边融合边压缩，维持 ≤ max_chars）
  │     4B 失败 → 保留上一版 S（或 fallback）
  │
  └─ 终态检查（代码层）:
        remaining 空 且 注入字符 ≤ max_chars → 正常返回
        remaining 空 但 仍超预算 → hdl 兜底（质量过滤后）
```

### 迭代终止语义（v8 澄清）

- **完成条件 = 轮次全部覆盖（remaining 空）**，不是"跑满 3 轮"。
- 3 轮是上限保护（防无限循环 + 防信息损失累积——每轮融合都压缩旧信息）。
- 每轮 prompt 均携带预算指令，融合即压缩；多轮后自然收敛在预算内。
- 兜底：全部轮次覆盖后仍超预算 → hdl（规则生成、稳定、无 4B 依赖）＋ 质量过滤（`len < 8` 或纯确认词/提问句的 hdl 弃用，宁可不注入）。

### 与 8/5 软上限的关系

- 保留 "Keep at most 8 items" 作为 4B 输出的结构引导（合并目标），但体积控制主约束改为预算指令（≤ max_chars 字符）。
- 注入层（format_topic_carryover / 未来 wiki_carryover）的 `[:8]` / `[:200]` 硬截断保留为**最后防线**，不再是主要信息丢弃点。
