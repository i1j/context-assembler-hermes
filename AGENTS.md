# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项       | 值                                                                        |
| -------- | ------------------------------------------------------------------------- |
| 版本     | v4.5.1 |
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

## 已知修复与改进（相对于源项目）

### 初始部署修复（2026-06-04）

| 修复                                            | 说明                                                                                                                                             |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，已改为 profile 感知。断路器状态文件写入 `~/.hermes/profiles/tester/`                                   |
| `sys.path` 保障本地 `ca/` 优先              | 插件目录加入 `sys.path.insert(0, ...)`，确保导入走本地 `ca/` 子目录                                                                          |
| 添加 `register(ctx)` 函数                     | 原插件无 `register()` 函数，所有 hook 回调永不注册                                                                                             |
| Hook 签名适配                                   | 所有 hook 回调改为 `**kwargs: Any` 模式。`on_session_start` 移除对 `kwargs["hermes_home"]` 的依赖。`pre_llm_call` 返回 `Optional[str]` |
| 模块级引擎注册表                                | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁，支持多 session 并发                                                             |

### Bug 修复（2026-06-05）

| 修复                           | 说明                                                                                                                                                                        | 代码位置                           |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| C-stage Executor shutdown 竞态 | `_shutdown_cache_executor()` 没设 `_destroyed`。修复：`destroy()` 和 `reset()` 改用 `cache.destroy()`（同时设 `_destroyed` + cancel retry + shutdown executor） | `ca/__init__.py` 第 245, 1148 行 |
| Head 保护区方向错误            | `_compute_layers_v2()` 取 `sorted(...)[-HEAD_AUTO_L1_COUNT:]` 取了末尾 3 轮。修复：`[-N:]` → `[:N]`                                                                | `ca/__init__.py` 第 662 行       |

### ToolSummarizer 结构化摘要 10 handlers（2026-06-06）

`ca/tool_summarizer.py` 内按工具名分派 handler，替代通用字段提取。

| #  | Handler                     | 行号 | L0 摘要示例                                                                                                   |
| -- | --------------------------- | ---- | ------------------------------------------------------------------------------------------------------------- |
| #  | Handler                     | 行号 | L0 摘要示例                                                                                                   |
| -- | --------------------------- | ---- | ------------------------------------------------------------------------------------------------------------- |
| 1  | `_summarize_terminal`     | 129  | `t:grep -i "CA plugin" → 1 lines`（关键输出行内联），失败→ `[ERROR] t:cmd → Traceback...`；内建 pytest 检测 → `pytest: 8 passed, 2 skipped — 0.44s` |
| 2  | `_summarize_execute_code` | 228  | `exc:from hermes_tools... → import os`（关键输出行内联），带 `tool_label="exc"` 参数              |
| 3  | `_summarize_write_file`   | 235  | `write_file: /home/i1j/test.txt`（只保留文件路径）                                                          |
| 4  | `_summarize_patch`        | 266  | `patch: /home/i1j/tool_summarizer.py` + replace_all 标记                                                    |
| 5  | `_summarize_read_file`    | 302  | `read_file: …/ca_assembler/test.txt (42 lines)`（路径前缀压缩 + 行数）                                  |
| 6  | `_summarize_search_files` | 342  | `search_files: *.py → 0 hits` 或 `66 matches [ca:30, tests:25, docs:11]`                                 |
| 7  | `_summarize_skills_list`  | 404  | `skills_list: 3 skills (tester-workflow, ...)`                                                              |
| 8  | `_summarize_skill_view`   | 444  | `skill_view: tester-workflow — 12 lines`                                                                   |
| 9  | `_summarize_skill_manage` | 496  | `skill_manage: patch tester-workflow (error)`（提取 action/name/file_path）                                 |
| 10 | `_summarize_memory`       | 547  | `memory: replace memory (error)`（action/target/old_text + content 80 字符预览）                            |

分发机制：`summarize()` → `getattr(self, f"_summarize_{sanitized}", None)` → handler。工具名中 `.`/`-` 自动映射为 `_`。handler 失败时 `try/except` 回退到通用字段提取。

通用增强：

- **terminal 错误标记**：`_ERROR_RE` 正则（Traceback/Error:/Exception/...）扫描输出，匹配时前缀 `[ERROR]`
- **terminal/exec L0 行内联**：有输出时优先展示首个关键行，格式 `t:cmd_part → key_line`
- **search_files 目录分组**：total_count > 3 时 `Counter` 按父目录名聚合，取前 4 组

### Bug 修复（2026-06-06）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **arguments JSON string → dict 适配** | reduce 所有 10 个 structured handler 因 `args.get()` 在 JSON string 上调用时全部崩溃回退到通用逻辑。根因：OpenAI API 标准中 `function.arguments` 是 JSON string，但 handler 假设已是 parsed dict。修复：在 `summarize()` 入口（L603-615）做一次 JSON parse，全部 handler 同时生效 | `ca/tool_summarizer.py:603-615` |
| **旧数据全量回填** | 部署至今所有 DB 中 6,762 条工具轮 L0 存的是 raw JSON 格式（`{"total_count": N}`）。`scripts/backfill_tool_summaries.py` 离线重跑 summarizer，已全部升级为结构化摘要 | 共修复 174 个 DB |

### Bug 修复（2026-07-01）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **断路器 stale 文件清理** | `_state_file_path()` 每次 `_write_state()` 前清理不再运行的 PID 残留状态文件，防止无限堆积。添加 `re.compile` 模式匹配 + `/proc/{pid}` POSIX 检查 + 跳过当前 PID。 | `ca/__init__.py` → `_cleanup_stale_state_files()` + `_pid_exists()` |
| **terminal/exec L0 信息密度** | terminal handler 从 `cmd_short[:60] (N lines)` 改为 `t:cmd_part[:30] → key_lines[0][:50]`，有输出时优先展示首个关键行。execute_code 带 `tool_label="exc"`，取代旧 `l0.replace()`。 | `ca/tool_summarizer.py` L225-238 |
| **read_file L0 路径压缩** | 从纯路径改为 `…{parent}/{fname} (N lines)`，压缩路径前缀、添加行数。 | `ca/tool_summarizer.py` L344-348 |

**验证日志**：日志中有 ~50 条 `WARNING` 记录（`Tool-specific summarizer for 'search_files' failed: 'str' object has no attribute 'get', falling back`），所有 handler 命中。修复后 DB 中 0 条此格式 WARNING。

### 遗留问题状态（2026-07-01）

| 问题 | 说明 | 优先级 |
|------|------|--------|
| **空摘要 BM25 排除** | `result_summary="无返回数据"` 的工具轮仍进入 BM25 检索/升级候选。**但 DB 实测 0 条工具轮走 `retrieved` 路径**，当前运行负载下无实际风险 | 低 |
| **系统消息降级**（非 background_review 路径） | 仅 `background_review` ContextVar 修复了。其他系统触发消息仍可能被 LLM 误判降级。DB 实测 1411 行全部 status=0，未实际触发 | 低 |
| **去重标注缺少计数** | `(同[~/N])` 只标注引用目标，不显示该模式跨轮出现了几次（如「连续15轮 read_file: 无返回数据」无法通过标记知道重复次数） | 低 |

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
| `CA_EMBED_TIMEOUT`  | 30                         | 嵌入超时（秒）  |
| `CA_LLM_TIMEOUT`    | 180                        | LLM 超时（秒）  |

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
    if isinstance(m.get("content",""), str) and m["content"].startswith("[~/"))
print(f"{ca_count} CA summaries in assembled context")
```

### Plan-based 消息组装（v4.5.0 → v4.5.1）

| 改进 | 说明 | 代码位置 |
|------|------|---------|
| `_compute_turn_plan()` | 统一决策逻辑，返回 `List[TurnPlanEntry]` | `ca/__init__.py` |
| `_build_messages_from_plan()` | 按 plan entry 的 `target_level` 从 `store.read_turn_texts()` 读取对应 level 文本构建消息列表 | `ca/__init__.py` |
| `store.read_turn_texts()` | 返回 `(l2_text, l1_text, l0_text)` 三元组 | `ca/store.py` |

**核心改进**：
- **决策统一**：head/tail/middle/upgrades 判断逻辑只有一处（`_compute_turn_plan`），不再分散在两个方法中
- **对话轮级决策**：plan 以 turn 为单位做决策，消除旧 v4 的 per-message 不一致
- **plan 未覆盖消息处理**：当前轮用户消息等非缓存消息自动追加以保持完整性
- **v4.5.1 变更**：
  - 移除旧 `CA_PLAN_BUILD_ENABLED` 回退开关，plan-based 为唯一路径
  - 删除 `_build_final_messages_v4`、`_compute_and_store_turn_plan`、`_compute_layers_v2` 方法（净减 ~220 行）
  - 移除 head 自动提升机制（`HEAD_AUTO_L1_COUNT`），全部走拣选
  - 对话轮标签统一为 `[~/N/0]` 两位格式
  - 去重改为留最先+原位 `(同[~/N/0])`/`(同[~/N/m])` 标记

## 当前遗留状态（2026-06-06）

| 问题                                  | 说明                                                                         | 优先级 |
| ------------------------------------- | ---------------------------------------------------------------------------- | ------ |
| `_shutdown_cache_executor()` 死代码 | `ca/__init__.py` 第 254-262 行，全项目无调用方。低风险                     | 低     |
| 空摘要 BM25 排除                      | `result_summary="无返回数据"` 的工具轮仍进入检索/升级候选                  | 低     |
| 系统消息降级                          | `background_review` ContextVar 路径已修。其他系统触发消息仍可能被 LLM 误判 | 低     |
| bare `except:` 吞异常               | 在 `tests/conftest.py`，不影响被测代码                                     | 低     |

**已修复（v4.5.1）**：

| 问题 | 说明 |
|------|------|
| 旧方法 deprecated | `_build_final_messages_v4`、`_compute_and_store_turn_plan`、`_compute_layers_v2` 已删除。plan-based 为唯一路径。净减 ~220 行。 |
| 去重方向 | `_deduplicate_messages()` 改为留最先+原位 `(同[~/N/0])`/`(同[~/N/m])` 标记。首次出现位置不动 → 前缀稳定。 |
| Head 区移除 | `HEAD_AUTO_L1_COUNT` 已移除。所有对话轮平等走拣选（tail→L2 / upgrades+L1→L1 / middle→L0）。 |
| 对话轮标签格式 | `[~/N]` 改为 `[~/N/0]`，与工具轮 `[~/N/m]` 统一两位格式。 |

### 指纹去重标注（v4.5.1 — 留最先+原位指向标记）

| 改进 | 说明 |
|------|------|
| `_deduplicate_messages` 增强 | 去重策略改为 **留最先，原位指向标记**。同一指纹的去重组保留**首次出现**的完整消息，后续重复在原位替换为轻量指向标记 `(同[~/N/0])` / `(同[~/N/m])`。对话轮摘要标签统一为 `[~/N/0]` 两位格式。 |
| **缓存稳定性** | 首次出现位置永远不动 → 前缀不断裂。标记原位替入不影响已缓存前缀（重复位置本就要变化） |

**5 个历史 DB 实测（plan-based 路径）**：
| DB | 标记数 | 标记 token | 总 token | 占比 |
|----|--------|-----------|---------|------|
| 30 轮 | — | — | — | — |
| 15 轮 | — | — | — | — |
| 16 轮 | — | — | — | — |
| 44 轮 | — | — | — | — |
| 21 轮 | 1,150 | 10,247 | 471,089 | 2% |

**输出示例**：
```
[  1] (同[~/19/0])                          ← 指向首次出现的对话轮
[  2] (同[~/19/0])                           ← 同上
  ...
[1088] [~/19/0] 搜索: 失败                     ← 首次出现，完整内容
```

**无标签消息**（如 tail 区原始 tool_calls 消息，content 为空）不受影响——`ref_tag` 为空时不做标记，静默去重。

## 测试接口清单

### Fixtures（`tests/conftest.py`）

| Fixture | Scope | 签名 | 说明 |
|---------|-------|------|------|
| `hardware_info` | session | `() -> dict` | CPU/内存信息，用于测试报告 |
| `fd_checker` | function | `() -> FdWatcher` | 文件描述符泄漏检测，`.assert_no_leak(max_delta=10)` |
| `ca_engine` | function | `(tmp_path) -> ContextAssembler` | 标准引擎实例，自动 mock embedding+LLM，teardown 执行 `.destroy()` |
| `engine` | function | `(ca_engine) -> ContextAssembler` | `ca_engine` 别名，向后兼容 |
| `_mock_embed` | function (autouse) | `(ca_engine) -> None` | 自动 mock `EmbeddingClient.embed` → `[0.1]*768` |
| `_mock_llm` | function (autouse) | `() -> None` | 自动 mock `_call_llm_for_l1` → 固定 L1 文本 |

### 测试文件与测试类

| 文件 | 测试类 | 测试数 | 范围 |
|------|--------|--------|------|
| `test_c.py` | （函数级） | 14 | 对话轮 C‑stage：摘要生成、OODA 解析、状态标记 |
| `test_a.py` | （函数级） | 17 | 对话轮 A‑stage：分层、检索、升级、尾部保护 |
| `test_v440.py` | `TestToolTurnCStage` | 12 | 工具轮 C‑stage：多工具调用、字段优先级、L0 截断、预升级 |
| | `TestToolTurnAStage` | 8 | 工具轮 A‑stage：尾部保护、预升级 L1、升级过滤、预算 |
| | `TestLStageBackfill` | 7 | L‑stage：对话轮/工具轮补全、永久失败、周期扫描、超时 |
| | `TestConfigAndOthers` | 11 | 配置热重载、去重非法值、Store 新列、并发销毁、快照隔离、性能 |
| `test_store.py` | （函数级） | 10 | Store 层：读写 turn、turn_plan、BM25 tokens、分区清理 |
| `test_config.py` | （函数级） | 6 | Config：环境变量解析、默认值、非法值回退 |
| `test_embedding.py` | （函数级） | 5 | Embedding：向量计算、归一化、缓存 |
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
