# CA (ContextAssembler) 插件 — 部署实现说明 (v4.4.0)

> 当前文档描述 **tester profile 部署实例**的接口、数据流、运行时状态。
> 完整设计文档见同目录 `technical-plan.md`。
> 核心区别：CA 是 **plugin**（靠 `plugins.enabled` 的 hook 机制运行），不是 context engine。

---

## 1. 部署快照

| 项 | 值 |
|---|---|
| 版本 | v4.4.0（自包含独立副本） |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/` |
| 核心引擎 | `ca/` 子目录 |
| 启用方式 | `plugins.enabled: [ca_assembler, ...]` |
| 入口文件 | `__init__.py`（插件适配层）+ `ca/__init__.py`（核心引擎） |
| 源项目 | `~/projects/context-assembler/` |

---

## 2. 注册接口

`register(ctx)` 注册 5 个 Hermes 生命周期 hooks：

```python
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call)
    ctx.register_hook("post_llm_call",    _on_post_llm_call)
```

---

## 3. Hook 分发函数

### 3.1 `_on_session_start` — 引擎初始化

```python
def _on_session_start(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（必含）

行为：
1. 创建 `CAContextAssemblerPlugin` 实例
2. 调用 `plugin.on_session_start(session_id)`
3. 注册到模块级 `_engines` 字典（`session_id → plugin`）

内部 `on_session_start()` 流程：
- 检查断路器 `is_available()` → 3 次失败则 1 小时冷却
- 从 `hermes_constants.get_hermes_home()` 获取 profile 基路径
- DB 路径：`{hermes_home}/ca_cache/{session_id}.db`
- 通过 `session_manager.get(session_id, db_path)` 创建引擎
- 成功后日志：`"CA plugin started for session {session_id}"`

### 3.2 `_on_session_end` — 资源清理

```python
def _on_session_end(**kwargs: Any) -> None:
```

**kwargs**: `session_id`

行为：
- 从 `_engines` 移除实例
- 调用 `plugin.on_session_end()` → `engine.wait_for_pending()` + `session_manager.remove()`

### 3.3 `_on_session_reset` — 重置

```python
def _on_session_reset(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（可选，无 session_id 时重置全部）

行为：
- 从 `_engines` 移除（单 session 或全部）
- 调用 `plugin.on_session_reset()` → `engine.reset()` + 清除断路器状态

### 3.4 `_on_pre_llm_call` — 上下文注入（A-stage）

```python
def _on_pre_llm_call(**kwargs: Any) -> Optional[str]:
```

**kwargs**:
| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | str | 当前会话 |
| `user_message` | str | 用户输入文本 |
| `context_length` | int | 上下文 Token 窗口上限（默认 `Config.CONTEXT_LENGTH`） |

**返回值**: `Optional[str]`

- 引擎不可用或出错 → 返回 `None`（不注入）
- 调用 `engine.assemble(user_message, context_length)` 完整管线
- 从返回的完整消息列表中提取 `[~/N]` 标记（CA 摘要注入）
- 多条摘要用 `\n\n` 拼接返回
- Hermes 会将返回文本注入到 user message 中

**返回格式示例**：
```
[~/1] CA插件运行正常钩子路径完整
[~/1/3] search_files: 无返回数据
[~/1/5] terminal: 配置项 `context.engine` 为 compressor
```

### 3.5 `_on_post_llm_call` — 数据积累（C-stage）

```python
def _on_post_llm_call(**kwargs: Any) -> None:
```

**kwargs**:
| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | str | 当前会话 |
| `user_message` | str | 用户输入文本 |
| `assistant_response` | str | LLM 的文本回复 |
| `conversation_history` | list | 本轮完整消息历史（含 tool 结果） |

行为：
- 异步调用 `engine.process_turn_async(user_message, assistant_response, history_copy)`
- `conversation_history` 传副本（列表拷贝），避免竞态

---

## 4. 三阶段数据流

```
 用户输入
    │
    ▼
┌─────────────────────────────────────────┐
│ A-stage (pre_llm_call) · 同步           │
│                                         │
│ assemble(user_message, context_length)  │
│  → 从 turn_cache 重建消息历史           │
│  → 获取 AssemblyCache 快照              │
│  → Head/Middle/Tail 分层                │
│  → BM25+向量双路检索 + RRF             │
│  → 动态 Token 预算闸门                  │
│  → 拣选升级候选（对话轮 > 工具轮）      │
│  → 组装最终消息列表                     │
│  → 返回 [~/N] 摘要 → 注入 user message │
└──────────┬──────────────────────────────┘
           │
           ▼
     LLM 处理（对话 + 工具调用）
           │
           ▼
┌─────────────────────────────────────────┐
│ C-stage (post_llm_call) · 异步          │
│                                         │
│ process_turn_async(user_message,        │
│   assistant_response, history)          │
│  → LLM 生成 OODA 摘要（对话轮）         │
│  → ToolSummarizer 规则引擎（工具轮）    │
│  → 嵌入 L1/L0                           │
│  → 写入 SQLite（turn_cache）            │
│  → 增量更新 AssemblyCache               │
│  → 触发 L-stage                         │
└──────────┬──────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│ L-stage (守护线程) · 异步补全           │
│                                         │
│ 扫描 _assemble_status=1 的记录          │
│ 用 l2_text 重新生成摘要                 │
│ 3 次失败 → 永久跳过 (status=2)          │
│ C-stage 触发 + 定时 60s 自检            │
└─────────────────────────────────────────┘
```

---

## 5. 存储结构

### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：
```
~/.hermes/profiles/tester/ca_cache/{session_id}.db
```

### turn_cache 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | TEXT | 会话 ID |
| `turn_index` | INTEGER | 对话轮次（从 1 开始） |
| `tool_sub_index` | INTEGER | 工具轮次（0=对话轮, 1+=工具轮） |
| `turn_type` | TEXT | `dialogue` 或 `tool` |
| `l2_text` | TEXT | 原始消息 JSON（递增快照） |
| `l1_text` | TEXT | 结构化摘要 JSON |
| `l0_text` | TEXT | 单行摘要（截断至 100 字符） |
| `l0_embedding` | BLOB | L0 嵌入向量（4096 字节） |
| `l1_embedding` | BLOB | L1 嵌入向量（4096 字节） |
| `bm25_tokens` | TEXT | BM25 分词 |
| `token_offset` | INTEGER | 累计 Token 偏移 |
| `_assemble_status` | INTEGER | 0=成功, 1=降级, 2=永久跳过 |
| `backfill_attempts` | INTEGER | L-stage 尝试次数 |
| `created_at` | TEXT | 创建时间戳 |

### 存储特性

- **L2 是递增快照**：每条 subturn 存一个独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0+ l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache

---

## 6. 断路器

| 项目 | 值 |
|---|---|
| 文件路径 | `{hermes_home}/.ca_assembler_state_{PID}.json` |
| 状态格式 | `{"failures": N, "retry_after": null \| ISO_timestamp}` |
| 触发阈值 | 连续 3 次失败 |
| 冷却时间 | 1 小时 |
| 恢复 | 1 次成功 reset 或超时后自动清除 |

---

## 7. 关键环境变量

| 变量 | 当前值 | 说明 |
|------|--------|------|
| `CA_EMBED_BACKEND` | `ollama` | 嵌入后端 |
| `CA_EMBED_MODEL` | `qwen3-embedding:0.6b` | 嵌入模型 |
| `CA_EMBED_ENDPOINT` | `http://localhost:11439` | 嵌入服务端点 |
| `CA_LLM_MODEL` | `qwen3-4b-instruct` | L1 摘要生成模型 |
| `CA_LLM_ENDPOINT` | `http://localhost:11440` | LLM 服务端点 |
| `CA_EMBED_TIMEOUT` | `30` | 嵌入超时（秒） |
| `CA_LLM_TIMEOUT` | `180` | LLM 超时（秒） |

---

## 8. 运行时验证

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
# 主动调用 assemble 看产出
from ca import session_manager
engine = session_manager.get(session_id, str(db_path))
result = engine.assemble("测试", 32000)
ca_count = sum(1 for m in result
    if isinstance(m.get("content",""), str) and m["content"].startswith("[~/"))
print(f"{ca_count} CA summaries in assembled context")
```

---

## 9. 与 source project 的差异

| 差异点 | source 项目 | 当前部署 |
|--------|-----------|---------|
| 代码位置 | `~/projects/context-assembler/` | `plugins/ca_assembler/` 自包含副本 |
| `register()` | 不存在 | 已添加，注册 5 个 hooks |
| `_state_file_path()` | `Path.home() / ".hermes"` | `get_hermes_home()`（profile 感知） |
| `sys.path` | 无特殊处理 | 本地 `ca/` 子目录优先 |
| 激活方式 | `context.engine: ca_assembler` | `plugins.enabled: [ca_assembler]` |
