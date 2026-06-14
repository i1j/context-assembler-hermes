# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项          | 值                                                                                |
| ----------- | --------------------------------------------------------------------------------- |
| 版本        | v5.3.0-simple (过渡简化版)                                                           |
| 注入方式    | 简化替换（_simple_mutation_mode） — DB 直查 L1，不依赖 turn_plan/cache/编码表        |
| plugin.yaml | v5.2.1（待同步）                                                                    |
| 部署方式    | 自包含独立副本                                                                    |
| 插件路径    | `~/.hermes/profiles/tester/plugins/ca_assembler/`                               |
| 核心引擎    | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`）                 |
| 插件适配    | `plugins/ca_assembler/__init__.py`（入口 `CAContextAssemblerPlugin`）         |
| 启用方式    | `plugins.enabled: [ca_assembler, ...]`                                          |
| 源项目      | `~/projects/context-assembler/`（已分化，含独有修复）                           |

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
2. 引擎初始化时加载话题分割阈值：
   - 默认策略（`CA_TOPIC_JACCARD_MERGE=0.07`）→ 全部实义轮合并，由 20K 尾区切割
   - 如启用自适应（见后文）→ `0.6 × last_ideal + 0.4 × global_avg`，限幅 `[0.08, 0.35]`
   - 阈值在本会话内固定不变
3. 检查断路器 `is_available()` → 3 次失败则 1 小时冷却
4. 从 `get_hermes_home()` 获取 profile 基路径
5. DB 路径：`{hermes_home}/ca_cache/{session_id}.db`
6. 通过 `session_manager.get(session_id, db_path)` 创建引擎
7. 注册到模块级 `_engines` 字典（`session_id → plugin`）
8. 成功后日志：`"CA plugin started for session {session_id}"` 含阈值信息

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

行为：

1. 持久化会话理想阈值 → `{ca_cache}/topic_threshold_meta.json`
2. 从 `_engines` 移除 → `engine.reset()` + 清除断路器状态

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

**返回值**: `Optional[str]`，详见 [v5.2 注入规则分析](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md#3-tool_plan-注入重构v52)。

注入模式由 `Config.HISTORY_INJECTION` 控制：

| 模式 | 入口 | 效果 | pre_llm_call 返回值 | 恢复 |
|------|------|------|-------------------|------|
| **replace**（默认） | `_simple_mutation_mode` | DB 直查 L1 替换 tool 行，尾部保护 | `None`（浅拷贝传播） | snapshot 全量恢复 |
| **append** | `_build_messages_from_plan`+tool_plan | 摘要拼入 user message | 文本字符串 | 无需 |
| **off** | — | 仅数据积累 | `None` | 无需 |

**行类型注射规则**（replace 模式 v5.3.0-simple）：

| 条件 | user | assistant_fin | assistant{tc} | tool |
|------|------|--------------|--------------|------|
| 尾部保护区（倒数第 3 个 user 之后） | 原文保留 | 原文保留 | 原文保留 | 原文保留 |
| 保护区外 | 原文保留 | 原文保留 | `_format_tool_group_assembly(l1)` | l1_text 或 l0_text |

不区分 bg_review（统一规则）。DB 查询用 `store.read_turn_texts`(tool_group) + `store.read_tool_rows_for_group`。无碰撞风险（原生 `(turn, api, seq)` 过滤）。
# tool 行按 tool_plan 生成独立摘要（无标签，各自占一行）
tool: "read_file: /tmp/test.py (60 lines)"
tool: "search: *.py → 3 hits"
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

| 字段                  | 类型    | 说明                                                      |
| --------------------- | ------- | --------------------------------------------------------- |
| `session_id`        | TEXT    | 会话 ID                                                   |
| `turn_index`        | INTEGER | 对话轮次（从 1 开始）                                     |
| `api_call_count`    | INTEGER | API 调用序号（0=user, 1..N=API 调用, 999999=final）       |
| `seq_index`         | INTEGER | 组内消息序号（0=assistant{tc}, 1..M=tool 行）             |
| `role`              | TEXT    | `user` / `assistant` / `tool` / `system`          |
| `content`           | TEXT    | 消息文本内容（thought / tool result / final text）        |
| `tool_call_id`      | TEXT    | 工具调用 ID（仅 role="tool" 时）                          |
| `tool_name`         | TEXT    | 工具名（仅 role="tool" 时）                               |
| `tool_calls_json`   | TEXT    | assistant 的 tool_calls 定义 JSON                         |
| `finish_reason`     | TEXT    | `tool_calls` / `stop` / `length`（仅 assistant 行） |
| `api_request_id`    | TEXT    | API 调用唯一 ID                                           |
| `duration_ms`       | INTEGER | 执行耗时（仅 tool 行）                                    |
| `status`            | TEXT    | `ok` / `error` / `blocked` / `cancelled`          |
| `error_type`        | TEXT    | 错误类型                                                  |
| `error_message`     | TEXT    | 错误消息                                                  |
| `usage_json`        | TEXT    | Token 用量 JSON                                           |
| `l1_text`           | TEXT    | CA 摘要 JSON                                              |
| `l0_text`           | TEXT    | 单行摘要（≤100 字符）                                    |
| `l0_embedding`      | BLOB    | L0 嵌入向量（4096 字节）                                  |
| `l1_embedding`      | BLOB    | L1 嵌入向量（4096 字节）                                  |
| `bm25_tokens`       | TEXT    | BM25 分词                                                 |
| `token_offset`      | INTEGER | 累计 Token 偏移                                           |
| `query_embedding`   | BLOB    | 用户消息嵌入向量                                          |
| `_assemble_status`  | INTEGER | 0=成功, 1=降级, 2=永久跳过                                |
| `backfill_attempts` | INTEGER | L-stage 尝试次数                                          |
| `created_at`        | TEXT    | 创建时间戳                                                |

**行类型速查**：

| 行类型          | `api_call_count` | `seq_index` | `role`  | 关键特征                                               |
| --------------- | ------------------ | ------------- | --------- | ------------------------------------------------------ |
| user            | 0                  | 0             | user      | 用户输入                                               |
| assistant{tc}   | N（≥1）           | 0             | assistant | 含 `tool_calls_json`，`finish_reason="tool_calls"` |
| tool            | N（≥1）           | ≥1           | tool      | 含 `tool_call_id`，`status`                        |
| final assistant | 999999             | 0             | assistant | `finish_reason="stop"`                               |

### 存储特性

- **L2 是递增快照**：每条 subturn 存独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0 + l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache
- **WAL 模式**：Store 初始化时设置 PRAGMA journal_mode=WAL

## 内存缓存结构（AssemblyCache）

| 缓存                    | Key 类型                      | 说明                                             |
| ----------------------- | ----------------------------- | ------------------------------------------------ |
| `l0_texts`            | `Dict[int, str]`            | 对话轮 L0 文本，key=turn_index                   |
| `l1_texts`            | `Dict[int, str]`            | 对话轮 L1 JSON，key=turn_index                   |
| `tool_l0_texts`       | `Dict[Tuple[int,int], str]` | 个体工具 L0，key=(turn_index, seq_index)         |
| `tool_l1_texts`       | `Dict[Tuple[int,int], str]` | 个体工具 L1 JSON，key=(turn_index, seq_index)    |
| `tool_group_l0_texts` | `Dict[Tuple[int,int], str]` | 工具组 L0，key=(turn_index, api_call_count)      |
| `tool_group_l1_texts` | `Dict[Tuple[int,int], str]` | 工具组 L1 JSON，key=(turn_index, api_call_count) |

`add_tool_group()` 在 `flush_tool_buffer()` 末尾同步调用，保证缓存与 DB 一致。

## 关键环境变量

| 变量                          | 默认值                     | 说明                                                                           |
| ----------------------------- | -------------------------- | ------------------------------------------------------------------------------ |
| `CA_EMBED_BACKEND`          | `ollama`                 | 嵌入后端                                                                       |
| `CA_EMBED_MODEL`            | `qwen3-embedding:0.6b`   | 嵌入模型                                                                       |
| `CA_EMBED_ENDPOINT`         | `http://localhost:11439` | 嵌入服务端点                                                                   |
| `CA_LLM_MODEL`              | `qwen3-4b-instruct`      | L1 摘要生成模型                                                                |
| `CA_LLM_ENDPOINT`           | `http://localhost:11440` | LLM 服务端点                                                                   |
| `CA_EMBED_TIMEOUT`          | 30                         | 嵌入超时（秒）                                                                 |
| `CA_LLM_TIMEOUT`            | 180                        | LLM 超时（秒）                                                                 |
| `CA_CONTEXT_LENGTH`         | 50000                      | 上下文 Token 预算上限                                                          |
| `CA_L1_TEMPERATURE`         | 0.3                        | L1 摘要生成温度                                                                |
| `CA_L1_MAX_TOKENS`          | 800                        | L1 摘要生成最大 Token 数                                                       |
| `CA_PROTECT_TAIL_TOKENS`    | 20000（来自 settings.yaml） | 当前话题块 Token 保护安全阀。来源：`ca/settings.yaml`→`_YAML_DEFAULTS`→env `CA_PROTECT_TAIL_TOKENS` |
| `CA_TOOL_TAIL_TURN_COUNT`   | 2                          | 工具轮尾区保留最近对话轮数                                                     |
| `CA_SYSTEM_TAIL_TURN_COUNT` | 2                          | 系统尾区：最近 N 条系统消息原文透传                                            |
| `CA_COMPRESSION_THRESHOLD`  | 0.50                       | 压缩警戒比值                                                                   |
| `CA_HISTORY_INJECTION`      | `replace`                | 注入模式：`replace`(mutation)/`append`(annotation)/`off`(仅数据积累)     |
| `CA_HISTORY_MUTATE`         | (已弃用)                   | 2值开关，`1`→替换 `0`→追加。未设 `CA_HISTORY_INJECTION` 时兼容此旧变量 |
| `CA_LLM_THINK`              | 未设置                     | L1 LLM think 参数（1/0/true/false）                                            |

### 话题拣配配置（v4.6.0）

| 变量                           | 默认值 | 说明                           |
| ------------------------------ | ------ | ------------------------------ |
| `CA_TOPIC_BOUNDARY_DISTANCE` | 0.50   | 话题边界检测余弦距离阈值       |
| `CA_TOPIC_JACCARD_ENTRY`     | 0.03   | 话题分割首次合并 Jaccard 阈值（需 todo 重叠） |
| `CA_TOPIC_JACCARD_CHAIN`     | 0.04   | 话题分割链内扩展 Jaccard 阈值（需 todo 重叠） |
| `CA_TOPIC_JACCARD_MERGE`   | 0.07   | 话题分割 Jaccard 独立合并阈值（无需 todo 重叠）。设为 0.07 确保全部 S→S 实义轮合并，由 20K 尾区保护完成唯一实质性切割 |
| `CA_TOPIC_RADIUS_WEIGHT`     | 2.0    | 半径公式中最近邻距离的权重系数 |
| `CA_TOPIC_MAX_UPGRADE`       | 10     | 检索升级最大 topic 数          |
| `CA_TOPIC_BG_LEVEL`          | `L0` | BG 话题固定级别                |

## 话题分割算法（v5.2.1）

### R1: BG 分类

检查 `new_materials`, `objective_facts`, `consensus`, `todo` 四个内容字段：

- 全空 → **BG 轮**（后台/过渡轮）
- 任一非空 → **实义轮**
- 连续 BG → 合并到同一话题
- BG↔实义 → 强制分裂

### R2: 实义轮合并（三条路径，任意满足即合并）

| 路径 | 条件 | 说明 |
|------|------|------|
| todo 链内扩展 | `has_todo_overlap` AND `J ≥ 0.04` AND 已在链中 | 上轮 TODO 与下轮 core_change/new_materials 精确匹配 |
| todo 首次合并 | `has_todo_overlap` AND `J ≥ 0.03` | 同上，入口级 |
| **Jaccard 独立合并**（v5.2.1 新增） | **`J ≥ threshold`** | **不依赖 todo 重叠**，用于长对话同话题扩展 |

`threshold` 默认 **0.07**（见下方"由 20K 切割"说明）。

### 由 20K 切割（v5.2.1 默认策略）

话题分割的 Jaccard 独立合并阈值设为 **0.07**，这意味着所有 S→S（实义→实义）连续对都满足合并条件（28 对中最低 J=0.0729），实义轮在话题上全部合并为一个连续块。

话题分割在此策略下的唯一作用是：

- **BG 轮隔离**：将"Review the conversation"等无内容轮从实义流中独立出去
- **实义轮合并**：将所有实义轮归入一个大话题
- **强制话题切换**：用户消息含"换一个话题"等短语时，无条件分裂

### 强制话题切换

当用户消息包含以下任一词组时，该轮在话题分割中**强制分裂**，不受任何合并条件影响：

```
"换一个话题", "换个话题", "切换话题", "新话题",
"另一件事", "另一个问题", "换个方向",
"换一个问题", "topic switch", "change topic", "next topic"
```

检测发生在 `assemble()` 中，由 `_detect_forced_split_turns(messages)` 扫描 `conversation_history` 中的 `role="user"` 消息实现。

真正的上下文质量由 **20K 尾区保护区** 保证——尾区内的轮强制 L2 原文透传。即使整个实义话题远超 20K，保护区外的轮按话题分级降级，不会破坏尾区内的对话质量。

如需改变此行为（例如希望话题分割承担更多上下文精细化管理），增大 `CA_TOPIC_JACCARD_MERGE` 即可。

#### 自适应阈值（可选，v5.2.1）

如不使用"由 20K 切割"策略，可恢复自适应阈值。将 `CA_TOPIC_JACCARD_MERGE` 设为 `0.18`（或留空由自适应计算），此时机制为：

```
会话 N 启动:
  T_start = 0.6 × last_ideal + 0.4 × global_avg (种子 0.18)
  限幅 [0.08, 0.35]

每轮 assemble() 后:
  收集所有已知 S→S 对的 Jaccard 值
  计算 25th percentile 作为本会话的理想值
  （首次 3 对以上数据后写入 _ideal_threshold_this_session）

会话 N 结束 (reset):
  persist_ideal_threshold()
  → 更新 {ca_cache}/topic_threshold_meta.json
  → 影响下一会话的 T_start
```

```json
{
  "global_sum": 0.93,
  "global_count": 5,
  "last_ideal": 0.21
}

```

### 非确定性修复

`_add_bigrams` 中使用 `sorted(s)` 保证 `''.join(s)` 跨运行一致。

## L1 摘要生成架构

关键模块表见上。数据流详见 [v5.2 分析报告](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md#2-l0_embedding-孤儿数据清理)。

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

| 字段               | 类型  | 说明                                       |
| ------------------ | ----- | ------------------------------------------ |
| `context_length` | int   | 总 Token 窗口上限（`CA_CONTEXT_LENGTH`） |
| `budget_max`     | int   | 可用预算上限（`context_length × 0.95`） |
| `used_tokens`    | int   | 当前累计 token 偏移                        |
| `remaining`      | int   | 剩余可用预算                               |
| `usage_pct`      | float | 使用率百分比                               |

## 测试接口清单

**活跃**: 315 passed, 4 failed（既存test_a.py）, 20 skipped（全量+legacy: 378/31/20/1）

```bash
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/ --tb=short -q -p no:cacheprovider -o "addopts="
```

### 调试记录

详见 `docs/analysis/` 分析报告和 `docs/debug/` 调试记录。

### 预算实测

所有行 `_assemble_status=0`，budget 从未耗尽。详见 [v5.2 分析报告](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md)。

## ⚠️ 关键概念：CA 不是 context engine

**CA 是 Hermes 插件（plugin），不是 context engine。**

Hermes 有两条完全独立的机制：

1. **Plugin hooks** → `plugins.enabled` 中的 `ca_assembler`。通过 `register()` 注册的 8 个 hook 回调工作。
2. **Context engine** → `context.engine` 配置项。只从仓库 `plugins/context_engine/` 子目录加载引擎，与用户 profile 的 `plugins/ca_assembler/` 毫无关系。

**`context.engine` 设成什么、是否回退，都不影响 CA 插件的工作。**

## 与 source project 的差异

| 差异点                      | source 项目                       | 当前部署                              |
| --------------------------- | --------------------------------- | ------------------------------------- |
| 代码位置                    | `~/projects/context-assembler/` | `plugins/ca_assembler/` 自包含副本  |
| `register()`              | 不存在                            | 已添加，注册 8 个 hooks               |
| `_state_file_path()`      | `Path.home() / ".hermes"`       | `get_hermes_home()`（profile 感知） |
| `sys.path`                | 无特殊处理                        | 本地 `ca/` 子目录优先               |
| 激活方式                    | `context.engine: ca_assembler`  | `plugins.enabled: [ca_assembler]`   |
| `_simple_mutation_mode` | 不存在                            | 新增，mutation 模式入口（v5.3.0-simple） |
| 注入方式                    | 标签注入 `[~/N/0]`              | DB 直查 L1 替换 tool 行，不依赖 plan/cache |
| `bg_review A-stage`       | 不支持                            | gate 跳过，C-stage 写 biz_category    |
| l0_embedding                | 一直计算                          | 已注释（无人消费）                    |
| 工具组输出                  | 旧紧凑格式（header+各工具详情）   | v5.2 精简为仅 header                 |
| `_add_bigrams 确定性`      | 集合 `''.join(s)` 无心化不稳定  | v5.2.1 用 `sorted(s)` 保证确定性       |
| 话题分割合并路径            | 仅 todo 重叠一条路径            | v5.2.1 新增 Jaccard 独立合并 + 自适应阈值 |
| 编码表 (conv_encoding)       | 不存在                            | v5.3.0-simple 已废弃（线程安全 + key碰撞问题，被 _simple_mutation_mode 取代） |

## 脚本工具

| 脚本                                   | 说明                             |
| -------------------------------------- | -------------------------------- |
| `scripts/backfill_tool_summaries.py` | 历史工具摘要回填（修复存量数据） |
| `scripts/benchmark_l1.py`            | L1 生成性能基准测试              |

## 相关文档

所有文档按类别归档在 `docs/` 下：

| 类别       | 目录                            | 内容                                                                      |
| ---------- | ------------------------------- | ------------------------------------------------------------------------- |
| 变更历史   | `docs/changelog.md`           | 全版本变更记录（唯一权威源）                                              |
| 分析       | `docs/analysis/`              | 缓存分析、注入重构报告（v5.1）                                            |
| 调试       | `docs/debug/`                 | 调试记录（_simple_mutation_mode、编码表验证、占位符根因等）               |

## 知识图谱（graphify）

`graphify-out/` 目录包含项目代码的静态知识图谱分析产物，用于快速理解架构和数据流。

| 文件 | 用途 |
|------|------|
| `GRAPH_REPORT.md` | 图谱概况：2210 节点、2930 边、303 社区。God Nodes 排名、边关系分布、动态数据流、建议查询问题 |
| `graph.html` | 可交互图谱（浏览器打开），可视化节点与边关系 |
| `graph.json` | 完整图谱数据（节点+边），可被代码分析工具消费 |
| `manifest.json` | 文件清单（AST 哈希、mtime），用于增量更新检测 |
| `cache/` | AST 解析缓存，加速后续 graphify 重建 |
| `.graphify_labels.json` | 社区标签映射 |

**何时使用**：代码重构前、跨模块数据流追踪、定位 God Nodes（高耦合中心如 `ContextAssembler` 100 度、`SQLiteStore` 62 度）。
