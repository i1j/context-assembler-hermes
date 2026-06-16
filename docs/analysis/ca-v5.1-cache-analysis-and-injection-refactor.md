# CA v5.1 — 缓存分析与注入重构报告

**日期**: 2026-06-13
**范围**: cache 现状分析 + hdl_embedding 孤儿清除 + tool_plan 注入重构 + bg_review 填充

---

## 1. Cache 架构总览

### 1.1 双层结构

| 层 | 实现 | 职责 |
|---|------|------|
| **内存字典** | `AssemblyCache` (ca/cache.py) | 6 个 `Dict` 存 L0/L1 文本 + 4 个 `Dict` 存嵌入向量 |
| **检索索引** | `BM25Snapshot` / `BM25Okapi` | 不可变快照，原子替换，对话轮 + 工具轮双索引 |

### 1.2 三个存储分组

| 组 | Key 类型 | 字段 | 来源 |
|---|---------|------|------|
| 对话轮 | `int` turn_index | `Hdls`, `Fcts` + 嵌入 | `_run_c_stage()` → `add_turn()` |
| 个体工具 | `(int,int)` (turn,seq) | `tool_Hdls`, `tool_Fcts` + 嵌入 | `CacheBuilder.build()`（从 DB 重建） |
| 工具组 | `(int,int)` (turn,api_call_count) | `tool_group_Hdls`, `tool_group_Fcts` | `flush_tool_buffer()` → `add_tool_group()` |

### 1.3 数据流（graphify 动态边验证）

```
写入路径:
  _run_c_stage() ──add_turn()──→ l0_texts+l1_texts ──_submit_rebuild()──→ BM25Snapshot
  flush_tool_buffer() ──add_tool_group()──→ tool_group_l0/l1 ──_submit_rebuild()──→ BM25Snapshot

读取路径:
  _compute_assemble_plan() ──ensure_snapshot()/get_bm25_snapshot()──→ BM25Snapshot → 检索
  _build_aligned_outcomes() ──直接读──→ cache.l1_texts[turn] / cache.l0_texts / tool_group_l1
  _format_tool_group_assembly() ──直接读──→ cache.tool_l1_texts / tool_l0_texts

重建路径:
  CacheBuilder.build() ──store.read_session()──→ 6个dict全部填充 ──rebuild_bm25_snapshot()──→ BM25Snapshot
```

### 1.4 运行时状态（session 20260613_105128_554cc1）

| 指标 | 值 | 解读 |
|------|-----|------|
| turn_cache 行数 | 332 | 23 轮对话 |
| 角色分布 | user=23, assistant=143, tool=166 | 每轮平均 6.2 assistant + 7.2 tool 消息 |
| 有嵌入行 | 23/332 (7%) | **只有 user 行有嵌入** |
| `_assemble_status=0` | 332/332 (100%) | 从未触发降级 |

---

## 2. hdl_embedding 孤儿数据清理

### 2.1 发现

`hdl_embedding` 只来源于对话轮（user 行）的 C-stage LLM 调用，但：

| 消费端 | 结果 |
|--------|------|
| `rebuild_bm25_snapshot()` | ❌ 不包含 hdl_embeddings |
| `Retriever.retrieve()` | ❌ 只用 fct_embeddings |
| `TopicRetriever.retrieve()` | ❌ 只用 topic 形心 |
| `_compute_topic_groups()` | ❌ 只用 fct_embeddings |
| `_build_aligned_outcomes()` | ❌ 只读 Hdls（文本） |
| `retrieve_l0_upgrade()` | **死代码，无人调用** |

每次 C-stage 调用 `embed_client.embed(Hdl)` 的 ~200-500ms 完全是浪费。

### 2.2 处理

| 文件 | 改动 |
|------|------|
| `ca/__init__.py` _run_c_stage | 注释掉 `l0_emb = self.embed_client.embed(Hdl)` |
| `ca/lstage.py` 对话轮 backfill | 注释掉 `l0_emb = self.engine.embed_client.embed(l0)` |
| `ca/lstage.py` 工具轮 backfill | 注释掉 `l0_emb = self.engine.embed_client.embed(tool_l0)` |
| `ca/retrieval.py` | 删除死代码 `retrieve_l0_upgrade()` |

**DB schema 列保留** — 已有数据不破坏，新写入全为 NULL。

---

## 3. tool_plan 注入重构（v5.2）

### 3.1 背景

v5.1 行为：tool 行全部被删除（`""`），工具组详情合并到 `assistant{tc}` 的 `_format_tool_group_assembly` 输出中。但历史记录表不能删行——`""` 最终被 `_mutation_mode` 设为 `" "` 占位，浪费语义。

### 3.2 改动

#### 数据类 `_AssemblePlanResult`

```python
@dataclass
class _AssemblePlanResult:
    plan: List[TurnPlanEntry]
    messages: List[Dict]
    stats: Any
    tokens_before: int
    bypass_turns: Set[int] = field(default_factory=set)
    tool_plan: List[TurnPlanEntry] = field(default_factory=list)  # v5.2 新增
```

#### 新方法 `_compute_tool_plan_v2()`

对 `conversation_history` 中每条 `role="tool"` 的消息，按父对话轮级别决定：

| 父对话轮级别 | tool 行级别 | 行为 |
|-------------|-----------|------|
| L2 | L1 | 生成 L1 摘要文本 |
| L1 | L0 | 生成 L0 摘要文本 |
| L0 | skip | 不移除（`""` → `" "` 占位，由 _mutation_mode 处理） |

tool_plan **不持久化**到 `turn_plan` 表（运行时派生即可）。

#### `_build_aligned_outcomes` 签名扩展

```python
def _build_aligned_outcomes(self, plan, conversation_history, bypass_turns=None, tool_plan=None):
```

tool 行处理逻辑：
- `_tool_by_key[(turn, seq)]` 命中 → 输出 `[~/N/M] 摘要文本`（L1 或 L0 fallback）
- 未命中 → `""`（由 mutation mode 清空占位）
- 无摘要但有 tool_plan 条目 → `None`（保留原文）

#### `_format_tool_group_assembly` 精简

之前：输出完整工具组文本（header + 各工具详情 + ×N 合并）
现在：**仅输出 header**

```
之前：【工具组:查文件→aaa(3个,ok)】
        search: *.py → 3 hits
        read_file: /tmp/test.py (60 lines) ×2

现在：【工具组:查文件→aaa(3个,ok)】
```

各工具行的详情由独立 `tool_plan` 条目的 `[~/N/M]` 标签携带。

### 3.3 inject 后的文本结构（replace 模式）

```
user: "查了文件系统 — 文件A"
assistant{tc}: "【工具组:查文件→aaa(3个,ok)】"     ← 精简 header
tool: "[~/3/1] read_file: /tmp/test.py (60 lines)"  ← 独立标签（L1）
tool: "[~/3/2] search: *.py → 3 hits"               ← 独立标签（L1）
assistant_fin: "文件内容已查到"
```

---

## 4. bg_review 轮填充逻辑

### 4.1 背景

bg_review 轮被从 plan 中完全过滤（不走话题分级/预算/保护区），在 `_mutation_mode` 中原被清为 `" "`。其工具行因不在 `tool_plan` 中也被清为 `" "`。但 bg_review 的工具操作（skill_view、skill_manage 等）对 LLM 理解主线程对话无参考价值，纯占位。

### 4.2 填充规则

数据来源：**DB `read_session()`**（非 cache），按 turn_index 批量读取后逐行匹配。

| 行类型 | 优先 | 降级 | 兜底 |
|-------|------|------|------|
| user | `_format_l1_for_display(l1)` | `Hdl` | `" "` |
| assistant{tc} | `_format_tool_group_assembly(l1)` → header | `Hdl` | `" "` |
| tool | **`Hdl`（无 `[~/N/M]` 标签）** | — | `" "` |
| final assistant | — | — | `" "` |

### 4.3 匹配保护

tool 行按 `(api_call_count, seq_index, role)` 三字段匹配 DB 记录。任何字段对不上 → 清空为 `" "`。

### 4.4 效果示例

```
之前（v5.1）：
  user: " "
  assistant{tc}: " "
  tool: " "
  assistant{tc}: " "
  tool: " "

之后（v5.2）：
  user: "Review the conversation above and update the skill library..."
  assistant{tc}: "【工具组:检查 skill→ca-development(1个,ok)】"
  tool: "skill_view: ca-development"
  assistant{tc}: "【工具组:执行指令→调用 1 个工具(1个)】"
  tool: "(1 lines)"
```

---

## 5. 未解决/已知问题

### 5.1 嵌入覆盖率极低

只有 user 行（23/332 = 7%）有 L0/L1 嵌入。工具行和 assistant{tc} 行虽然写了 Fct，但从不写嵌入。`BM25Snapshot` 不包含 `tool_group_Fcts`，TopicRetriever 对工具组内容不可见。

### 5.2 旧 DB 从未清理

263 个 session DB 文件，共 611MB。最旧的可追溯到 5月25日。`Config.CA_CACHE_MAX_SESSIONS` 默认 50 只管理内存引擎实例，不管理磁盘文件。磁盘上无 GC 机制。

### 5.3 WAL 未 checkpoint

最新 session 的 WAL 文件（4.2MB）> DB 文件（4.7MB）。从未显式 checkpoint。

### 5.4 降级路径零触发

332 行全为 `_assemble_status=0`，降级/熔断路径未经真实场景验证。
