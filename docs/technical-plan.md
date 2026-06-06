# ContextAssembler 详细设计文档 (v4.5.1)

## 文档信息

| 项目         | 内容                             |
| ------------ | -------------------------------- |
| 文档版本     | 合并 v4.3.4 + v4.4.0 + v4.4.1 补充 |
| 对应需求文档 | Software_REQUIREMENTS.md v4.4.0   |
| 编制日期     | 2025-06-27（原始）/ 2026-06-05（v4.4.1 修订） |

---

## 1. 设计概述

### 1.1 设计目标

为 Hermes Agent 构建高可靠性、可观测的上下文记忆系统，核心策略为 **增量 L0/L1 摘要 + 本地 BM25/向量双路检索 + Token 预算闸门 + 全指纹去重**。

v4.4.0 在对话轮摘要、双路检索、硬截断等能力基础上，新增两大特性：

- **工具轮摘要**：规则引擎生成工具调用轮次的 L1/L0，不经 LLM。
- **L‑stage 异步补全**：独立后台阶段修复缺失的摘要（对话轮 + 工具轮）。

v4.4.1 在此基础上解决了对话轮与工具轮尾区混用、Token 估算偏差、上下文窗口查询过时、后台审查轮浪费 LLM 调用等问题，并新增话题边界检测与 turn_plan 可观测性。

### 1.2 关键设计原则

| 原则       | 实现方式                                                          |
| ---------- | ----------------------------------------------------------------- |
| 两阶段解耦 | C‑stage 异步生成摘要，A‑stage 同步组装上下文                    |
| 防御性工程 | 别名匹配、容错解析、截断清洗、深度规范化指纹                      |
| 轻量化     | SQLite 持久化，自实现 BM25、余弦相似度、向量序列化                |
| 可观测性   | 健康检查、Prometheus 指标、阶段统计、turn_plan 拣选决策记录       |
| 集中配置   | 所有可调参数通过 `config.py` 统一暴露，支持环境变量覆盖和热重载 |
| 线程安全   | 关键数据结构加锁，异步任务可追踪，资源泄漏已消除                  |
| 职责分离   | Plugin 层管理生命周期与断路器，引擎层专注上下文处理               |
| 弹性容错   | 失败冷却、连接池过期重试、WAL 模式校验、主动容量驱逐              |
| 对话/工具分离 | 对话轮与工具轮采用独立拣选策略，尾区保护各自独立                |

---

## 2. 系统架构

### 2.1 逻辑架构

```
┌────────────────────────────────────────────────────────────┐
│                      Hermes Agent                          │
│                                                            │
│  ┌─────────────────────────┐  ┌──────────────────────────┐ │
│  │ C‑stage (post_llm_call) │  │ A‑stage (pre_llm_call)   │ │
│  │       异步              │  │       同步               │ │
│  │                         │  │                          │ │
│  │ L2 → LLM 生成 OODA 文本 │  │ 用户输入                 │ │
│  │     → OODAParser 解析   │  │  → 获取快照+数据副本     │ │
│  │     → 清洗+提取 L0      │  │  → BM25/向量检索 (RRF)   │ │
│  │     → 嵌入(L1,L0)       │  │  → 动态预算闸门         │ │
│  │     → 写入 SQLite       │  │  → Head/Middle/Tail 组装 │ │
│  │     → 更新 AssemblyCache│  │  → 全指纹去重(原位标记) │ │
│  │     → 工具轮规则摘要    │  │  → turn_plan 拣选记录   │ │
│  │     → 话题边界检测      │  │  → turn_plan 驱动组装   │ │
│  │     → 预选工具轮        │  │                          │ │
│  └───────────┬─────────────┘  └──────────┬───────────────┘ │
│              │                           │                 │
│              └───────────┬───────────────┘                 │
│                          ▼                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ L‑stage (守护线程) · 异步补全                        │  │
│  │ 扫描 _assemble_status=1 → 对话 LLM 重生成 / 工具规则  │  │
│  └──────────────────────────────────────────────────────┘  │
│                          ▼                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ 基础设施：config / store / cache / retrieval /       │  │
│  │           embedding / ooda_parser / post_process     │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

### 2.2 模块依赖与职责划分

```
plugins/ca_assembler/__init__.py
│   职责：生命周期管理、Hermes hook 注册、断路器状态读写、异常拦截短路
│
└── ca/__init__.py (ContextAssembler)
    │   职责：上下文组装核心逻辑（C/A/L 三阶段），不感知断路器
    │
    ├── config.py            ← 集中配置 + 校验 + 热重载
    ├── health.py            ← 健康检查 + Prometheus 指标
    ├── store.py (SQLiteStore) ← 持久化（turn_cache + turn_plan），WAL 模式，线程安全
    ├── cache.py (AssemblyCache) ← 内存缓存 + BM25 快照（含工具轮独立索引）
    ├── retrieval.py (Retriever) ← 双路检索（对话 + 工具）+ RRF 融合
    ├── embedding.py (EmbeddingClient) ← 嵌入服务，多后端，LRU 缓存
    ├── ooda_parser.py       ← OODA 解析 + 向量语义去重
    ├── post_process.py      ← JSON 容错解析 + 清洗
    ├── prompts.py           ← L1 生成 Prompt
    ├── tool_summarizer.py   ← 工具轮规则引擎摘要
    ├── tool_field_priority.yaml ← 工具字段优先级配置
    ├── lstage.py            ← L-stage 异步补全线程
    └── stats.py             ← 阶段性能统计 (AssembleStats)
```

**关键边界**：

- Plugin 层全权负责断路器的读写与短路判断，引擎层**不感知**。
- `AssemblyCache` 对外提供不可变快照 `BM25Snapshot`（含独立的 `tool_bm25`、`tool_turn_keys`、`tool_l1_embeddings`），及线程安全数据副本。
- `AssembleStats` 嵌入 `assemble` 流程，对每个阶段计时并汇总。

---

## 3. 数据结构

### 3.1 turn_cache 表（schema v3）

主键：`(session_id, turn_index, turn_type, tool_sub_index)`

| 列名                  | 类型    | 默认值     | 说明                                                                 |
| --------------------- | ------- | ---------- | -------------------------------------------------------------------- |
| `session_id`        | TEXT    | —         | 会话 ID                                                              |
| `turn_index`        | INTEGER | —         | 对话轮次（从 1 开始）                                                |
| `turn_type`         | TEXT    | 'dialogue' | 区分对话轮（dialogue）与工具轮（tool）                               |
| `tool_sub_index`    | INTEGER | 0          | 同一轮次内工具组的序号，对话轮恒为 0，工具轮从 1 递增                |
| `l2_text`           | TEXT    | NULL       | 原始 L2 对话文本（递增快照），供 L‑stage 补全使用                    |
| `l1_text`           | TEXT    | —         | 结构化摘要 JSON                                                       |
| `l0_text`           | TEXT    | —         | 单行摘要（截断至 100 字符）                                           |
| `l0_embedding`      | BLOB    | —         | L0 嵌入向量（4096 字节）                                              |
| `l1_embedding`      | BLOB    | —         | L1 嵌入向量（4096 字节）                                              |
| `bm25_tokens`       | TEXT    | —         | BM25 分词                                                             |
| `token_offset`      | INTEGER | —         | 累计 Token 偏移                                                       |
| `_assemble_status`  | INTEGER | 0          | 摘要生成状态：0=正常，1=C‑stage 失败待补全，2=L‑stage 永久失败       |
| `backfill_attempts` | INTEGER | 0          | L‑stage 补全重试次数                                                  |
| `created_at`        | TEXT    | —         | 创建时间戳                                                            |

### 3.2 turn_plan 表（schema v3 新增）

记录每次 `assemble()` 的拣选决策，用于调试比对和未来基于 turn_plan 的拣选组装。

```sql
turn_plan(session_id, turn_index, turn_type, tool_sub_index,
          target_level, decision_reason,
          l2_tokens, summary_tokens, tokens_saved,
          rrf_score, upgrade_rank, budget_remaining,
          topic_group)
```

| 字段 | 说明 |
|------|------|
| `target_level` | 最终呈现级别：L2（原文）/ L1（摘要）/ L0（一行） |
| `decision_reason` | 枚举：`head` / `tail` / `retrieved` / `pre_upgraded` / `tool_head` / `middle` |
| `tokens_saved` | 相比保留 L2 节省的 token 数 |
| `topic_group` | 话题分组 ID（由 C-stage 话题边界检测写入） |

### 3.3 AssemblyCache 扩展

增加工具轮相关字典，键为 `(turn_index, sub_index)` 复合键：

- `tool_l0_texts`, `tool_l1_texts`：工具轮 L0/L1 文本。
- `tool_l0_embeddings`, `tool_l1_embeddings`：对应的嵌入向量。

快照 `BM25Snapshot` 新增属性：

- `tool_bm25`：工具轮 BM25 索引（独立于对话轮）。
- `tool_turn_keys`：工具轮复合键列表。
- `tool_l1_embeddings`：工具轮 L1 嵌入字典。

---

## 4. C‑stage 详细设计

### 4.1 异步流程

1. `process_turn_async` 在锁内计算 `turn_index = max(内部计数器+1, 历史用户消息数)`，检查重复任务。
2. **后台审查检测**（v4.4.1）：主线程读取 `tools.skill_provenance.get_current_write_origin()` ContextVar。若为 `"background_review"`，将标志传入 daemon 线程（ContextVar 不跨线程传播），跳过 LLM 调用，规则生成结构化 L1：`{"core_change": "系统后台审查", "_assemble_status": 0, ...}`。
3. 正常流程：启动后台线程执行 `_run_c_stage`：
   - 调用 `_call_llm_for_l1` 生成 OODA 文本（带重试，降级时返回完整五节"无有效增量"）。
   - `OODAParser.parse` 解析 OODA 文本，并与上一轮 L1 做向量语义去重。
   - `robust_json_parse` + `clean_increment` 容错清洗。
   - 提取 L0（`core_change` 前 100 字符）。
   - 并行嵌入 L1、L0（失败时置 None）。
   - **话题边界检测**（v4.4.1）：当前轮 L1 向量与上一对话轮 L1 向量计算余弦相似度，低于 `TOPIC_BOUNDARY_DISTANCE`（默认 0.50）→ 递增 `topic_id`，通过 `store.upsert_turn_plan_topic()` 持久化。
   - `SQLiteStore.write_turn` 持久化。
   - `AssemblyCache.add_turn` 更新内存缓存，异步触发快照重建。
4. **工具轮处理**：检测本轮消息中的工具调用 → `_extract_tool_groups` 分组 → `ToolSummarizer.summarize_group` 生成 L1/L0 → 存储到 `turn_cache`（`turn_type='tool'`）→ 更新 `AssemblyCache` 工具轮字典。
5. **预选工具轮**：以对话 L1 的 `core_change` 为 Query，在工具轮 BM25 索引中检索 Top-K，标记为预选（`_pre_upgraded_tool_turns`），供下一轮 A-stage 使用。
6. 异常时写入"无有效增量"降级记录（`_assemble_status=1`），更新缓存。
7. 无论成功或失败，均触发 L-stage 补全线程。

### 4.2 工具组提取

`_extract_tool_groups` 将消息列表中的 `tool_calls` 消息及其后续 `tool` 响应消息划分为独立工具调用组。

- 一个 `assistant` 消息（含 `tool_calls`）及紧随其后的所有 `role: tool` 消息视为一个组。
- 每个组分配一个 `sub_index`，从 1 开始递增（对话轮恒为 0）。
- C‑stage 和 A‑stage 均通过遍历消息列表中的工具组顺序分配 `sub_index`，确保跨阶段复合键 `(turn_index, sub_index)` 绝对匹配。

### 4.3 工具轮摘要（ToolSummarizer）

**L1 模板字段**：

| 字段                   | 说明                                     | 缺失默认值                                                                          |
| ---------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------- |
| `tool_name`          | 工具名（工具组取第一个）                 | `"unknown_tool"`                                                                  |
| `tool_args`          | 关键参数                                 | `{}`                                                                              |
| `thought_process`    | 思维链（来自 assistant 消息的 content）  | `""`                                                                              |
| `result_summary`     | 结果摘要                                 | 优先提取 result/summary/message 等字段；若无则为 `"无返回数据"`                    |
| `error`              | 错误信息                                 | `null`；若工具返回 error 字段则填充，且 result_summary 标记为 `"失败"`          |
| `implicit_knowledge` | 隐含知识（工具组中多调用的结果合并在此） | `[]`                                                                              |
| `next_action_hint`   | 后续建议                                 | `""`                                                                              |

**L0 生成规则**：`[tool_name]：[result_summary 或 error]`，截断至 100 字符。若 `thought_process` 非空且 ≤30 字符，可追加 `(thinking: ...)`。

**字段优先级配置**：YAML/JSON 配置文件，分为 VIP（全文保留）、P0（头尾截断）、P1（仅名称）、P2（丢弃）四级。支持 Profile 机制。

### 4.3.1 按工具类型结构化摘要（结构化 handler）

**问题**：通用字段提取逻辑对所有工具一视同仁，但 terminal/read_file 等工具的原始输出包含大量行级数据，`_head_tail_truncate` (120 字符) + L0 `[:100]` 硬截断产生不可读的碎片。

**方案**：`ToolSummarizer.summarize()` 入口通过 `getattr(self, "_summarize_{tool_name}", None)` 分发到专用 handler。未匹配的工具走原通用逻辑。

**当前 handler（10 个）**：

| 工具 | Handler（行号） | L0 示例（改前 → 改后） | L1 摘要方法 |
|------|----------------|------------------------|-------------|
| `terminal` | `_summarize_terminal` (129) | `terminal: DB: ...T 1 tool ...L1: 13.4` → `terminal: find /home -name "*.py" (4 lines)` | 提取命令 + 去重前 5 关键行。内建 pytest 检测 → `pytest: 8 passed, 2 skipped — 0.44s` |
| `execute_code` | `_summarize_execute_code` (228) | 同 terminal 碎片 | 代理到 terminal handler，替换 tool_name |
| `write_file` | `_summarize_write_file` (235) | `write_file: 无返回数据` → `write_file: /home/i1j/test.txt` | 只保留文件路径，不含内容 |
| `patch` | `_summarize_patch` (266) | `patch: 无返回数据` → `patch: /home/i1j/tool_summarizer.py` | 目标文件 + replace_all 标记 |
| `read_file` | `_summarize_read_file` (302) | `read_file: {"content": "1|import pytest..."` → `read_file: /tmp/test.py` | 文件名 + 行数范围 |
| `search_files` | `_summarize_search_files` (342) | `search_files: {"total_count": 0}` → `search_files: *.py → 0 hits` 或 `66 matches [ca:30, tests:25, docs:11]` | 查询模式 + 命中数 + >3 时 Counter 按父目录分组 |
| `skills_list` | `_summarize_skills_list` (404) | `skills_list: 无返回数据` → `skills_list: 3 skills (tester-workflow, ...)` | 提取技能名列表（前 5） |
| `skill_view` | `_summarize_skill_view` (444) | `skill_view: 无返回数据` → `skill_view: tester-workflow — 12 lines` | 技能名 + 行数 |
| `skill_manage` | `_summarize_skill_manage` (496) | `skill_manage: 失败` → `skill_manage: patch tester-workflow (error)` | 提取 action/name/file_path 关键参数；old_string/new_string 等 bulk content 不进入 result_summary。失败时展示 `key_params | err=...` |
| `memory` | `_summarize_memory` (547) | `memory: {"success": false, ...}` → `memory: replace memory (error)` | 提取 action/target/old_text 关键参数；content 仅取前 80 字符预览 |
| 其他 | （无 handler） | 不变 | 通用字段提取 |

**增加新 handler**：在 `ToolSummarizer` 类中定义 `_summarize_<tool_name>` 方法，返回 `(l1_dict, l0_str)`。工具名中的 `.` 和 `-` 自动映射为 `_`（如 `read-file` → `_summarize_read_file`）。

**零影响**：handler 失败时 `try/except` 回退到通用逻辑。未注册工具行为不变。

**数据契约**：`summarize()` 入口对 `function.arguments` 做 JSON string → dict 归一化（OpenAI API 标准格式）。所有 handler 直接以 parsed dict 接收 `arguments`，无需自行 `json.loads`。通用回退路径（§4.3 通用字段提取）也受益于同一份 parsed args。

**后续成熟**：结构化摘要策略稳定后可推入 `tool_field_priority.yaml` 作为通用默认。

### 4.3.2 通用摘要增强

各 structured handler 之上，两个跨 handler 的增强规则：

**terminal 错误标记**：`_summarize_terminal()` 检测输出是否包含运行时错误，通过 `_ERROR_RE` 正则（Traceback/Error:/Exception/ModuleNotFound/ImportError/NotFound/failed/FAILED 等）扫描所有非空行。匹配时 `result_summary` 和 L0 均以 `[ERROR]` 前缀标记，使 LLM 一眼识别本次调用失败了。

**search_files 目录分组**：`_summarize_search_files()` 对大量命中（total_count > 3）的文件路径用 `Counter` 按父目录名聚合，取前 4 组。替代罗列前 3 个文件名，LLM 直接看到匹配分布。输出示例：`search_files: 66 matches [ca:30, tests:25, docs:11]`。

### 4.4 预选工具轮

`_pre_upgrade_tools` 以对话 L1 的 `core_change` 为 Query，在工具轮 BM25 索引中检索 Top-K（`CA_TOOL_PRE_UPGRADE_COUNT`，默认 3），标记为预选。

### 4.5 LLM 失败标记

当 LLM 返回的文本以 `"核心摘要：无有效增量"` 开头且包含降级特征时，`_assemble_status` 置为 1（待补全）；否则为 0。

### 4.6 LLM 参数注入

参数三级回退：实例属性 → Config 类属性 → 硬编码默认值。`num_predict` 使用 `Config.LLM_NUM_PREDICT`（默认 24768）。

---

## 5. A‑stage 详细设计

### 5.1 同步流程

A‑stage 由 `ContextAssembler.assemble` 实现，包含完整的阶段统计。

1. 从 DB 重建消息历史（`_rebuild_messages_from_cache`），追加当前用户消息。
2. 检查消息列表，若缓存快照为空且 Token 溢出则进行硬截断。
3. 获取不可变快照 `BM25Snapshot` 及数据副本。
4. 计算 `tail_start`（`_compute_tail_start`）。
5. 建立 `idx_to_turn` 映射和 `tool_key_map`（消息索引 → `(turn_index, sub_index)` 复合键）。
6. 计算 `tool_tail_turns`：最后 `TOOL_TAIL_TURN_COUNT`（默认 2）个对话轮的工具原文保护集。
7. 嵌入用户查询（失败时降级为 None）。
8. 计算可用升级预算（`_available_budget`）。若 budget > 0：
   - `Retriever.retrieve` 获取对话轮升级候选。
   - `Retriever.retrieve_tools` 获取工具轮升级候选。
   - `_build_candidates` 计算每个候选的 token 节省量。
   - `_select_upgrades` 贪心拣选。
9. 统一拣选决策（`_compute_turn_plan`），写入 `turn_plan` 表。
10. 按 plan 构建消息（`_build_messages_from_plan`）：每条 entry 的 `target_level` 决定从 turn_cache 按需读取的文本级别（L2→展开 l2_text JSON，L1/L0→单行摘要 `[~/N/0]` / `[~/N/M]` 标记）。
11. 若启用去重，执行 `_deduplicate_messages`（保留最先出现，原位插 `(同[~/N/0])`/`(同[~/N/m])` 标记）。
12. `stats.finalize` 汇总阶段统计。

**阶段计时覆盖**：`get_snapshot` / `layers` / `embed_query` / `retrieval` / `plan` / `build` / `dedup`。

**v4.5.1 变更**：已移除自动头区（`HEAD_AUTO_L1_COUNT`）和旧 v4 回退路径（`CA_PLAN_BUILD_ENABLED`）。plan-based 为唯一路径。

### 5.2 对话轮与工具轮尾区分离（v4.4.1）

核心洞察：对话轮和工具轮是两种不同的资源——对话需要深度上下文保护，旧工具响应对 LLM 价值递减。

| 维度 | 对话轮 | 工具轮 |
|------|--------|--------|
| 尾区保护 | `_compute_tail_start`：反向累计**仅对话消息**（跳过 `role=tool`），10K tokens（`//2` 估算后 ≈ 20K chars） | `TOOL_TAIL_TURN_COUNT=2`：最近 N 个**对话轮**的工具原文保留 |
| 升级策略 | 检索升级（Retriever.retrieve）→ L1 JSON 摘要；未升级→ L0 一行 | 预升级（`_pre_upgrade_tools`）→ L1 JSON 摘要；检索升级→ L1/L0 |
| Middle | → L0 一行（或检索升级→L1） | → L0 一行（与对话 Middle 一致） |

**工具尾区按对话轮判定而非消息索引**：工具是否在尾区取决于其所属对话轮是否在最后 N 轮，与工具组在消息列表中的物理位置无关。

### 5.3 分层计算

对话轮统一走 tail/middle 拣选，已移除自动头区：

- **Tail**：`_compute_tail_start` 从尾部反向累计**仅对话消息**的 token，达到 `PROTECT_TAIL_TOKENS`（10000）时停止。Tail 中的对话轮保留原文（L2）。
- **Middle**：Tail 之前的对话轮。默认降级为 L0 一行摘要；若被检索升级为 L1，则展为 L1 JSON 摘要。
- **工具轮 Head**：C‑stage 预选产生的 `_pre_upgraded_tool_turns` 集合，不在 Middle 中。
- **工具轮 Tail**：最后 `TOOL_TAIL_TURN_COUNT` 个对话轮的工具原文保护集。

### 5.4 检索与拣选

**检索**：`Retriever` 提供两个独立方法：
- `retrieve`：对话轮 BM25 + 向量双路检索，RRF 融合。
- `retrieve_tools`：工具轮 BM25 + 向量双路检索（使用独立的 `tool_bm25` 和 `tool_l1_embeddings`），RRF 融合。

**预算闸门**（`_available_budget`）精确计算：

1. **系统消息 Token**：累加所有 `role: system` 消息。
2. **工具预升级 Token**：C‑stage 预选工具轮中的工具组（`tool_head`），计原文 token。
3. **Tail Token**：对话消息 >= `tail_start` 的 + 工具组所属对话轮在 `tool_tail_turns` 中的。
4. **公式**：`budget = context_length × 0.95 - system_tokens - head_tokens - tail_tokens - system_overhead`。≤0 时跳过检索。
   > 注：`head_tokens` 名称保留但仅用于工具预升级，对话轮无自动头区。

**系统提示词开销**（`system_overhead`）：
1. **默认 20000**：首次运行时预估 Hermes 系统提示词占用 ≈ 20K tokens。
2. **动态覆盖**：首次 `post_llm_call` 从 `conversation_history` 中提取所有 `role: system` 的消息，`len // 4` 估算 token 数，调用 `set_system_overhead()` 更新。
3. **覆盖条件**：仅当测得的实际值 > 0 且与当前值不同时更新，避免冗余写入。
4. **fallback**：若 `conversation_history` 为空或无 system 消息，20000 默认保留，确保 CA 不占用模型的实际系统提示词窗口。

**候选计算**（`_build_candidates`）：
- 对话轮：该轮次下所有非 system、非 tool 消息的 token 总和减去摘要 token。
- 工具轮：完整工具组（`tool_calls` 消息 + 所有 `tool` 响应）的 token 总和减去摘要 token。

**拣选**（`_select_upgrades`）：
1. 类型优先级：对话轮 > 工具轮。
2. 同类型内按 token 节省量降序。
3. 在预算范围内贪心升级。

### 5.5 消息组装（`_build_messages_from_plan` / `_compute_turn_plan` — v4.5.1）

v4.5.0 将 turn_plan 从调试记录升级为**消息组装的直接输入**。v4.5.1 移除旧回退路径，plan-based 为唯一路径。所有拣选决策统一在 `_compute_turn_plan` 中计算，写入 `turn_plan` 表，再由 `_build_messages_from_plan` 按 plan 读取构建。

**决策规则（`_compute_turn_plan`）：**

对话轮：
| 条件 | target_level | decision_reason |
|------|-------------|-----------------|
| head + 有效 L1 | L1 | head |
| head + L1 无效 | L2（回退） | head |
| tail（消息下标 ≥ tail_start） | L2 | tail |
| upgrades + 有效 L1 | L1 | retrieved |
| middle + 有 L0 | L0 | middle |
| 兜底 | L2 | middle |

工具轮：
| 条件 | target_level | decision_reason |
|------|-------------|-----------------|
| tool_head + !upgrades + L1 | L1 | tool_head |
| tool_head + 无 L1 | L2 | tool_head |
| 所属轮在 tool_tail_turns | L2 | tail |
| upgrades + L1 | L1 | retrieved |
| upgrades + L0 | L0 | retrieved |
| middle + L0 | L0 | middle |
| 兜底 | L2 | middle |

**Plan 排序**：先按 `turn_index`，再按 `dialogue`（priority=0）→ `tool`（priority=1），保证对话轮在其附属工具轮之前。

**消息构建（`_build_messages_from_plan`）：**

对于每条 plan entry，从 `store.read_turn_texts()` 按需读取文本：

- `target_level=L2` → 从 `l2_text`（JSON 消息数组）展开全部消息
- `target_level=L1/L0` → 生成单条 `[~/N/0]` 或 `[~/N/M]` 摘要消息（对话轮格式 `[~/N/0]`，工具轮 `[~/N/m]`）
- L1 优先用 l1_text，降级到 l0_text；L0 相反
- plan 未覆盖的消息（当前轮用户消息等）自动追加到末尾

**相比旧 v4 的改进：**
1. **turn 级决策**：旧 v4 按消息粒度处理，导致同一 turn 的 user/assistant/tool 响应走不同分支。plan 统一为 turn 级，同一个 turn 的所有消息共享同一决策。
2. **消除工具尾区泄漏**：旧 v4 中 role=tool 的响应消息通过"普通消息"分支被对话 tail 保护捕获。plan 按 turn 级判断，工具组不会独立泄漏。
3. **决策即存储**：`_compute_turn_plan` 同时产出 plan 列表和写库，不重复遍历。

> 旧路径 `_build_final_messages_v4` + `_compute_and_store_turn_plan` 已在 v4.5.1 中删除。plan-based 为唯一路径。

**硬截断兜底**：系统消息无条件保留。工具组整体保留或移除。从尾部反向填充，超出预算时停止。插入提示消息 `[工具调用结果因上下文截断已被省略]`。

### 5.6 Token 估算（v4.4.1 修正）

对齐 Hermes `context_compressor._CHARS_PER_TOKEN=4`：

```python
def _token_estimate(text):
    cjk = sum(1 for c in text if 0x4e00 <= c <= 0x9fff)
    if cjk > len(text) * 0.5:
        return int(len(text) * 1.5)   # CJK: 保守 1.5×
    return max(1, len(text) // 2)     # ASCII: 2 chars/token（偏保守）
```

### 5.7 上下文窗口查询（v4.4.1）

三级回退：

1. Hermes `agent.model_metadata.get_model_context_length()` — 运行时获取，覆盖所有 provider。
2. 自有 `_MODEL_CONTEXT_WINDOW` 查表 — 离线/单元测试时使用。
3. `CONTEXT_LENGTH`（默认 50K）— 兜底。

压缩预算 = `model_window × COMPRESSION_THRESHOLD`（默认 0.50，对齐 Hermes `compression.threshold`）。
deepseek-v4-flash: 1M × 0.50 = **500K tokens**。

### 5.8 插件层后处理（上下文注入优化）

`pre_llm_call()` 收到 `assemble()` 返回的 CA 摘要列表后，注入 Hermes user message 前执行两项跨回合优化，均在插件层（`plugins/__init__.py`），不涉及核心引擎：

**连续同工具空结果合并 + ×n 计数**：遍历摘要列表，对相邻条目剥离 `[~/N/M]` 前缀标签后比较正文。相同则跳过后续条目并在保留条目末尾追加 `×{count}`。例如两条连续 `search_files: 0 hits` 合并为 `search_files: 0 hits ×2`。既避免重复行膨胀，又保留重复次数供 LLM 判断工具调用健康度。

设计决策：消费端修复而非生产端。理由：① 零风险——不动核心引擎、DB 或缓存；② 注入编号（`[~/N/M]`）仅在当前注入上下文偏移，DB 索引不受影响；③ ×n 标记补偿了信息丢失。

---

## 6. 全指纹去重设计

- **标签剥离**：指纹计算前用 `_CA_TAG_RE` 正则剥离 `[~/N/0]` / `[~/N/M]` 前缀标签，使跨轮相同摘要（同名工具同结果、同 core_change 对话轮等）可命中同一指纹。
- **最先出现保留**：两遍扫描算法——第一遍建指纹→最先出现索引，第二遍保留最先出现的那条，后续重复在原位替换为指向标记 `(同[~/N/0])`/`(同[~/N/m])`。首次出现位置永远不动 → 缓存前缀稳定。
- **深度规范化**：递归排序 dict 键、列表保持顺序、解析字符串化 JSON、`None` → `""`、深度限制 10。
- **包含 role 字段**：避免跨角色相同内容误杀。
- **系统消息豁免**：无条件保留，不参与 fingerprint→index 映射。
- **去重逻辑**：在 `assemble` 末尾执行，处理 `_build_messages_from_plan` 输出的消息列表。由 `_deduplicate_messages`（两遍扫描）+ `_msg_fingerprint`（标签剥离+归一化+SHA256）组成。
- **Fail‑Safe**：`CA_DEDUP_ENABLED` 非法值时**强制启用**去重（故障导向安全），记录 WARNING。

---

## 7. L‑stage 异步补全设计

### 7.1 线程模型

每个 `ContextAssembler` 实例创建两个守护线程：

- `DialogueBackfillThread`：补全对话轮缺失摘要。
- `ToolBackfillThread`：补全工具轮缺失摘要。

### 7.2 补全流程

1. 查询 `_assemble_status = 1` 的记录。
2. 使用 `l2_text` 作为输入（不依赖外部消息快照）。
3. 对话轮：调用 `_call_llm_for_l1` 重新生成。
4. 工具轮：调用规则引擎重新生成。
5. 成功：`_assemble_status` → 0，写入 DB + 缓存。
6. 失败：递增 `backfill_attempts`；达到 3 次 → `_assemble_status = 2`，永久跳过。

### 7.3 触发与关闭

- **触发**：C‑stage 结束时无条件唤醒。线程自行查询，无待处理记录则立即休眠。
- **定时自检**：每 60 秒自动扫描。
- **关闭**：先 `wait_for_pending` 等待 C‑stage 任务，再通知 L‑stage 停止并 `join`。

---

## 8. 存储层与缓存设计

### 8.1 SQLiteStore

- WAL 模式（启动时校验）、NORMAL 同步、busy_timeout。
- 后台 checkpoint 守护线程（轮询）。
- 线程本地连接，空闲超时自动关闭。
- 写入重试（指数退避），批量写入单事务。
- 嵌入 BLOB 安全解包（损坏返回 None）。
- 内存缓存 session_id 列表（24h TTL）。
- `turn_plan` 表：`write_turn_plan` / `read_turn_plan` / `upsert_turn_plan_topic`。

### 8.2 AssemblyCache

- 维护对话轮和工具轮的 L0/L1 文本与嵌入字典。
- `add_turn` / `add_tool_turn` 后提交异步重建任务；连续失败时启用**冷却期**（5s）。
- `rebuild_bm25_snapshot()` 构建不可变 `BM25Snapshot`：
  - 对话轮：`bm25`、`turn_indices`、`l1_embeddings`。
  - 工具轮：`tool_bm25`、`tool_turn_keys`、`tool_l1_embeddings`。
  - 原子引用替换发布。
- `get_snapshot_data()` / `get_tool_snapshot_data()` 返回线程安全副本。

### 8.3 CacheBuilder

从 SQLite 加载历史数据填充 `AssemblyCache`，失败时重置内部字典并构建空快照，确保返回可用缓存。

---

## 9. 嵌入服务设计

- 多后端支持：Ollama / sentence-transformers / fallback。
- 线程安全 LRU 缓存（手动实现），**仅缓存真实后端结果**，fallback 伪向量不缓存。
- 并行编码（`ThreadPoolExecutor`），可配置超时。
- Ollama 连接池使用 `urllib3.PoolManager`；网络异常时自动 `pool.clear()` 再重试。

---

## 10. 配置管理设计

### 10.1 核心配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
|| `CA_CONTEXT_LENGTH` | 50000 | 上下文总 Token 窗口上限 |
| `CA_PROTECT_TAIL_TOKENS` | 10000 | 对话尾区 token 预算（`//2` 后 ≈ 20K chars） |
| `CA_TOOL_TAIL_TURN_COUNT` | 2 | 工具尾区保护最近 N 个对话轮 |
| `CA_COMPRESSION_THRESHOLD` | 0.50 | 压缩警戒比值，乘 model_window 得预算上限 |
| `CA_TOOL_PRE_UPGRADE_COUNT` | 3 | C‑stage 预选工具轮数量 |
| `CA_TOOL_MAX_UPGRADE_K` | 3 | A‑stage 最大升级工具轮数 |
| `CA_TOOL_PRE_UPGRADE_WINDOW` | 50 | 预选检索窗口（最近 N 轮） |
| `CA_TOPIC_BOUNDARY_DISTANCE` | 0.50 | 话题边界余弦距离阈值 |
| `CA_DEDUP_ENABLED` | True | 全指纹去重开关 |
| `CA_LLM_NUM_PREDICT` | 24768 | LLM 生成 token 上限 |
| `CA_BACKFILL_DIALOGUE_RATE` | 2 | 对话轮补全速率 |
| `CA_BACKFILL_TOOL_RATE` | 5 | 工具轮补全速率 |
| `CA_SHUTDOWN_TIMEOUT` | 5 | 资源清理等待超时 |

### 10.2 环境变量

| 变量 | 说明 |
|------|------|
| `CA_EMBED_BACKEND` | 嵌入后端（ollama / sentence-transformers） |
| `CA_EMBED_MODEL` | 嵌入模型名 |
| `CA_EMBED_ENDPOINT` | 嵌入服务端点 |
| `CA_LLM_MODEL` | L1 摘要生成模型 |
| `CA_LLM_ENDPOINT` | LLM 服务端点 |
| `CA_DEBUG` | 调试模式开关 |

### 10.3 热重载

支持 `Config.reload()` 热重载 + `validate()` 校验（上下限检查），reload 失败时保留旧值并记录严重错误。

---

## 11. 健康检查与监控

- `HealthCheck.check_store`：连通性、WAL 大小、DB 文件大小。
- `HealthCheck.check_embedding_client`：缓存命中率、失败次数、降级使用量。
- `get_prometheus_metrics`：导出指标，标签值转义。
- `AssembleStats`：A‑stage 阶段性能计时（含工具轮相关统计字段）。

---

## 12. 错误处理与降级

| 组件            | 错误                | 处理                              |
| --------------- | ------------------- | --------------------------------- |
| SQLiteStore     | 锁竞争              | 指数退避重试                      |
| EmbeddingClient | 超时 / 网络错误     | 清除连接池重试，最终降级 fallback |
| A‑stage        | 嵌入失败            | 记录错误，降级为纯 BM25 检索      |
| C‑stage        | LLM 超时 / 解析失败 | 降级写入"无有效增量"（status=1） |
| 工具 JSON 解析  | 解析失败            | 降级摘要 `result_summary="无法解析"`，不重试 |
| L‑stage        | 补全失败 ≥3 次     | `_assemble_status=2`，WARNING，永久跳过 |
| AssemblyCache   | 重建异常            | 进入冷却期，延迟重试              |
| 快照构建        | 失败                | 重试一次；仍失败则降级为直接返回原始消息 |
| 嵌入维度不一致  | 不匹配              | 过滤不匹配条目，全部不匹配则降级纯 BM25 |
| Plugin          | 连续初始化失败 3 次 | 断路器短路 1 小时                 |
| 会话销毁超时    | 超时                | `SHUTDOWN_TIMEOUT` 后强制继续     |

---

## 13. 线程安全设计

| 组件                        | 机制                    | 说明                                                                              |
| --------------------------- | ----------------------- | --------------------------------------------------------------------------------- |
| `SessionManager`          | `threading.Condition` | 等待会话销毁完成，避免异步销毁期间的竞态                                          |
| `SessionManager` 超时保护 | 强制移除标记            | 等待销毁超过 `SHUTDOWN_TIMEOUT` 时，强制移除 `_destroying` 标记                  |
| `AssemblyCache`           | `RLock` 保护快照读写  | `rebuild_bm25_snapshot` 与 `get_bm25_snapshot` 互斥                           |
| `AssemblyCache` 数据复制  | 锁内复制、锁外重建      | `add_turn` 仅短暂加锁，快照构建在锁外进行                                       |
| L‑stage 线程               | 写入锁 + 快照机制隔离   | 补全线程通过 `store.write_turn` 加锁；A‑stage 读取快照副本                       |
| `SQLiteStore`             | WAL 模式 + 写入重试     | 多线程并发读写安全，线程本地连接                                                  |
| `EmbeddingClient`         | `OrderedDict` + `RLock` | 线程安全 LRU                                                                      |
| `SessionManager`          | `RLock` + 锁外异步销毁  | 清理循环主动容量驱逐                                                              |
| 话题 ID                   | `_topic_lock`          | 原子递增 `_current_topic_id`                                                    |

---

## 14. 断路器设计

- Plugin 层维护状态文件 `{hermes_home}/.ca_assembler_state_{PID}.json`。
- 连续失败 3 次后设置 `retry_after`（1 小时后），`is_available()` 返回 `False`。
- `on_session_start` 首先检查断路器，若不可用则直接短路。
- 成功初始化或 reset 时调用 `_record_success()` 清除状态。

---

## 15. 话题边界检测（v4.4.1）

C-stage 末尾执行：

1. 当前轮 L1 嵌入向量已生成。
2. 从 DB 读取上一对话轮的 L1 嵌入向量（避免异步线程竞态）。
3. 计算余弦相似度。
4. 低于 `TOPIC_BOUNDARY_DISTANCE`（默认 0.50）→ 新话题，递增 `_current_topic_id`。
5. 通过 `store.upsert_turn_plan_topic()` 持久化到 `turn_plan.topic_group`。

目的：为未来 A-stage 按话题归并邻接轮次提供数据基础。

---

## 16. turn_plan 拣选决策记录（v4.4.1 → v4.5.0 升级为组装驱动源 → v4.5.1 唯一路径）

v4.4.1 引入 turn_plan 表记录拣选决策，用途仅为调试比对。v4.5.0 将 turn_plan 升级为**A-stage 消息组装的直接输入**，v4.5.1 移除旧 v4 回退路径，plan-based 为唯一路径：

**v4.4.1 及之前（旧流程，已删除）**：
```
_compute_layers_v2 → _build_final_messages_v4(组装) → _compute_and_store_turn_plan(写库)
                                                                    ↓
                                                              仅调试使用
```

**v4.5.0+ 新流程**：
```
_compute_turn_plan(统一决策) → write_turn_plan(持久化)
                                     ↓
                         _build_messages_from_plan(按plan构建)
```

`TurnPlanEntry` 结构维持不变：

| 字段 | 说明 |
|------|------|
| `turn_index` | 对话轮次 |
| `turn_type` | `dialogue` 或 `tool` |
| `tool_sub_index` | 工具组序号（对话轮恒为 0） |
| `target_level` | L2（原文）/ L1（摘要）/ L0（一行） |
| `decision_reason` | `tail` / `retrieved` / `tool_head` / `middle` |
| `tokens_saved` | 相比保留 L2 节省的 token 数 |
| `topic_group` | 话题分组 ID |

plan 按 `(turn_index, type_priority, tool_sub_index)` 排序，保证对话轮在前、其工具轮在后。`_build_messages_from_plan` 遍历 plan，对每条 entry 调 `store.read_turn_texts()` 按 target_level 读取对应文本，plan 未覆盖的消息（当前轮用户输入等）自动追加。

---

## 17. 关键设计决策 (ADR)

| 决策                                     | 理由                                                                                                                                                                       |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C‑stage 异步化                          | 避免 LLM 调用阻塞用户响应                                                                                                                                                  |
| `turn_index = max(counter+1, user_count)` | 消除引擎重启场景下的主键覆盖                                                                                                                                               |
| 全指纹去重 + 深度规范化                  | 解决嵌套 JSON 键序陷阱，杜绝跨角色误杀                                                                                                                                     |
| fallback 嵌入不缓存                       | 防止后端恢复后仍使用伪向量，确保检索质量                                                                                                                                   |
| AssemblyCache 冷却重试                    | 避免连续重建失败耗尽线程池，实现弹性容错                                                                                                                                   |
| SessionManager 异步销毁 + 主动容量驱逐    | 消除死锁风险，严格控制内存上限                                                                                                                                             |
| WAL 模式主动校验                          | 防止只读文件系统导致回退到 DELETE 模式，保障并发性能                                                                                                                       |
| Embedding 连接池过期自动清除              | 解决长时间空闲后 Nginx/LB 断开导致的首次请求失败                                                                                                                           |
| A‑stage 集成 AssembleStats               | 提供阶段级性能计时，满足可观测性要求                                                                                                                                       |
| **对话轮与工具轮尾区分离**               | 对话需要深度上下文保护，旧工具响应对 LLM 价值递减；独立尾区策略避免工具响应挤占对话 tail 预算                                                                              |
| **Token 估算对齐 Hermes**                | `len // 2` 对齐 Hermes `_CHARS_PER_TOKEN=4`，CJK 保守 1.5×，消除 4× 高估                                                                                                |
| **上下文窗口三级回退**                    | Hermes API → 自有查表 → 200K 兜底，覆盖 Ollama 自定义模型和 deepseek-v4 1M 窗口                                                                                            |
| **后台审查轮规则跳过 LLM**               | `write_origin == "background_review"` 时规则生成 L1："系统后台审查"，避免浪费 LLM 调用                                                                                     |
| 工具组粒度                                | 一个 `tool_calls` 消息及其后续所有 `tool` 响应视为一个工具轮，C/A 阶段索引逻辑完全一致                                                                                     |
| 子索引从 1 开始                           | 对话轮 `tool_sub_index=0`，工具轮从 1 递增，清晰区分                                                                                                                       |
| 兜底预选工具轮 L1                          | 即使 A‑stage 预算极度紧张，预选工具轮保证至少以 L1 形式保留                                                                                                                |
| 工具组匹配使用索引而非对象引用            | 消息列表可能被重组，对象引用不可靠；使用 `start_index` 确保匹配鲁棒性                                                                                                      |
| L‑stage 无条件触发                       | 线程自行判断是否有待处理记录，避免遗漏，无额外开销                                                                                                                         |
| 去重 Fail‑Safe 策略                      | 非法配置值时强制启用去重，遵循"故障导向安全"，宁可误杀重复，不可撑爆窗口                                                                                                   |
| **话题边界检测**                          | 余弦相似度 < 0.50 → 新话题，为未来按话题归并邻接轮次提供数据基础                                                                                                           |
| **turn_plan 表**                         | 记录每次拣选决策，当前用于调试，下一步支持 turn 级拣选组装                                                                                                                  |
| **turn_plan 驱动组装**                   | v4.5.0：turn_plan 从调试记录升级为 A-stage 消息组装的直接输入。`_compute_turn_plan` 统一决策，`_build_messages_from_plan` 按 plan 读取文本                                                                                                                     |
| **原位去重标记**                          | `(同[~/N])` 标记替换静默删除——LLM 在时间线上看到"这事又在 N 轮发生了"，而非被删项凭空消失。标注在删位而非幸存者上，幸存者内容纯净                                                                                          |

---

## 附录 A：正向追溯表（需求 → 设计/实现）

| 需求 ID             | 需求描述                           | 设计章节    | 实现模块/方法                                       |
| ------------------- | ---------------------------------- | ----------- | --------------------------------------------------- |
| REQ-FUNC-CSTAGE-001 | 异步非阻塞执行                     | 4.1         | `process_turn_async`                              |
| REQ-FUNC-CSTAGE-002 | 增量 L1 摘要生成                   | 4.1, 4.5    | `_run_c_stage` → `_call_llm_for_l1`            |
| REQ-C-ERROR         | C‑stage 降级标识                  | 4.5         | `_run_c_stage` 中设置 `_assemble_status`        |
| REQ-FUNC-CSTAGE-003 | L0 原文提取                        | 4.1         | `_extract_l0`                                     |
| REQ-FUNC-CSTAGE-004 | 按需计算优化                       | 5.4         | 检索仅在 A‑stage 执行                              |
| REQ-FUNC-CSTAGE-005 | 防覆盖索引计算                     | 4.1         | `process_turn_async` 内 `max(...)`              |
| REQ-TOOL-C001       | 识别工具调用组                     | 4.2         | `_extract_tool_groups`                            |
| REQ-TOOL-C002       | 规则生成 L1 模板                   | 4.3         | `ToolSummarizer.summarize/summarize_group`        |
| REQ-TOOL-C003       | L1 错误处理                        | 4.3         | 错误字段处理逻辑                                    |
| REQ-TOOL-C004       | L0 生成规则                        | 4.3         | L0 生成及 thinking 追加                             |
| REQ-TOOL-C005       | 字段分级体系                       | 4.3         | 优先级配置与提取                                    |
| REQ-TOOL-C006       | 存储与索引                         | 3.1, 3.3    | `store.write_turn`, `AssemblyCache` 工具轮字典  |
| REQ-TOOL-C007       | 无条件摘要生成                     | 4.1         | C‑stage 必定生成                                   |
| REQ-TOOL-C008       | 规则引擎容错                       | 12          | 降级摘要生成                                        |
| REQ-FUNC-ASTAGE-001 | Head/Middle/Tail 分层              | 5.2, 5.3    | `_compute_tail_start`（自动头区已移除）              |
| REQ-FUNC-ASTAGE-002 | 双路检索与 RRF 融合                | 5.4         | `Retriever.retrieve/retrieve_tools`               |
| REQ-FUNC-ASTAGE-003 | 动态预算闸门（含系统、Head、Tail） | 5.4         | `_available_budget`                               |
| REQ-FUNC-ASTAGE-004 | 预算耗尽短路                       | 5.4         | `budget > 0` 检查                                 |
| REQ-FUNC-ASTAGE-005 | 无效摘要过滤                       | 5.5         | `_is_valid_summary`                               |
| REQ-FUNC-ASTAGE-006 | 统一 Token 估算                    | 5.6         | `_token_estimate`                                 |
| REQ-FUNC-ASTAGE-007 | 硬截断兜底                         | 5.5         | `_hard_truncation`                                |
| REQ-FUNC-ASTAGE-008 | 工具组完整性保护                   | 5.5         | `_hard_truncation` 分组逻辑                       |
| REQ-FUNC-ASTAGE-009 | 截断提示消息                       | 5.5         | `_hard_truncation` 插入提示                       |
| REQ-TOOL-P001       | C‑stage 预选                      | 4.4         | `_pre_upgrade_tools`                              |
| REQ-TOOL-P002       | A‑stage Tail 保护（工具轮）       | 5.2, 5.5    | `_build_messages_from_plan` 中 tool_tail_turns 判断 |
| REQ-TOOL-P003       | 兜底保障                           | 5.5         | 预选工具轮强制 L1                                   |
| REQ-TOOL-P004       | 确定性降级                         | 5.4         | `_select_upgrades` 排序                           |
| REQ-TOOL-P005       | 同类型内确定性排序                 | 5.4         | 按 RRF 得分排序                                     |
| REQ-TOOL-P006       | 升级上限                           | 5.4         | `CA_TOOL_MAX_UPGRADE_K` 控制                      |
| REQ-FUNC-DEDUP-001  | 系统消息豁免                       | 6           | `_deduplicate_messages`                           |
| REQ-FUNC-DEDUP-002  | 深度规范化指纹                     | 6           | `_deep_normalize`                                 |
| REQ-FUNC-DEDUP-003  | 跨角色防误杀                       | 6           | 指纹含 role                                         |
| REQ-FUNC-DEDUP-004  | 时序保持                           | 6           | 顺序遍历                                            |
| REQ-FUNC-DEDUP-005  | Fail‑Safe 降级（非法值启用）      | 6, 10       | `_parse_bool_env` 强制启用策略                    |
| REQ-FUNC-DEDUP-006  | 调试日志                           | 6           | DEBUG 输出                                          |
| REQ-L-STAGE-001~007 | L‑stage 补全各项需求              | 7.1~7.3     | `BackfillThread` 及触发逻辑                       |
| REQ-TOOL-CFG01~10   | 配置项                             | 10          | `config.py`                                       |
| REQ-PERF-001~007    | 性能需求                           | 5.1, 5.4 等 | 设计保证，需基准测试                                |
| REQ-REL-001~005     | 可靠性需求                         | 12, 13, 14  | 相应模块实现                                        |
| REQ-OBS-001~002     | 可观测性需求                       | 11, 16      | `AssembleStats`, `health.py`, `turn_plan`        |

---

## 附录 B：反向追溯表（实现模块 → 需求）

| 模块                                  | 主要职责                             | 满足的需求 ID                                                                                                           |
| ------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `ca/__init__.py` (ContextAssembler) | C/A/L 三阶段控制、拣选组装、预算计算 | REQ-FUNC-CSTAGE-001~005, A‑stage-001~009, DEDUP-001~006, TOOL-C001, TOOL-P001~006, L-STAGE-001~007, OBS-002, C-ERROR |
| `ca/config.py`                      | 集中配置与校验                       | REQ-FUNC-DEDUP-005, TOOL-CFG01~10                                                                                       |
| `ca/store.py`                       | 持久化存储（含 turn_plan）          | REQ-REL-003, TOOL-C006, L-STAGE-003                                                                                     |
| `ca/cache.py`                       | 内存缓存与 BM25 快照（含工具轮索引） | REQ-REL-001, TOOL-C006, 检索性能                                                                                        |
| `ca/retrieval.py`                   | 双路检索（对话+工具）+ RRF 融合      | REQ-FUNC-ASTAGE-002, TOOL-P005                                                                                          |
| `ca/embedding.py`                   | 嵌入服务与 LRU 缓存                  | REQ-PERF-003                                                                                                            |
| `ca/ooda_parser.py`                 | OODA 解析与语义去重                  | REQ-FUNC-CSTAGE-002 (辅助)                                                                                              |
| `ca/post_process.py`                | JSON 容错与清洗                      | REQ-FUNC-CSTAGE-002 (辅助)                                                                                              |
| `ca/prompts.py`                     | L1 生成提示词                        | REQ-FUNC-CSTAGE-002                                                                                                     |
| `ca/stats.py`                       | 阶段统计（`AssembleStats`）        | REQ-OBS-002                                                                                                             |
| `ca/health.py`                      | 健康检查与 Prometheus                | REQ-OBS-001                                                                                                             |
| `ca/tool_summarizer.py`             | 工具轮摘要规则引擎                   | REQ-TOOL-C002~C005, C008                                                                                                |
| `ca/tool_field_priority.yaml`       | 字段优先级配置                       | REQ-TOOL-C005                                                                                                           |
| `ca/lstage.py`                      | 异步补全线程                         | REQ-L-STAGE-001~006                                                                                                     |
| `plugins/ca_assembler/__init__.py`  | 插件适配与断路器                     | REQ-REL-004                                                                                                             |

---



