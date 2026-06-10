# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项       | 值                                                                        |
| -------- | ------------------------------------------------------------------------- |
| 版本     | v5.1 |
| 注入方式 | 1:1 对齐替换/跳过（无 `[~/N/M]` 标签），v5.1 bypass_turns 统一两路 |
| plugin.yaml | v5.1（已同步）                    |
| 部署方式 | 自包含独立副本                                                            |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/`                       |
| 核心引擎 | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`）         |
| 插件适配 | `plugins/ca_assembler/__init__.py`（入口 `CAContextAssemblerPlugin`） |
| 启用方式 | `plugins.enabled: [ca_assembler, ...]`                                  |
| 源项目   | `~/projects/context-assembler/`（已分化，含独有修复）                   |

## 注册接口

`register(ctx)` 注册 8 个 Hermes hooks（5 生命周期 + 3 工具轮数据采集）：

```python
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call)
    ctx.register_hook("post_llm_call",    _on_post_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("pre_tool_call",    _on_pre_tool_call)
    ctx.register_hook("post_tool_call",   _on_post_tool_call)
```

## Hook 分发函数

### `_on_session_start` — 引擎初始化

```python
def _on_session_start(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（必含）

行为：

1. 创建 `CAContextAssemblerPlugin` 实例
2. 检查断路器 `is_available()` → 3 次失败则 1 小时冷却
3. 从 `get_hermes_home()` 获取 profile 基路径
4. DB 路径：`{hermes_home}/ca_cache/{session_id}.db`
5. 通过 `session_manager.get(session_id, db_path)` 创建引擎
6. 注册到模块级 `_engines` 字典（`session_id → plugin`）
7. 成功后日志：`"CA plugin started for session {session_id}"`

### `_on_session_end` — 资源清理

```python
def _on_session_end(**kwargs: Any) -> None:
```

**kwargs**: `session_id`

行为：从 `_engines` 移除实例 → `engine.wait_for_pending()` + `session_manager.remove()`

### `_on_session_reset` — 重置

```python
def _on_session_reset(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（可选，无 session_id 时重置全部）

行为：从 `_engines` 移除 → `engine.reset()` + 清除断路器状态

### `_on_pre_llm_call` — 上下文注入（A-stage）

```python
def _on_pre_llm_call(**kwargs: Any) -> Optional[str]:
```

**kwargs**:

| 参数               | 类型 | 说明                                                    |
| ------------------ | ---- | ------------------------------------------------------- |
| `session_id`     | str  | 当前会话                                                |
| `user_message`   | str  | 用户输入文本                                            |
| `context_length` | int  | 上下文 Token 窗口上限（默认 `Config.CONTEXT_LENGTH`） |

**返回值**: `Optional[str]`

- 引擎不可用或出错 → 返回 `None`（不注入）
- 调用 `engine._compute_assemble_plan(user_message, context_length)` 获取 plan
- 注入模式由 `Config.HISTORY_INJECTION` 控制（环境变量 `CA_HISTORY_INJECTION`）：
  - **replace 模式（默认）**：调用 `_build_aligned_outcomes(plan, history)` 产生 1:1 对齐结果
    → 原地替换/移除行，保存快照供 post_llm_call 恢复
  - **append 模式**：调用 `_build_messages_from_plan` 拼接标签文本返回
  - **off 模式**：返回 `None`，不注入（仅做数据积累）
- **不再使用 `[~/N/M]` 标签**（mutation 模式）— 摘要直接以可读文本输出

#### 三路注入路径

| 维度 | Replace 模式（默认） | Append 模式 | Off 模式 |
|------|---------------------|-------------|----------|
| 入口 | `_build_aligned_outcomes()` | `_build_messages_from_plan()` | — |
| 输出与 history | 替换/移除 history 条目 | 摘要文本拼入 user message | 不动 history |
| 标签 | 无标签 — 纯文本摘要 | 含 `[~/N/0]` 标签（annotation 旧格式） | — |
| history 修改 | 原地替换 content / 移除行 | 不碰原始 history | 不碰 |
| pre_llm_call 返回值 | `None`（mutation 通过浅拷贝传播） | 文本字符串拼接 | `None` |
| 恢复 | post_llm_call 从 snapshot 全量恢复 | 无需恢复 | 无需恢复 |
| 数据积累 (C-stage) | 正常进行 | 正常进行 | 正常进行 |
| 适用场景 | 高压缩比，LLM 仅见汇编版 | 注入额外摘要，保留全量原文 | 仅用于数据采集，不改变上下文 |

**行类型注射规则**（mutation 模式 v5.1，由 `_build_aligned_outcomes` 决策）：

| history 行 | L2 | L1 | L0 |
|---|---|---|---|
| `user` | None（保留原文） | `_format_l1_for_display(l1)` | `l0_text` |
| `assistant{tc}` | None（保留原文） | `_format_tool_group_assembly(l1)` | `_format_tool_group_assembly(l1/l0)` → 紧凑格式 |
| `tool` | `""`（移除） | `""`（移除） | `""`（移除） |
| `assistant_fin` | None | None | None |

**返回值语义**：
- `None` = 保留原文
- `""` = 行将被移除（所有 tool 行均被删除）
- `str` = 替换 content（无标签）

**格式化函数**：

| 函数 | 输入 | 输出示例 |
|------|------|---------|
| `_format_l1_for_display(l1_json)` | `{"core_change":"查了文件系统","new_materials":["文件A"], ...}` | `查了文件系统\n  文件A` |
| `_format_tool_group_assembly(l1, history, turn_idx, gidx)` | `{"group_intent":"查文件","group_result":"aaa;x.py","tool_count":3,"state":"ok"}` + history 工具行 | `【工具组:查文件→aaa;x.py(3个,ok)】`<br>`  read_file: aaa ×2` |
| `_format_group_summary(l1)` | **已弃用 v5.1** — 由 `_format_tool_group_assembly` 替代 | — |

**×N 合并**：移入 `_format_tool_group_assembly` 内部。连续相同 tool_name+detail 的工具行在组内合并 → `read_file: aaa ×2`。`_merge_consecutive_tool_outcomes()` 不再被调用（保留供遗留测试引用）。

**bypass_turns 数据流**（v5.1 新增）：
- 在 `_compute_assemble_plan` 中计算最后 2 个对话轮的索引集合
- 通过 `_AssemblePlanResult.bypass_turns` 字段传递给两端
- `_build_aligned_outcomes`：bypass 轮的工具组无条件 L2（原文保留）
- `_build_messages_from_plan`：bypass 轮跳过摘要注入，直接注入原始消息
- **消除两路不一致**：原 `_build_messages_from_plan` 内部硬计算已被移除

**bypass_turns 感知**（`_build_aligned_outcomes`）：
- bypass 轮中 `entry.turn_index in _bypass_set` → 工具组 `assistant{tc}` 行保留原文（`None`）
- 所有 `tool` 行不论 bypass 与否均被删除（`""`）

**输出示例**（v5.1 mutation 模式注入后 conversation_history 变更）：
```
# 对话轮 L1 — user 行被替换
user: "查了文件系统\n  文件A"
# 工具组 L1 — assistant{tc} 行被替换（含全部工具详情）
assistant{tc}: "【工具组:查文件→aaa;x.py(3个,ok)】
  search: *.py → 3 hit
  read_file: /tmp/test.py (60 lines) ×2"
# tool 行全部删除（空字符串 → del conversation_history[i]）
# final assistant — 保留原文
assistant_fin: "文件内容已查到"
```

### `_on_post_llm_call` — 数据积累与快照恢复（C-stage）

```python
def _on_post_llm_call(**kwargs: Any) -> None:
```

**kwargs**:

| 参数                     | 类型 | 说明                             |
| ------------------------ | ---- | -------------------------------- |
| `session_id`           | str  | 当前会话                         |
| `user_message`         | str  | 用户输入文本                     |
| `assistant_response`   | str  | LLM 的文本回复                   |
| `conversation_history` | list | 本轮完整消息历史（含 tool 结果） |

行为：
1. **先同步 `flush_tool_buffer()`** — 将 `_tool_buffer` 中的增量采集数据写入 store
2. **快照恢复** — 优先从 `_saved_history_snapshot` 全量还原 mutation 前的 history；fallback 到 `_saved_history` 按 `id(msg)` 逐条恢复
3. **再异步 `process_turn_async()`** — 生成对话轮摘要
`conversation_history` 传副本（列表拷贝），避免竞态。

**快照两层结构**：
- `_saved_history_snapshot: Optional[List[Dict]]` — 完整消息列表快照，全量 clear+extend 恢复（PR3 新增，优先）
- `_saved_history: Optional[Dict[int, str]]` — 旧式 `id(msg) → content` 映射，逐条恢复（向后兼容）

### `_on_post_api_request` — 工具组结构捕获（PR2 新增）

```python
def _on_post_api_request(**kwargs: Any) -> None:
```

**kwargs**: `api_request_id`, `api_call_count`, `assistant_message`, `finish_reason`, `usage`, `session_id`

行为：从 `assistant_message` 提取 `content`(thought) + `tool_calls` → 创建 `ToolGroupBuffer` 存入 `_tool_buffer[api_request_id]`。

### `_on_pre_tool_call` — 工具预注册（PR2 新增）

```python
def _on_pre_tool_call(**kwargs: Any) -> None:
```

**kwargs**: `tool_name`, `args`, `tool_call_id`, `api_request_id`, `session_id`

行为：在对应 buffer 中注册工具占位（status=pending）。buffer 不存在时 auto-create sentinel（api_call_count=999999）。

### `_on_post_tool_call` — 工具结果填充（PR2 新增）

```python
def _on_post_tool_call(**kwargs: Any) -> None:
```

**kwargs**: `tool_name`, `args`, `result`, `tool_call_id`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message`, `session_id`

行为：将工具执行结果填充到对应 buffer 中。Hermes 原始 status(`ok`/`error`/`blocked`/`cancelled`)透传。

## 存储结构

### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：`~/.hermes/profiles/tester/ca_cache/{session_id}.db`

### turn_cache 表（v5 schema）

**主键**：`(session_id, turn_index, api_call_count, seq_index)`

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | TEXT | 会话 ID |
| `turn_index` | INTEGER | 对话轮次（从 1 开始） |
| `api_call_count` | INTEGER | API 调用序号（0=user, 1..N=API 调用, 999999=final） |
| `seq_index` | INTEGER | 组内消息序号（0=assistant{tc}, 1..M=tool 行） |
| `role` | TEXT | `user` / `assistant` / `tool` / `system` |
| `content` | TEXT | 消息文本内容（thought / tool result / final text） |
| `tool_call_id` | TEXT | 工具调用 ID（仅 role="tool" 时） |
| `tool_name` | TEXT | 工具名（仅 role="tool" 时） |
| `tool_calls_json` | TEXT | assistant 的 tool_calls 定义 JSON |
| `finish_reason` | TEXT | `tool_calls` / `stop` / `length`（仅 assistant 行） |
| `api_request_id` | TEXT | API 调用唯一 ID |
| `duration_ms` | INTEGER | 执行耗时（仅 tool 行） |
| `status` | TEXT | `ok` / `error` / `blocked` / `cancelled` |
| `error_type` | TEXT | 错误类型 |
| `error_message` | TEXT | 错误消息 |
| `usage_json` | TEXT | Token 用量 JSON |
| `l1_text` | TEXT | CA 摘要 JSON |
| `l0_text` | TEXT | 单行摘要（≤100 字符） |
| `l0_embedding` | BLOB | L0 嵌入向量（4096 字节） |
| `l1_embedding` | BLOB | L1 嵌入向量（4096 字节） |
| `bm25_tokens` | TEXT | BM25 分词 |
| `token_offset` | INTEGER | 累计 Token 偏移 |
| `query_embedding` | BLOB | 用户消息嵌入向量 |
| `_assemble_status` | INTEGER | 0=成功, 1=降级, 2=永久跳过 |
| `backfill_attempts` | INTEGER | L-stage 尝试次数 |
| `created_at` | TEXT | 创建时间戳 |

**行类型速查**：

| 行类型 | `api_call_count` | `seq_index` | `role` | 关键特征 |
|--------|-----------------|-------------|--------|---------|
| user | 0 | 0 | user | 用户输入 |
| assistant{tc} | N（≥1） | 0 | assistant | 含 `tool_calls_json`，`finish_reason="tool_calls"` |
| tool | N（≥1） | ≥1 | tool | 含 `tool_call_id`，`status` |
| final assistant | 999999 | 0 | assistant | `finish_reason="stop"` |

### 存储特性

- **L2 是递增快照**：每条 subturn 存独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0 + l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache
- **WAL 模式**：Store 初始化时设置 PRAGMA journal_mode=WAL

## 内存缓存结构（AssemblyCache）

| 缓存 | Key 类型 | 说明 |
|------|---------|------|
| `l0_texts` | `Dict[int, str]` | 对话轮 L0 文本，key=turn_index |
| `l1_texts` | `Dict[int, str]` | 对话轮 L1 JSON，key=turn_index |
| `tool_l0_texts` | `Dict[Tuple[int,int], str]` | 个体工具 L0，key=(turn_index, seq_index) |
| `tool_l1_texts` | `Dict[Tuple[int,int], str]` | 个体工具 L1 JSON，key=(turn_index, seq_index) |
| `tool_group_l0_texts` | `Dict[Tuple[int,int], str]` | 工具组 L0，key=(turn_index, api_call_count) |
| `tool_group_l1_texts` | `Dict[Tuple[int,int], str]` | 工具组 L1 JSON，key=(turn_index, api_call_count) |

`add_tool_group()` 在 `flush_tool_buffer()` 末尾同步调用，保证缓存与 DB 一致。

## 关键环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CA_EMBED_BACKEND` | `ollama` | 嵌入后端 |
| `CA_EMBED_MODEL` | `qwen3-embedding:0.6b` | 嵌入模型 |
| `CA_EMBED_ENDPOINT` | `http://localhost:11439` | 嵌入服务端点 |
| `CA_LLM_MODEL` | `qwen3-4b-instruct` | L1 摘要生成模型 |
| `CA_LLM_ENDPOINT` | `http://localhost:11440` | LLM 服务端点 |
| `CA_EMBED_TIMEOUT` | 30 | 嵌入超时（秒） |
| `CA_LLM_TIMEOUT` | 180 | LLM 超时（秒） |
| `CA_CONTEXT_LENGTH` | 50000 | 上下文 Token 预算上限 |
| `CA_L1_TEMPERATURE` | 0.3 | L1 摘要生成温度 |
| `CA_L1_MAX_TOKENS` | 800 | L1 摘要生成最大 Token 数 |
| `CA_PROTECT_TAIL_TOKENS` | 10000 | 对话轮尾区保护 Token 数 |
| `CA_TOOL_TAIL_TURN_COUNT` | 2 | 工具轮尾区保留最近对话轮数 |
| `CA_SYSTEM_TAIL_TURN_COUNT` | 2 | 系统尾区：最近 N 条系统消息原文透传 |
| `CA_COMPRESSION_THRESHOLD` | 0.50 | 压缩警戒比值 |
| `CA_HISTORY_INJECTION` | `replace` | 注入模式：`replace`(mutation)/`append`(annotation)/`off`(仅数据积累) |
| `CA_HISTORY_MUTATE` | (已弃用) | 2值开关，`1`→替换 `0`→追加。未设 `CA_HISTORY_INJECTION` 时兼容此旧变量 |
| `CA_LLM_THINK` | 未设置 | L1 LLM think 参数（1/0/true/false） |

### 话题拣配配置（v4.6.0）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CA_TOPIC_BOUNDARY_DISTANCE` | 0.50 | 话题边界检测余弦距离阈值 |
| `CA_TOPIC_JACCARD_ENTRY` | 0.03 | 话题分割首次合并 Jaccard 阈值 |
| `CA_TOPIC_JACCARD_CHAIN` | 0.04 | 话题分割链内扩展 Jaccard 阈值 |
| `CA_TOPIC_RADIUS_WEIGHT` | 2.0 | 半径公式中最近邻距离的权重系数 |
| `CA_TOPIC_MAX_UPGRADE` | 10 | 检索升级最大 topic 数 |
| `CA_TOPIC_BG_LEVEL` | `L0` | BG 话题固定级别 |

## L1 摘要生成架构

### 关键模块

| 模块/文件 | 职责 |
|-----------|------|
| `ca/post_process.py` | 防御性解析器：`parse_v1_markdown_xml`(主入口)、`_safe_truncate`(智能截断)、`_json_to_v1_markdown`(格式转换)、`ItemState` 枚举、状态前缀提取 |
| `ca/prompts.py` | `L1_GENERATION_PROMPT` — "研发对话意图分析器"人设 |
| `ca/tool_summarizer.py` | `ToolSummarizer` 类：`summarize()` 按工具名分发, `generate_group_summary()` 组摘要 |
| `ca/__init__.py :: _call_llm_for_l1` | LLM 调用，返回 `Tuple[str,str]`(response, finish_reason) |
| `ca/__init__.py :: _compute_assemble_plan` | Plan 计算阶段（含 bypass_turns 生产） |
| `ca/__init__.py :: _build_aligned_outcomes` | 1:1 对齐（mutation 模式入口，v5.1 新增 bypass_turns 感知） |
| `ca/__init__.py :: _format_tool_group_assembly` | 工具组紧凑格式（v5.1 新增，替代 _format_group_summary） |
| `ca/__init__.py :: _format_l1_for_display` | 对话轮 L1 JSON → 可读文本 |
| `ca/__init__.py :: _format_group_summary` | **已弃用 v5.1** — 由 _format_tool_group_assembly 替代 |
| `ca/__init__.py :: _format_single_tool` | 个体工具格式化（v5.1 保留供遗留引用，不再被生产调用） |
| `ca/__init__.py :: _build_messages_from_plan` | 标签注入模式（annotation 模式/旧路径，v5.1 新增 bypass_turns 参数） |
| `ca/ooda_parser.py` | OODA 分区、中文别名映射 |
| `ca/store.py :: format_previous_summary_for_prompt` | DB JSON → Markdown 适配器 |
| `ca/config.py` | 配置项热重载 |

### 数据流

```
旧 DB JSON (5类英key)               LLM 输出 (4类中文+XML)
    │                                      │
    ▼                                      ▼
format_previous_summary_for_prompt()    parse_v1_markdown_xml()
    │                                      │
    ├─ None/"无" → "无"                    ├─ 提取 <core_change>
    ├─ JSON → _json_to_v1_markdown         ├─ 提取 OODA 4 类
    └─ 纯文本 → 原样返回                   └─ 解析 → (l1_dict, l0_text, core_state)
           │                                      │
           ▼                                      ▼
    ┌──────────────────────────────────────────────┘
    ▼
clean_increment() → DB (5类英key JSON，格式不变)
```

## 断路器

| 项目     | 值                                                       |
| -------- | -------------------------------------------------------- |
| 文件路径 | `{hermes_home}/.ca_assembler_state_{PID}.json`         |
| 状态格式 | `{"failures": N, "retry_after": null \| ISO_timestamp}` |
| 触发阈值 | 连续 3 次失败                                            |
| 冷却时间 | 1 小时                                                   |
| 恢复     | 1 次成功 reset 或超时后自动清除                          |

## 运行时验证

### 插件是否已注册

```bash
grep -i 'CA plugin started for session' ~/.hermes/profiles/tester/logs/agent.log
```

### 数据是否正在积累

```python
import sqlite3
from pathlib import Path
db = Path.home() / '.hermes/profiles/tester/ca_cache/{session_id}.db'
conn = sqlite3.connect(str(db))
cur = conn.cursor()
cur.execute('SELECT COUNT(*), COUNT(l0_embedding) FROM turn_cache')
n, emb = cur.fetchone()
print(f'{n} rows, {emb} with embeddings')
```

### Token 水位查询

```python
from ca import session_manager
from pathlib import Path
db_path = Path.home() / '.hermes/profiles/tester/ca_cache/{session_id}.db'
engine = session_manager.get(session_id, str(db_path))
water = engine.debug_token_budget()
print(water)
```

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `context_length` | int | 总 Token 窗口上限（`CA_CONTEXT_LENGTH`） |
| `budget_max` | int | 可用预算上限（`context_length × 0.95`） |
| `used_tokens` | int | 当前累计 token 偏移 |
| `remaining` | int | 剩余可用预算 |
| `usage_pct` | float | 使用率百分比 |

## 测试接口清单

**测试总数**：386 条（活跃 295 条 + legacy 91 条）

### Fixtures（`tests/conftest.py`）

| Fixture | Scope | 签名 | 说明 |
|---------|-------|------|------|
| `hardware_info` | session | `() -> dict` | CPU/内存信息，用于测试报告 |
| `fd_checker` | function | `() -> FdWatcher` | 文件描述符泄漏检测 |
| `ca_engine` | function | `(tmp_path) -> ContextAssembler` | 标准引擎实例，自动 mock embedding+LLM |
| `engine` | function | `(ca_engine) -> ContextAssembler` | `ca_engine` 别名 |
| `_mock_embed` | function (autouse) | `(ca_engine) -> None` | 自动 mock embed → `[0.1]*768` |
| `_mock_llm` | function (autouse) | `() -> None` | 自动 mock L1 → `('mock_response', 'stop')` |

### 活跃测试文件

| 文件 | 测试数 | 范围 |
|------|--------|------|
| `test_pr3_injection.py` | 8 | 三级摘要+注入 |
| `test_aligned_outcomes.py` | 21 | 1:1 对齐注入 |
| `test_tool_buffer.py` | 15 | Buffer 层 |
| `test_c.py` | 25 | 对话轮 C‑stage |
| `test_a.py` | 20 | 对话轮 A‑stage |
| `test_v440.py` | 40 | 工具轮 C‑stage + A‑stage + L‑stage |
| `test_v460.py` | 53 | 话题分割 |
| `test_config.py` | 11 | Config |
| `test_parse_v1.py` | 27 | parse_v1_markdown_xml |
| `test_store_adapter.py` | 10 | format_previous_summary_for_prompt |
| `test_store.py` | 12 | Store 层 |
| `test_embedding.py` | 6 | Embedding 客户端 |
| `test_plugin.py` | 31 | 插件断路器+生命周期+注入 |
| `test_circuit.py` | 7 | 断路器 |
| `test_health.py` | 5 | 健康检查 |
| `test_lifecycle.py` | 7 | 生命周期 |
| `test_degradation.py` | 2 | 降级 |
| `test_quality.py` | 7 | 质量评估（全部 stub/skip） |
| `test_system.py` | 3 | 端到端（全部 skip） |

### 运行方式

```bash
# 运行全部活跃测试（需 ca_assembler 目录为 cwd）
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/test_pr3_injection.py tests/test_aligned_outcomes.py tests/test_tool_buffer.py tests/test_c.py tests/test_a.py tests/test_v440.py tests/test_v460.py tests/test_config.py tests/test_parse_v1.py tests/test_store_adapter.py tests/test_store.py tests/test_embedding.py tests/test_plugin.py tests/test_circuit.py tests/test_health.py tests/test_lifecycle.py tests/test_degradation.py tests/test_quality.py tests/test_system.py -v -p no:cacheprovider -o "addopts="

# 单文件
python -m pytest tests/test_c.py -v -p no:cacheprovider -o "addopts="
```

## 预算实测结论（2026-06-14）

18 轮对话实测：

- 所有行 `_assemble_status=0`（无降级）
- budget 从未耗尽：~72K 预算 vs ~32K 使用
- `budget=0` 只跳过检索升级（`retriever.retrieve()` 不执行）
- **Middle L0 永远生成，不受预算约束**——这是设计
- `_system_overhead` 默认 20K 仅作保守缓冲区，动态测量代码已移除

## ⚠️ 关键概念：CA 不是 context engine

**CA 是 Hermes 插件（plugin），不是 context engine。**

Hermes 有两条完全独立的机制：

1. **Plugin hooks** → `plugins.enabled` 中的 `ca_assembler`。通过 `register()` 注册的 8 个 hook 回调工作。
2. **Context engine** → `context.engine` 配置项。只从仓库 `plugins/context_engine/` 子目录加载引擎，与用户 profile 的 `plugins/ca_assembler/` 毫无关系。

**`context.engine` 设成什么、是否回退，都不影响 CA 插件的工作。**

## 与 source project 的差异

| 差异点                 | source 项目                       | 当前部署                              |
| ---------------------- | --------------------------------- | ------------------------------------- |
| 代码位置               | `~/projects/context-assembler/` | `plugins/ca_assembler/` 自包含副本  |
| `register()`         | 不存在                            | 已添加，注册 8 个 hooks               |
| `_state_file_path()` | `Path.home() / ".hermes"`       | `get_hermes_home()`（profile 感知） |
| `sys.path`           | 无特殊处理                        | 本地 `ca/` 子目录优先               |
| 激活方式               | `context.engine: ca_assembler`  | `plugins.enabled: [ca_assembler]`   |
| `_build_aligned_outcomes` | 不存在                        | 已实现，mutation 模式主要入口        |
| 注入方式               | 标签注入 `[~/N/0]`             | 无标签 1:1 对齐替换（mutation 模式） |

## 脚本工具

| 脚本 | 说明 |
|------|------|
| `scripts/backfill_tool_summaries.py` | 历史工具摘要回填（修复存量数据） |
| `scripts/benchmark_l1.py` | L1 生成性能基准测试 |

## 相关文档

所有文档按类别归档在 `docs/` 下：

| 类别 | 目录 | 内容 |
|------|------|------|
| 变更历史 | `docs/changelog.md` | 全版本变更记录（唯一权威源） |
| 设计 | `docs/design/` | 技术方案、话题拣选、系统分析、改进方案 |
| 调试 | `docs/debug/` | 调试报告、修复验证、部署调试 |
| 评审 | `docs/review/` | 交叉评审（开发线/测试线） |
| 测试计划 | `docs/test-plans/` | 试验计划 |
| L1 重构 | `docs/ca-l1-refactor/` | 白皮书、需求、方案、测试 |
| 工具轮重构 | `docs/design/` | 分析文档（tool-turn-refactor-analysis.md）、技术方案（technical-plan.md） |
