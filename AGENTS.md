# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项       | 值                                                                        |
| -------- | ------------------------------------------------------------------------- |
| 版本     | v4.7.1 |
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

变更历史统一记录在 `docs/changelog.md`（项目根目录）。调试/修复详情见 `docs/ca-debug-report-fix-verification.md`。

### v4.7.0 — L1 摘要系统重构（2026-06-15）

全面吸收白皮书 v1.2 Gold Master 设计（`docs/ca-l1-whitepaper-v1.2-gm.md`），重构 L1 摘要生成链路：

- **ca/post_process.py**（新增）：`parse_v1_markdown_xml` 防御性解析器 + `_safe_truncate` 智能截断 + `_json_to_v1_markdown` 格式转换
- **ca/prompts.py**：替换为"研发对话意图分析器"人设，4 类 Markdown + `<core_change>` XML 标签
- **ca/__init__.py**：新增 `L1TruncatedException`；`_call_llm_for_l1` 返回 `Tuple[str,str]`；截断检测下沉至 `_run_c_stage`；独立 `temperature`/`max_tokens`
- **ca/config.py**：新增 `L1_TEMPERATURE`(0.3) + `L1_MAX_TOKENS`(800) + validate + reload
- **ca/ooda_parser.py**：`TITLE_ALIASES` 扩展 4 类中文别名
- **ca/store.py**：`format_previous_summary_for_prompt` 历史适配器
- **ca/lstage.py**：同步新签名 + 截断检测
- **ca/stats.py**：新增 4 个统计字段（truncated_fallback / parse_fallback_count / skipped_empty / l1_latency_ms）

**设计哲学**：PDD (Prompt-Driven Development) — 模型负责语义理解和 Markdown 续写，Python 代码负责截断检测、格式清洗、边界校验、新旧数据兼容。截断检测（`finish_reason=='length'` + `endswith('</core_change>')`）下沉至 `_run_c_stage` 调用层，使 mock 路径可覆盖。

### v4.7.1 — L1 状态感知链路集成（2026-06-16）

基于 v1.5.1 Final 文档（`docs/ca-l1-whitepaper-v1.5.1-final.md`），新增状态前缀提取与结构化透传，对抗小模型"完成时态"幻觉：

- **ca/post_process.py**：新增 `ItemState` 枚举（DONE/PLANNED/DISCUSSING/UNKNOWN）+ `STATE_PREFIX_REGEX`（含模块级 fail-fast assert）+ `_normalize_state()`（作用域隔离归一化）+ `parse_core_change_state()`（结构化透传）；标题统一"决策与共识"→"决策与方案"；`parse_v1_markdown_xml` 返回三元组 `(l1_dict, l0_text, core_state)`
- **ca/prompts.py**：标题统一 + `<core_change>` 增加 `【已实施】/【计划】/【探讨】` 状态前缀引导
- **ca/ooda_parser.py**：`TITLE_ALIASES` 增加"决策与方案"
- **ca/store.py**：新增 `_infer_legacy_state()`（文本自检推断历史状态）+ `format_previous_summary_for_prompt` 适配
- **ca/__init__.py**：适配新签名；状态注入 `l1_dict["_state"]`，零 schema 变更
- **所有测试文件**：标题统一 + 状态前缀场景

**设计哲学**：状态前缀使用 `【】` 中文直角引号避免 Markdown/JSON/XML 语法冲突；结构化字段 `_state` 仅存枚举值（`"done"`/`"planned"`/`"discussing"`），供下游代码层消费。

## 存储结构
### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：`~/.hermes/profiles/tester/ca_cache/{session_id}.db`

### turn_cache 表

| 字段                  | 类型    | 说明                            |
| --------------------- | ------- | ------------------------------- |
| `session_id`        | TEXT    | 会话 ID                         |
| `turn_index`        | INTEGER | 对话轮次（从 1 开始）           |
| `tool_sub_index`    | INTEGER | 工具轮次（0=对话轮, 1+=工具轮） |
| `turn_type`         | TEXT    | `dialogue` 或 `tool`        |
| `l2_text`           | TEXT    | 原始消息 JSON（递增快照）       |
| `l1_text`           | TEXT    | 结构化摘要 JSON                 |
| `l0_text`           | TEXT    | 单行摘要（截断至 100 字符）     |
| `l0_embedding`      | BLOB    | L0 嵌入向量（4096 字节）        |
| `l1_embedding`      | BLOB    | L1 嵌入向量（4096 字节）        |
| `bm25_tokens`       | TEXT    | BM25 分词                       |
| `token_offset`      | INTEGER | 累计 Token 偏移                 |
| `_assemble_status`  | INTEGER | 0=成功, 1=降级, 2=永久跳过      |
| `backfill_attempts` | INTEGER | L-stage 尝试次数                |
| `query_embedding`   | BLOB    | 用户消息嵌入向量（v4.6.0）       |
| `created_at`        | TEXT    | 创建时间戳                      |

### 存储特性

- **L2 是递增快照**：每条 subturn 存独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0 + l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache

## 关键环境变量

| 变量                  | 当前值                     | 说明            |
| --------------------- | -------------------------- | --------------- |
| `CA_EMBED_BACKEND`  | `ollama`                 | 嵌入后端        |
| `CA_EMBED_MODEL`    | `qwen3-embedding:0.6b`   | 嵌入模型        |
| `CA_EMBED_ENDPOINT` | `http://localhost:11439` | 嵌入服务端点    |
| `CA_LLM_MODEL`      | `qwen3-4b-instruct`      | L1 摘要生成模型 |
| `CA_LLM_ENDPOINT`   | `http://localhost:11440` | LLM 服务端点    |
|| `CA_EMBED_TIMEOUT`  | 30                         | 嵌入超时（秒）  |
|| `CA_LLM_TIMEOUT`    | 180                        | LLM 超时（秒）  |
|| `CA_CONTEXT_LENGTH` | 200000                     | 上下文 Token 预算上限（原 50000） |

### L1 摘要生成配置（v4.7.0）

| 变量                       | 默认值 | 说明                              |
| -------------------------- | ------ | --------------------------------- |
| `CA_L1_TEMPERATURE`      | 0.3    | L1 摘要生成温度（高确定性）         |
| `CA_L1_MAX_TOKENS`       | 800    | L1 摘要生成最大 Token 数            |

### 话题拣选配置（v4.6.0）

| 变量                       | 默认值   | 说明                          |
| -------------------------- | -------- | ----------------------------- |
| `CA_TOPIC_JACCARD_ENTRY` | 0.03     | 话题分割首次合并 Jaccard 阈值 |
| `CA_TOPIC_JACCARD_CHAIN` | 0.04     | 话题分割链内扩展 Jaccard 阈值 |
| `CA_TOPIC_RADIUS_WEIGHT` | 2.0      | 半径公式中最近邻距离的权重系数 |
| `CA_TOPIC_MAX_UPGRADE`   | 10       | 检索升级最大 topic 数         |
| `CA_TOPIC_BG_LEVEL`      | `L0`   | BG 话题固定级别               |

## L1 摘要生成架构（v4.7.0 / v4.7.1）

### 新增模块

| 模块/文件 | 类型 | 职责 |
|-----------|------|------|
| `ca/post_process.py` | **新增** | 防御性解析器：`parse_v1_markdown_xml`(主入口)、`_safe_truncate`(智能截断)、`_json_to_v1_markdown`(格式转换)、预编译正则、常量 |
| `ca/__init__.py :: L1TruncatedException` | **新增** | 截断异常类，携带 `response_text` 供降级回读 |

### 修改模块

| 模块 | 变更 |
|------|------|
| `ca/prompts.py` | `L1_GENERATION_PROMPT` 替换为"研发对话意图分析器"人设，4 类 Markdown + `<core_change>` XML 标签 |
| `ca/config.py` | 新增 `L1_TEMPERATURE`(0.3) + `L1_MAX_TOKENS`(800) |
| `ca/ooda_parser.py` | `TITLE_ALIASES` 扩展 4 类中文别名 |
| `ca/store.py` | 新增 `format_previous_summary_for_prompt` 适配器 |
| `ca/__init__.py` | `_call_llm_for_l1` 返回 `Tuple[str,str]`；截断检测下沉至 `_run_c_stage`；独立 `temperature`/`max_tokens` |
| `ca/lstage.py` | `_backfill_dialogue` 同步新签名 + 截断检测 |
| `ca/stats.py` | 新增 4 统计字段 |

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

### Token 水位查询（v4.6.0）

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

### Fixtures（`tests/conftest.py`）

| Fixture | Scope | 签名 | 说明 |
|---------|-------|------|------|
| `hardware_info` | session | `() -> dict` | CPU/内存信息，用于测试报告 |
| `fd_checker` | function | `() -> FdWatcher` | 文件描述符泄漏检测，`.assert_no_leak(max_delta=10)` |
| `ca_engine` | function | `(tmp_path) -> ContextAssembler` | 标准引擎实例，自动 mock embedding+LLM，teardown 执行 `.destroy()` |
| `engine` | function | `(ca_engine) -> ContextAssembler` | `ca_engine` 别名，向后兼容 |
| `_mock_embed` | function (autouse) | `(ca_engine) -> None` | 自动 mock `EmbeddingClient.embed` → `[0.1]*768` |
| `_mock_llm` | function (autouse) | `() -> None` | 自动 mock `_call_llm_for_l1` → `('mock_response', 'stop')`（v4.7.0 新签名 `Tuple[str,str]`） |

### 测试文件与测试类

| 文件 | 测试类 | 测试数 | 范围 |
|------|--------|--------|------|
| `test_c.py` | （函数级） | 19 | 对话轮 C‑stage：摘要生成、OODA 解析、状态标记、**截断检测（v4.7.0）**、**DB 写入格式验证** |
| `test_a.py` | （函数级） | 17 | 对话轮 A‑stage：分层、检索、升级、尾部保护 |
| `test_v440.py` | `TestToolTurnCStage` | 12 | 工具轮 C‑stage：多工具调用、字段优先级、L0 截断、预升级（1 个已替换为 pre_upgrade_removed） |
| | `TestToolTurnAStage` | 8 | 工具轮 A‑stage：尾部保护、topic_boost、升级过滤、预算 |
| | `TestLStageBackfill` | 9 | L‑stage：对话轮/工具轮补全、永久失败、周期扫描、**截断路径（v4.7.0）** |
| | `TestConfigAndOthers` | 11 | 配置热重载、去重非法值、Store 新列、并发销毁、快照隔离、性能 |
| `test_config.py` | （函数级） | 11 | Config：环境变量解析、默认值、非法值回退、**L1_TEMPERATURE/L1_MAX_TOKENS（v4.7.0）** |
| `test_parse_v1.py` | `TestParseV1MarkdownXml` | 27 | **新增（v4.7.0/v4.7.1）**：`parse_v1_markdown_xml` 全场景测试 + 状态提取测试 |
| | `TestL1GenerationPrompt` | 2 | **新增（v4.7.0）**：REQ-1 prompt 内容验证（含新旧特征检测） |
| | `TestL1TruncatedException` | 1 | **新增（v4.7.0）**：异常类继承链和属性验证 |
| | `TestJsonToV1Markdown` | 3 | **新增（v4.7.0）**：格式转换、缺失 key、特殊字符 |
| | `TestSafeTruncateBoundary` | 5 | **新增（v4.7.0）**：边界值测试（0/1/全标点/全空格/负值） |
| `test_store_adapter.py` | `TestFormatPreviousSummary` | 11 | **新增（v4.7.0/v4.7.1）**：`format_previous_summary_for_prompt` 全场景 + 历史状态推断 |
| `test_store.py` | （函数级） | 10 | Store 层：读写 turn、turn_plan、BM25 tokens、分区清理 |
| `test_circuit.py` | （函数级） | 7 | 断路器：失败计数、冷却恢复、状态持久化 |
| `test_health.py` | （函数级） | 5 | 健康检查：引擎状态、DB 连接、缓存快照 |
| `test_lifecycle.py` | （函数级） | 7 | 生命周期：reset、destroy、并发安全、接口完整性 |
| `test_degradation.py` | （函数级） | 2 | 降级：LLM 失败后的规则摘要 |
| `test_quality.py` | （函数级） | 7 | 质量评估（全部 stub/skip，需人工评审） |
| `test_system.py` | （函数级） | 3 | 端到端（全部 skip，需 Ollama 环境） |
| `tests/legacy/test_dedup.py` | `TestSystemMessageExemption` | 2 | 去重：system 消息豁免 |
| | `TestNonSystemDedup` | 6 | 去重：全指纹、角色含、字典 content、不同角色保留 |
| | `TestOrderPreservation` | 3 | 去重：时序保持、多重复组、system 交错 |
| | `TestConfigurableDedup` | 9 | 去重：环境变量启停、非法值、禁用时跳过 |
| | `TestDedupPerformance` | 1 | 去重：200 条 < 2ms 性能 |
| | `TestDebugLogging` | 1 | 去重：CA_DEBUG 日志输出 |
| | `TestEdgeCases` | 4 | 去重：空列表、单条、全唯一、全重复 |
| `tests/test_plugin.py` | `TestIsAvailable` | 4 | 插件：断路器初始/3次失败/2次失败/恢复 |
| | `TestLifecycle` | 6 | 插件：init/session_start/end/reset/空引擎 |
| | `TestPreLlmCall` | 4 | 插件：pre_llm_call 引擎错误/CA标记提取/失败降级 |
| | `TestPostLlmCall` | 3 | 插件：post_llm_call 调用process/错误跳过/空跳过 |
| | `TestRegister` | 1 | 插件：register() 注册 5 个钩子 |

### 测试用例数据源（`tests/testcases/`）

| 文件 | 格式 | 用途 |
|------|------|------|
| `ContextAssembler_testcases_v4.3.4.json` | `[{id, req, title, preconditions, steps, expected, ...}]` | 主测试用例定义，`generate_tests.py` 的输入源 |
| `ContextAssembler_testcases_v1.2.json` | 同上 | 旧版 v1.2 用例集 |
| `ContextAssembler_testcases_v1.3.json` | 同上 | 旧版 v1.3 用例集 |

### 测试生成器（`tests/generate_tests.py`）

| 入口 | 说明 |
|------|------|
| `main()` | CLI：`python generate_tests.py --json <path> --output-dir <dir>` |
| `get_batch(tc_id)` | 按测试用例 ID 路由到对应测试批次的生成函数 |
| `_gen_c_stage_body` | C‑stage 测试主体生成 |
| `_gen_a_stage_body` | A‑stage 测试主体生成 |
| `_gen_store_body` | Store 层测试主体生成 |
| `_gen_embedding_body` | Embedding 测试主体生成 |
| `_gen_config_body` | Config 测试主体生成 |
| `_gen_health_body` | 健康检查测试主体生成 |
| `_gen_circuit_body` | 断路器测试主体生成 |
| `_gen_lifecycle_body` | 生命周期测试主体生成 |
| `_gen_quality_body` | 质量评估测试主体生成 |

### 测试数据目录

| 路径 | 内容 |
|------|------|
| `tests/data/dialogues/short_dialogues.json` | 短对话 3 条 |
| `tests/data/dialogues/medium_dialogues.json` | 中长度对话 |
| `tests/data/dialogues/long_dialogues.json` | 长对话（A‑stage 爬坡测试） |
| `tests/data/malformed_json/` | 120 个畸形 JSON 文件（容错测试） |
| `tests/data/reference_summaries/` | L1 摘要参考标准（质量评估） |

### 测试报告

| 文件 | 内容 |
|------|------|
| `tests/test_execution_report_v4.3.4.json` | v4.3.4 执行记录：通过/失败/跳过统计，失败用例根因分析 |
| `tests/docs/testplan.md` | 完整测试计划文档（67 KB），含需求追溯矩阵 |

### 运行方式

```bash
# 运行全部测试（需 ca_assembler 目录为 cwd）
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/ -v

# 按文件
python -m pytest tests/legacy/test_dedup.py -v

# 按类/函数
python -m pytest tests/legacy/test_dedup.py -v -k 'TestOrderPreservation'

# 排除已知环境依赖失败的测试
python -m pytest tests/ -v --ignore=tests/test_system.py

# 生成测试文件（从 JSON 用例定义）
python tests/generate_tests.py --json tests/testcases/ContextAssembler_testcases_v4.3.4.json --output-dir tests/
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

## 相关文档

| 文档                    | 路径                                                             | 内容                                       |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------ |
| 技术方案                | `docs/technical-plan.md`                                       | 完整设计文档：架构、数据结构、C/A/L 三阶段 |
| 调试报告验证工作流      | `docs/ca-debug-report-fix-verification.md`                     | 修复验证流程、关键管线代码位置             |
| ToolSummarizer 结构改进 | `docs/tool-summary-improvements.md`                            | 10 handlers 清单 + 待改进/已排除           |
| L0 摘要质量观察         | `ca-ctx-inspect/references/l0-summary-quality-observations.md` | 对话轮 L0 摘要质量实测                     |
| system_overhead 分析    | `docs/system-overhead-measurement-analysis.md`                 | 测量代码移除分析                           |
| CA v5 试验计划          | `docs/ca-v5-test-plan.md`                                      | 缓存友好改进 + 参数自动调优试验方案        |

### v4.7.0 相关文档

| 文档                    | 路径                                                             | 内容                                       |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------ |
| L1 白皮书（GM）         | `docs/ca-l1-whitepaper-v1.2-gm.md`                             | 外部设计文档，PDD 架构、解析器、适配器设计 |
| 需求与方案 v2           | `docs/ca-l1-refactor-requirements-v2.md`                       | 需求规格、总体设计、测试策略、实施步骤     |
| 实现方案 v2             | `docs/ca-l1-refactor-impl-plan-v2.md`                          | 实现策略、接口设计、关键实现细节、13 步计划 |
| 测试方案 v2             | `docs/ca-l1-refactor-test-plan-v2.md`                          | 分层策略、49 测试场景、Mock 策略            |
| 交叉评审（测试线视角）  | `docs/cross-review-test-perspective.md`                         | 可测试性视角：10 项发现（2 高严重度）      |
| 交叉评审（开发线视角）  | `docs/cross-review-dev-perspective.md`                         | 需求覆盖视角：7 项发现（2 高严重度）       |
| 技术方案初稿 v1         | `docs/ca-l1-refactor-technical-proposal-v1.md`                 | 存档草案（v4.6.0 基线，未走完评审流程）    |
