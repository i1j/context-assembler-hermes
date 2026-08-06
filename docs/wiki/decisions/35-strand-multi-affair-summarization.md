# 决策：Strand 多事务摘要重构（v6.4 方向，2026-07-31）

## 背景

话题摘要（v5.19/v8）的 `ooda_groups` 是**扁平单事务**结构（4 类 OODA 一组），一个话题块 = 一个 OODA 集合。但实测（ms7pwxci6q3ibr topic 4，turns 7-17）一个话题块内实际交织 **5-6 个独立工作线**（wiki 核心粒度设计 / 检索链路重构 / 两阶段 wiki 构建 / 两把尺子原则 / 阈值设定 / 连接池优化）。4B 能稳定识别核心事务（两次实测 5 个主线一致），仅边界事务漂移（连接池有时并入有时独立）。

**决策**：以 **strand（事务/工作线）** 取代话题块作为摘要的基本单元——一个话题块（detect 批处理窗口）内由 4B 识别 2-6 个 strand，每个 strand 有 hdl 名称、turns 轮次元数据、独立 OODA 四组。聚合为主题 wiki 时以 strand 为颗粒融合进最匹配的主题。

## 已固化决策（2026-07-31 用户确认）

1. **命名 strand**：不用 thread（与线程混淆）、affair、transaction。
2. **A-stage 评级机制不动**：继续沿用话题块 grade（`grade_on_switch` → `get_turn_grade`），strand 不参与 A-stage。理由：compress 调用即话题块重构，两者同步发生，strand 异步识别赶不上实时路径。
3. **turn 级取最高等级：取消**（因 strand 不进入 A-stage）。
4. **strand 限于一个话题块内**：跨话题块 = 两个不同 strand，不做跨块关联。
5. **字段用 hdl 不用 title**：strand 名称由 4B 生成，存 hdl 字段。
6. **信息损失是必要代价**：块内截断、跨块不合并，接受。
7. **wiki 重构，不向前兼容**：topic_wiki/wiki_topic_map 旧数据全丢，schema 直接重构（strand_summaries / wiki_strand_map / source_strands）。
8. **strand centroid 参考话题块算法**：embed(hdl + ooda 语义文本)，照搬 `_compute_centroids` / `_grade_topics_by_radius` 思路。
9. **块级空洞判定**：能识别出 strand 就不可能空洞（consumable 语义不变，块级检查）。
10. **strand 识别失败降级为单 strand**：hdl=块 hdl，ooda=fallback。
11. **宁多勿少**：边界内容识别为多个 strand（wiki 聚合时归同一主题），尽量不把多 strand 并成单 strand。
12. **保留话题切换做批处理**：detect 批处理窗口机制保留，strand 在窗口内识别。
13. **向量计算规范**：只 embed 语义文本（hdl/ooda/摘要），永不 embed 原始 Fct JSON。

## 目标数据结构

### 4B 输出（strand 粒度，宁多勿少）

```json
{
  "title": "话题块摘要标题（保留，块级）",
  "strands": [
    {
      "hdl": "wiki entry 核心粒度与字段设计",
      "turns": [7, 8, 9],
      "ooda": {
        "现象与问题": ["..."],
        "背景与约束": ["..."],
        "决策与方案": ["..."],
        "后续行动": ["..."]
      }
    },
    {
      "hdl": "连接池与超时配置优化",
      "turns": [12],
      "ooda": { "...": [] }
    }
  ],
  "key_facts": [...],
  "consumable": true
}
```

prompt 要点：
- "Identify 2-6 distinct work strands in this topic block. **When in doubt, split into separate strands** — prefer over-merging into one."
- "Each strand: hdl (short name), turns (which turns it appears in), ooda (4 groups)"
- 输入 `_format_turns_for_prompt` 已含 `# 轮次 N` 标注，4B 据此提取 turns。

### hdl 命名规范（v6.4.3，2026-08-01 追加）

**问题**：hdl 与 ooda 同批由 4B 输出，但 prompt 仅写 "short name" 无质量约束 → 4B 直接抄输入中的代码符号名，19 个 ms* 会话 164 条 completed strand 中 **79%（130 条）为纯 ASCII 代码符号名**（`embedding_service`、`topic_find_ca`、`consensus`、`l2_clustering`），严重损害 wiki entry title 与语义检索质量。

**修复**：`TOPIC_SUMMARIZE_PROMPT` / `REFINE_SUMMARY_PROMPT` 增加 Fct 风格 hdl 命名规范（参考 `ca/prompts.py` FCT_GENERATION_PROMPT 的写法）：
- 必须先组织该 strand 的 ooda 内容，再基于 ooda 总结 hdl（hdl = ooda 语义总结，非输入符号摘录）
- ✅ 正确示例：`wiki entry 核心粒度与字段设计`、`连接池与超时配置优化`
- ❌ 错误示例（禁止）：`embedding_service`、`topic_find_ca`、`consensus`、`l2_clustering`（代码符号名/英文标识符/文件名/函数名）
- 单次调用内强化，零额外 LLM 成本（用户选定方案）

**验证**（同输入新旧 prompt 对照，qwen3-4b-instruct）：
- 旧 prompt：2 strands，hdl = `embedding_service` / `session_management`（2/2 纯 ASCII 符号名）
- 新 prompt：4 strands，hdl = `语义召回与超时问题修复` / `检索链路本地化与服务解耦` / `聚类结果与图谱同步的本地化规划` / `会话清理逻辑适配`（0/4 纯 ASCII）
- 附带改善：宁多勿少更彻底（2→4 strands）
- TDD：新增 `TestPromptHdlNamingRule` 5 用例（主 prompt 要求基于 ooda 总结 / 禁止代码符号名 / 正反例 / refine prompt 同步），全量 542 passed

### strand 35 超长 hdl 根因与 P0+P1 修复（v6.4.3，2026-08-01）

**现象**：全量重跑后 strand 35（ms4lkp7xm27ocd topic 3）hdl = 91 chars 完整句子（`在 query_topic_summaries 的 step 1 和 step 2 中，通过 session_id!=? → 将 _run_topic_summarize 触发点调整至会话真正结束时...`），且 ooda={}、changes=[]。

**根因（三源验证：日志 + 复现 + 代码）**：
1. 4B 调用**成功**（response 3109 chars）但该次输出退化——**无 strands 且无 ooda_groups**（同输入复现时 4B 输出 4 条正常 strand → 4B 输出不稳定，温度 0.3 仍偶发空结构）
2. 走 `_assemble_summary` 的 `not strands_from_llm` 分支 → 单 strand 包装，hdl=块 hdl（`_build_hdl` 规则拼接首末轮 Fct hdl）
3. `topic_summary.py:596` **缺陷**：`changes = llm_result.get("changes", fallback_changes)`——4B 返回 `changes: []`（空列表）时 `.get` 返回空列表**而非** fallback_changes（48 条）→ `_fallback_ooda_groups([], ...)` 全空 → strand ooda={}
4. `_build_hdl` 无长度约束，拼接的原料（Fct 行级 Hdl）本身是 60+ 字完整句子 → 超长 hdl 落库

**修复（P0+P1）**：
- **P0-1**：`changes = llm_result.get("changes") or fallback_changes`（key_facts 同理）——4B 空列表回退代码提取，fallback ooda 非空
- **P0-2**：`_build_hdl` 输出经 `_truncate_hdl` 截断 ≤30 chars（优先句末标点/逗号切段）；新增 `_HDL_MAX_LEN=30`
- **P1-1**：`summarize_topic_chunk` 循环中 4B 返回退化输出（无 strands 且无 ooda_groups）→ **重试一次**（温度抖动防御）
- **P1-2**：`_fallback_single_strand` hdl 超长 → 降级为首条 change 摘要（`_first_change_summary`，去 stage_tag 前缀）

**验证**：
- TDD 新增 7 用例（`TestAssembleSummaryEmptyChangesFallback`×2 / `TestBuildHdlLengthLimit`×2 / `TestFallbackSingleStrandShortHdl`×1 / `TestSummarizeChunkRetryDegenerate`×2），全量 549 passed
- 真实数据验证（ms4lkp7xm27ocd topic 3）：_build_hdl 91→30 chars、fallback changes 48 条回退、strand ooda 非空、hdl ≤30

### SQLite schema（重构，不向前兼容）

```sql
CREATE TABLE strand_summaries (
    strand_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    topic_id      INTEGER NOT NULL,      -- 所属话题块（detect 窗口标识）
    profile       TEXT    NOT NULL DEFAULT '',
    hdl           TEXT,                  -- strand 名称（4B 生成，不用 title）
    turns         TEXT    NOT NULL DEFAULT '[]',  -- JSON array [7,8,9]
    ooda_json     TEXT    DEFAULT '{}',
    changes_json  TEXT    DEFAULT '[]',  -- 扁平聚合（各 ooda 组之和）
    key_facts_json TEXT   DEFAULT '[]',
    centroid_json TEXT,                  -- embed(hdl + ooda 内容) 均值
    status        TEXT    NOT NULL DEFAULT 'pending',
    created_at    REAL
);

CREATE TABLE topic_wiki (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL DEFAULT '',
    overview      TEXT    NOT NULL DEFAULT '',
    centroid_json TEXT,
    changes_json  TEXT    DEFAULT '[]',
    key_facts_json TEXT   DEFAULT '[]',
    source_strands TEXT   DEFAULT '{}',  -- {"session_id": [strand_id, ...]}
    updated_at    REAL,
    created_at    REAL
);

CREATE TABLE wiki_strand_map (
    session_id    TEXT    NOT NULL,
    strand_id     INTEGER NOT NULL,
    entry_id      INTEGER NOT NULL,
    PRIMARY KEY (session_id, strand_id)
);
```

## 与 v7.0 的关系

v7.0 theme-based Fct 方向（Fct changes 按 theme 组织，each theme has its own OODA loop，消除 Jaccard 分割）是**远期目标**。本决策是**中间步**：Fct 层维持现状，从话题摘要开始分解 strand；v7.0 再改 Fct（需从 elm/输入钩子改起）。

## 实施状态

- 方案文档：`.hermes/plans/2026-07-31_ca-strand-refactor.md`（8 任务，TDD）
- **✅ 已实装（v6.4.1，2026-08-01）**：8 任务全部完成，488 tests passed。
  - `parse_summary_response` 透传 strands/ooda_groups（Bug 1 修复）
  - `_apply_hdl_fallback` 仅降级 title 保留内容（Bug 2 修复）
  - `_assemble_summary` strands 分支 + `_fallback_single_strand` 兜底
  - store schema 三表重构：`strand_summaries` / `wiki_strand_map` / `topic_wiki.source_strands`（旧表 topic_summaries/wiki_topic_map 废弃，不向前兼容）
  - `TOPIC_SUMMARY_MAX_CHARS` 2000→4000（多 strand 体积 ~3174 实测）
  - `__init__.py` `_run_topic_summarize`/`_run_wiki_merge` 适配 strand 粒度；reprocess/evaluate 脚本同步
- **✅ v6.4.2 追加修复（2026-08-01）**：`TOPIC_SUMMARY_MAX_TOKENS=4096` 独立 num_predict——摘要链路原复用 F-stage 的 `L1_MAX_TOKENS=2048`，多 strand 输出被 `stop=length` 截断致 JSON parse 失败（批量重跑 46% 走 fallback）。
- **全量重跑验证（19 个 tester ms* 会话）**：OODA 覆盖率 65%→100%，ooda_other_only 37→0，skip 率 21%→10.9%，孤 strand=0；单轮话题正确拆分 5-7 strand（宁多勿少生效）。

## 摘要链路实现铁则（v6.4 实证，2026-07-31，迁移自 AGENTS.md）

### Jaccard 反例铁则
- 中文语义反转代码层拦不住："是" vs "不是" 仅差 1 字 → Jaccard≈0.92。代码层 ≥0.95 **仅滤笔误级差异**，语义去重归 4B。
- **JSON 键名污染缺陷族**：公共键名/元数据值参与比较 → Jaccard 虚假延续 + centroid 虚高（同源）。修复：`_extract_turn_fct` 归一化（剥键名 + 跳元数据/非字符串），`_compute_centroids` 只 embed 语义文本。
- **铁则：提取点归一化，非比较点；只 embed 语义文本，永不 embed 原始 Fct JSON。**

### topic_split 短消息分割阈值
- `_is_confirmatory_turn` = ≤5 字（经 6 session 验证）。≤3 导致 40-65% 单轮话题（4 字仅 7 个 Jaccard 特征 vs 累积文本 100+ 特征 <0.04）；≤5 减少 ~40% 单轮不过度合并。精炼轮维护排除名单。

### ca_topics.db 路径解析链（独立脚本必读）
- 解析顺序：`hermes_constants.get_hermes_home()` → `HERMES_HOME` env → `~/.hermes` fallback。
- **独立脚本跑 reprocess/evaluate 必须设 `HERMES_HOME=<profile>`**，否则静默写错 DB（profile 根目录会出现 0 字节空 DB 假象）。
- **reprocess 是 INSERT-only 无唯一约束** → 重复跑累积重复数据，干净重跑需先 `mv` 旧 DB。
- tester ms* strand 数据：`profiles/tester/ca_cache/ca_topics.db`。
