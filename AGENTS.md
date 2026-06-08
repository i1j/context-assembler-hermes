# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项       | 值                                                                        |
| -------- | ------------------------------------------------------------------------- |
| 版本     | v5.0-pr1 |
| plugin.yaml | 声明 v4.5.1（未同步）                |
| 部署方式 | 自包含独立副本                                                            |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/`                       |
| 核心引擎 | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`）         |
| 插件适配 | `plugins/ca_assembler/__init__.py`（入口 `CAContextAssemblerPlugin`） |
| 启用方式 | `plugins.enabled: [ca_assembler, ...]`                                  |
| 源项目   | `~/projects/context-assembler/`（已分化，含独有修复）                   |

## 注册接口

`register(ctx)` 注册 5 个 Hermes 生命周期 hooks：

```python
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call)
    ctx.register_hook("post_llm_call",    _on_post_llm_call)
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
- 调用 `engine.assemble(user_message, context_length)` 完整管线
- 从返回消息列表中提取 `[~/N/0]` / `[~/N/M]` 标记（CA 摘要注入）
- 多条摘要用 `\n\n` 拼接返回
- Hermes 会将返回文本注入到 user message 中

**返回格式示例**：

```
[~/1/0] CA插件运行正常钩子路径完整
[~/1/3] search_files: 无返回数据
[~/1/5] terminal: 配置项 `context.engine` 为 compressor
```

### `_on_post_llm_call` — 数据积累（C-stage）

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

行为：异步调 `engine.process_turn_async(user_message, assistant_response, history_copy)`
`conversation_history` 传副本（列表拷贝），避免竞态。

## 变更历史

### v4.7.1 — L1 状态感知链路集成（2026-06-16）

基于 v1.5.1 Final → v1.6 Final 迭代，新增状态前缀提取与结构化透传，对抗小模型"完成时态"幻觉：

- **ca/post_process.py**：新增 `ItemState` 枚举（DONE/PLANNED/DISCUSSING/UNKNOWN）+ `STATE_PREFIX_REGEX`（含模块级 fail-fast assert）+ `_normalize_state()`（作用域隔离归一化）+ `parse_core_change_state()`（结构化透传，含管理动作降级）；标题统一"决策与共识"→"决策与方案"；`parse_v1_markdown_xml` 返回三元组 `(l1_dict, l0_text, core_state)`
- **ca/prompts.py**：v1.6 Final 版本，`<example>` 标签 3 场景示例，人设"研发对话意图分析器"，优先级规则（已实施 > 计划 > 探讨），`【】`状态标签
- **ca/ooda_parser.py**：`TITLE_ALIASES` 增加"决策与方案"
- **ca/store.py**：新增 `_infer_legacy_state()`（文本自检推断历史状态）+ `MANAGEMENT_ACTION_KEYWORDS` 集成 + `format_previous_summary_for_prompt` 适配
- **ca/__init__.py**：适配新签名；状态注入 `l1_dict["_state"]`，零 schema 变更
- **管理动作关键词修复**（commit 0ee67e8）：匹配逻辑修复，含 AGENTS.md 同步
- **所有测试文件**：标题统一 + 状态前缀场景 + prompt 检测更新

### v5.0-pr1 — 存储重构 + 惰性迁移（2026-06-22）

PR1 为工具轮数据重构的第一阶段，聚焦存储层重构（`ca/store.py`），为 PR2（数据采集重定向）+ PR3（三级注入）奠定基础。

**关键变更**：

- **v5 turn_cache schema**：新主键 `(session_id, turn_index, api_call_count, seq_index)`；消息独立列（`role, content, tool_call_id, tool_name, tool_calls_json, finish_reason`）；元数据列（`api_request_id, duration_ms, status, error_type, error_message, usage_json`）
- **v4 向后兼容**：`turn_type` / `tool_sub_index` / `l2_text` 作为 `GENERATED ALWAYS AS STORED` 虚拟列保留至 PR2
- **turn_plan PK 扩展**：含 `api_call_count` + `seq_index`，支持逐工具调度
- **Readonly 模式**：`SQLiteStore(path, readonly=True)` 以 `?mode=ro` 打开 v4 旧库只读；`readonly=False` 打开 v5 新库读写
- **版本路由**：`_readonly` 标志控制 v4/v5 查询路径——v5 用 `ORDER BY turn_index, api_call_count, seq_index` + `role` 过滤；v4 保留旧 ORDER BY + `turn_type` 过滤
- **writable guard**：v4 DB 通过可写模式打开时 `RuntimeError` 阻断
- **`write_tool_group()` stub**：定义接口契约（PR2 实现）

**设计文档**：
- `pr1-store-plan.md`：PR1 实现方案（4 视角 34 条意见全部闭环）
- `pr1-review-decisions.md`：多视角审查裁决记录
- `docs/tool-turn-refactor/tool-turn-refactor-technical-plan.md`：整体技术方案

### v4.7.0 — L1 摘要系统重构（2026-06-15）

全面吸收白皮书 v1.2 Gold Master 设计（`docs/ca-l1-refactor/ca-l1-whitepaper-v1.2-gm.md`），重构 L1 摘要生成链路：

- **ca/post_process.py**（新增）：`parse_v1_markdown_xml` 防御性解析器 + `_safe_truncate` 智能截断 + `_json_to_v1_markdown` 格式转换
- **ca/prompts.py**：替换为"研发对话意图分析器"人设，4 类 Markdown + `<core_change>` XML 标签
- **ca/__init__.py**：新增 `L1TruncatedException`；`_call_llm_for_l1` 返回 `Tuple[str,str]`；截断检测下沉至 `_run_c_stage`；独立 `temperature`/`max_tokens`
- **ca/config.py**：新增 `L1_TEMPERATURE`(0.3) + `L1_MAX_TOKENS`(800) + validate + reload
- **ca/ooda_parser.py**：`TITLE_ALIASES` 扩展 4 类中文别名
- **ca/store.py**：`format_previous_summary_for_prompt` 历史适配器
- **ca/lstage.py**：同步新签名 + 截断检测
- **ca/stats.py**：新增 4 个统计字段（truncated_fallback / parse_fallback_count / skipped_empty / l1_latency_ms）

**设计哲学**：PDD (Prompt-Driven Development) — 模型负责语义理解和 Markdown 续写，Python 代码负责截断检测、格式清洗、边界校验、新旧数据兼容。

### v4.6.0 — 话题拣选重构（2026-06-13）

重构检索系统为话题分割 + 三级定级 + 检索迁移：

- **ca/retrieval.py**：替换为话题分割引擎：Jaccard 分词聚类、形心与半径定级
- **ca/ooda_parser.py**：OODA 分区 + TITLE_ALIASES 中文别名系统
- **ca/config.py**：新增 6 个 Topic 配置项
- **turn_plan 计算**：compute_turn_plan_v2() 基于话题级别的 Plan 分配
- **Store 层**：新增 topic_group、turn_plan 存储、query_embedding 列
- **Embedding 降级修复**：embed 失败抛异常走 None 降级（非伪向量）
- **Embedding 超时快速降级**：EMBED_MAX_RETRIES 默认 2→0

### v4.5.x — ToolTurn 结构化摘要

工具轮结构化摘要系统：10 个结构化 handler + turn_type/tool_sub_index 双键设计。

### v4.4.x — 初始版本

基础三层管线（C-stage 摘要 → A-stage 组装 → L-stage 补全）+ DB 存储 + 去重 + 断路器。

## 存储结构

### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：`~/.hermes/profiles/tester/ca_cache/{session_id}.db`

### turn_cache 表（v5.0 重构）

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
| `turn_type` | TEXT | 虚拟列（`GENERATED ALWAYS AS`，PR2 后移除） |
| `tool_sub_index` | INTEGER | 虚拟列（`GENERATED ALWAYS AS seq_index`，PR2 后移除） |
| `l2_text` | TEXT | 虚拟列（`GENERATED ALWAYS AS content`，PR2 后移除） |
| `created_at` | TEXT | 创建时间戳 |

**行类型速查**：

| 行类型 | `api_call_count` | `seq_index` | `role` | 关键特征 |
|--------|-----------------|-------------|--------|---------|
| user | 0 | 0 | user | 用户输入 |
| assistant{tc} | N（≥1） | 0 | assistant | 含 `tool_calls_json`，`finish_reason="tool_calls"` |
| tool | N（≥1） | ≥1 | tool | 含 `tool_call_id`，`status` |
| final assistant | 999999 | 0 | assistant | `finish_reason="stop"`

### 存储特性

- **L2 是递增快照**：每条 subturn 存独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0 + l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache
- **WAL 模式**：Store 初始化时设置 PRAGMA journal_mode=WAL

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
| `CA_COMPRESSION_THRESHOLD` | 0.50 | 压缩警戒比值 |
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

## L1 摘要生成架构（v4.7.0 / v4.7.1）

### 关键模块

| 模块/文件 | 职责 |
|-----------|------|
| `ca/post_process.py` | 防御性解析器：`parse_v1_markdown_xml`(主入口)、`_safe_truncate`(智能截断)、`_json_to_v1_markdown`(格式转换)、`ItemState` 枚举、状态前缀提取 |
| `ca/prompts.py` | `L1_GENERATION_PROMPT` — "研发对话意图分析器"人设，4 类 Markdown + `<core_change>` XML 标签 + `<example>` 场景示例 |
| `ca/__init__.py :: _call_llm_for_l1` | LLM 调用，返回 `Tuple[str,str]`(response, finish_reason) |
| `ca/__init__.py :: L1TruncatedException` | 截断异常类，携带 `response_text` 供降级回读 |
| `ca/ooda_parser.py` | OODA 分区、TITLE_ALIASES 中文别名映射 |
| `ca/store.py :: format_previous_summary_for_prompt` | DB JSON → 4 类 Markdown 适配器（含历史状态推断） |
| `ca/config.py` | `L1_TEMPERATURE` + `L1_MAX_TOKENS` 热重载 |

### 数据流

```
旧 DB JSON (5类英key)               LLM 输出 (4类中文+XML)
    │                                      │
    ▼                                      ▼
format_previous_summary_for_prompt()    parse_v1_markdown_xml()
    │                                      │
    ├─ None/"无" → "无"                    ├─ 物理截断尾随噪音
    ├─ JSON → _json_to_v1_markdown         ├─ 提取 <core_change>（含状态前缀）
    │        → 4类 Markdown                ├─ 提取 OODA 4 类列表（通过 TITLE_ALIASES 映射）
    └─ 纯文本 → 原样返回                   └─ 语义短路 + 状态提取 → (l1_dict, l0_text, core_state)
           │                                      │
           ▼                                      ▼
    ┌──────────────────────────────────────────────┘
    ▼
clean_increment() → DB (5类英key JSON，格式不变)
```

### 截断检测机制

截断检测在 `_run_c_stage` 调用层执行（v4.7.0 从 `_call_llm_for_l1` 内部下沉），双重校验：

```
finish_reason == 'length'           → 触发降级
not response.strip().endswith('</core_change>') → 触发降级
```

任一触发 → 写 `_assemble_status=1` 待补全记录 → L-stage 异步重试（最多 3 次）。

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

### 当前上下文是否在注入

```python
from ca import session_manager
from pathlib import Path
db_path = Path.home() / '.hermes/profiles/tester/ca_cache/{session_id}.db'
engine = session_manager.get(session_id, str(db_path))
result = engine.assemble("测试", 32000)
ca_count = sum(1 for m in result
    if isinstance(m.get("content",""), str) and m["content"].startswith("[~/")):
print(f"{ca_count} CA summaries in assembled context")
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

**方法**：`engine.debug_token_budget(session_id="")` — 纯只读，不修改任何状态。
**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `context_length` | int | 总 Token 窗口上限（`CA_CONTEXT_LENGTH`） |
| `budget_max` | int | 可用预算上限（`context_length × 0.95`） |
| `used_tokens` | int | 当前累计 token 偏移 |
| `remaining` | int | 剩余可用预算 |
| `usage_pct` | float | 使用率百分比 |

## 测试接口清单

**测试总数**：354 条（活跃 263 条 + legacy 91 条）

### Fixtures（`tests/conftest.py`）

| Fixture | Scope | 签名 | 说明 |
|---------|-------|------|------|
| `hardware_info` | session | `() -> dict` | CPU/内存信息，用于测试报告 |
| `fd_checker` | function | `() -> FdWatcher` | 文件描述符泄漏检测，`.assert_no_leak(max_delta=10)` |
| `ca_engine` | function | `(tmp_path) -> ContextAssembler` | 标准引擎实例，自动 mock embedding+LLM，teardown 执行 `.destroy()` |
| `engine` | function | `(ca_engine) -> ContextAssembler` | `ca_engine` 别名，向后兼容 |
| `_mock_embed` | function (autouse) | `(ca_engine) -> None` | 自动 mock `EmbeddingClient.embed` → `[0.1]*768` |
| `_mock_llm` | function (autouse) | `() -> None` | 自动 mock `_call_llm_for_l1` → `('mock_response', 'stop')` |

### 活跃测试文件

| 文件 | 测试数 | 范围 |
|------|--------|------|
| `test_c.py` | 25 | 对话轮 C‑stage：摘要生成、OODA 解析、状态标记、截断检测（v4.7.0）、DB 写入格式验证、配置参数 |
| `test_a.py` | 20 | 对话轮 A‑stage：分层、双检索、RRF 融合、预算门控、截断保护、plan‑based 组装 |
| `test_v440.py` | 40 | 工具轮 C‑stage(12) + A‑stage(8) + L‑stage(9) + Config等(11) |
| `test_v460.py` | 53 | 话题分割(9 类)：ComputeTopicGroups / ComputeTurnPlanV2 / GradeTopicsByRadius / IsBgTurn / JaccardTokens / TopicRetriever / QueryEmbedding / TopicConfig / V460Integration |
| `test_config.py` | 11 | Config：环境变量解析、默认值、非法值回退、L1_TEMPERATURE/L1_MAX_TOKENS 热重载、边界校验 |
| `test_parse_v1.py` | 27 | parse_v1_markdown_xml 全场景(16) + L1 提示词验证(2) + TruncatedException(1) + JSON→Markdown(3) + 截断边界(5) |
| `test_store_adapter.py` | 10 | format_previous_summary_for_prompt 全场景（含历史状态推断） |
| `test_store.py` | 12 | Store 层：读写 turn、turn_plan、BM25 tokens、分区清理、WAL 模式 |
| `test_embedding.py` | 6 | Embedding 客户端：缓存、降级、fallback 不缓存 |
| `test_plugin.py` | 28 | 插件断路器(4) + 生命周期(6) + pre_llm_call(4) + post_llm_call(3) + register(1) + BreakerStateCleanup(10) |
| `test_circuit.py` | 7 | 断路器：失败计数、冷却恢复、状态持久化 |
| `test_health.py` | 5 | 健康检查：引擎状态、DB 连接、缓存快照 |
| `test_lifecycle.py` | 7 | 生命周期：reset、destroy、并发安全、接口完整性 |
| `test_degradation.py` | 2 | 降级：LLM 失败后的规则摘要 |
| `test_quality.py` | 7 | 质量评估（全部 stub/skip，需人工评审） |
| `test_system.py` | 3 | 端到端（全部 skip，需 Ollama 环境） |

### legacy 测试（留存备份，不在常规运行中）

| 文件 | 测试数 | 说明 |
|------|--------|------|
| `legacy/test_dedup.py` | 26 | 去重：7 个测试类覆盖 system 豁免、全指纹、时序、配置、性能、调试 |
| `legacy/test_c_stage.py` | 16 | C-stage 旧版降级/去重/LLM 超时 |
| `legacy/test_a_stage.py` | 11 | A-stage 旧版分层/检索/预算 |
| `legacy/test_smoke.py` | 19 | 冒烟测试：DB/解析/嵌入/BM25 |
| `legacy/test_supplement_v3_2.py` | 8 | 补充测试：截断/异步/并发 |
| `legacy/test_review_fixes.py` | 5 | 审查修复验证 |
| `legacy/test_performance.py` | 2 | 性能基线 |
| `legacy/test_ooda_parser.py` | 1 | OODA 解析成功率 |
| `legacy/test_l1_gen_speed.py` | — | L1 生成速度测试（手动） |
| `legacy/test_stress_real.py` | — | 真实压力测试（手动） |

### 测试数据源（`tests/testcases/`）

| 文件 | 格式 | 用途 |
|------|------|------|
| `ContextAssembler_testcases_v4.3.4.json` | `[{id, req, title, preconditions, steps, expected, ...}]` | 主测试用例定义，`generate_tests.py` 的输入源 |
| `ContextAssembler_testcases_v1.2.json` | 同上 | 旧版 v1.2 用例集 |
| `ContextAssembler_testcases_v1.3.json` | 同上 | 旧版 v1.3 用例集 |

### 测试生成器（`tests/generate_tests.py`）

| 入口 | 说明 |
|------|------|
| `main()` | CLI：`python generate_tests.py --json <path> --output-dir <dir>` |
| `get_batch(tc_id)` | 按测试用例 ID 路由到对应测试批次 |

### 测试数据目录

| 路径 | 内容 |
|------|------|
| `tests/data/dialogues/short_dialogues.json` | 短对话 3 条 |
| `tests/data/dialogues/medium_dialogues.json` | 中长度对话 |
| `tests/data/dialogues/long_dialogues.json` | 长对话（A‑stage 爬坡测试） |
| `tests/data/malformed_json/` | 120 个畸形 JSON 文件（容错测试） |
| `tests/data/reference_summaries/v1.0/` | 50 份 L1 摘要参考标准（质量评估） |

### 测试报告

| 文件 | 内容 |
|------|------|
| `tests/test_execution_report_v4.3.4.json` | v4.3.4 执行记录：通过/失败/跳过统计，失败用例根因分析 |
| `tests/docs/testplan.md` | 完整测试计划文档，含需求追溯矩阵 |
| `tests/docs/qa-bug-report.md` | QA 缺陷报告 |
| `tests/docs/test-report.md` | 测试执行报告 |
| `tests/docs/bugs/` | 逐个 Bug 分析文档（7 个） |

### 运行方式

```bash
# 运行全部活跃测试（需 ca_assembler 目录为 cwd）
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/test_c.py tests/test_a.py tests/test_v440.py tests/test_v460.py tests/test_config.py tests/test_parse_v1.py tests/test_store_adapter.py tests/test_store.py tests/test_embedding.py tests/test_plugin.py tests/test_circuit.py tests/test_health.py tests/test_lifecycle.py tests/test_degradation.py tests/test_quality.py tests/test_system.py -v -p no:cacheprovider -o "addopts="

# 单文件
python -m pytest tests/test_c.py -v -p no:cacheprovider -o "addopts="

# 按类/函数
python -m pytest tests/test_c.py -v -k 'test_tc_c_T1' -p no:cacheprovider -o "addopts="

# legacy 测试单独
python -m pytest tests/legacy/ -v -p no:cacheprovider -o "addopts="

# 全部测试（含 legacy）
python -m pytest tests/ tests/legacy/ -v -p no:cacheprovider -o "addopts="
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

1. **Plugin hooks** → `plugins.enabled` 中的 `ca_assembler`。通过 `register()` 注册的 5 个 hook 回调工作。
2. **Context engine** → `context.engine` 配置项。只从仓库 `plugins/context_engine/` 子目录加载引擎，与用户 profile 的 `plugins/ca_assembler/` 毫无关系。

**`context.engine` 设成什么、是否回退，都不影响 CA 插件的工作。**

## 与 source project 的差异

| 差异点                 | source 项目                       | 当前部署                              |
| ---------------------- | --------------------------------- | ------------------------------------- |
| 代码位置               | `~/projects/context-assembler/` | `plugins/ca_assembler/` 自包含副本  |
| `register()`         | 不存在                            | 已添加，注册 5 个 hooks               |
| `_state_file_path()` | `Path.home() / ".hermes"`       | `get_hermes_home()`（profile 感知） |
| `sys.path`           | 无特殊处理                        | 本地 `ca/` 子目录优先               |
| 激活方式               | `context.engine: ca_assembler`  | `plugins.enabled: [ca_assembler]`   |

## 脚本工具

| 脚本 | 说明 |
|------|------|
| `scripts/backfill_tool_summaries.py` | 历史工具摘要回填（修复存量数据） |
| `scripts/benchmark_l1.py` | L1 生成性能基准测试 |

## 相关文档

| 文档 | 路径 | 内容 |
|------|------|------|
| 技术方案 | `docs/technical-plan.md` | 完整架构设计（已同步至 v4.7.1） |
| 调试报告验证工作流 | `docs/ca-debug-report-fix-verification.md` | 修复验证流程、关键管线代码位置 |
| CA→Reasonix 迁移分析 | `docs/ca-to-reasonix-analysis.md` | 引擎迁移与适配分析 |
| 部署调试记录（2026-06-19） | `docs/ca-deploy-debug-20260619.md` | 上下文注入质量修复记录 |
| 自检报告 | `docs/ca-devtest-workflow-selfcheck-report.md` | DevTest 工作流自检报告 |
| 工具轮重构分析 | `docs/tool-turn-refactor/tool-turn-refactor-analysis.md` | 工具轮重构设计分析 |
| 工具轮重构需求 | `docs/tool-turn-refactor/tool-turn-refactor-req.md` | 工具轮重构需求规格 |
| 工具轮重构测试需求 | `docs/tool-turn-refactor/tool-turn-refactor-test-req.md` | 工具轮重构测试策略 |
| 话题拣选方案 | `docs/topic-based-picking-plan.md` | 话题拣选原始方案设计 |
| L1 白皮书（GM） | `docs/ca-l1-refactor/ca-l1-whitepaper-v1.2-gm.md` | 外部设计文档，PDD 架构、解析器、适配器设计 |
| L1 需求与方案 v2 | `docs/ca-l1-refactor/ca-l1-refactor-requirements-v2.md` | 需求规格、总体设计、测试策略 |
| L1 实现方案 v2 | `docs/ca-l1-refactor/ca-l1-refactor-impl-plan-v2.md` | 实现策略、接口设计、13 步计划 |
| L1 测试方案 v2 | `docs/ca-l1-refactor/ca-l1-refactor-test-plan-v2.md` | 分层策略、Mock 策略 |
| 交叉评审（测试线） | `docs/cross-review-test-perspective.md` | 可测试性视角：10 项发现 |
| 交叉评审（开发线） | `docs/cross-review-dev-perspective.md` | 需求覆盖视角：7 项发现 |
| L0 摘要质量观察 | `ca-ctx-inspect/references/l0-summary-quality-observations.md` | 对话轮 L0 摘要质量实测 |
| system_overhead 分析 | `docs/system-overhead-measurement-analysis.md` | 测量代码移除分析 |
| CA v5 试验计划 | `docs/ca-v5-test-plan.md` | 缓存友好改进 + 参数自动调优试验方案 |
| ToolSummarizer 结构改进 | `docs/tool-summary-improvements.md` | 10 handlers 清单 + 待改进/已排除 |
