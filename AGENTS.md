# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项 | 值 |
|---|---|
| 部署方式 | 自包含独立副本，调试不影响 sysadmin |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/` |
| 核心引擎 | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`） |
| 插件适配 | `plugins/ca_assembler/__init__.py`（入口 `CAContextAssemblerPlugin`） |
| 启用方式 | `plugins.enabled: [ca_assembler, ...]` |
| 源项目 | `~/projects/context-assembler/`（已分化，含独有修复） |

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

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | str | 当前会话 |
| `user_message` | str | 用户输入文本 |
| `context_length` | int | 上下文 Token 窗口上限（默认 `Config.CONTEXT_LENGTH`） |

**返回值**: `Optional[str]`

- 引擎不可用或出错 → 返回 `None`（不注入）
- 调用 `engine.assemble(user_message, context_length)` 完整管线
- 从返回消息列表中提取 `[~/N]` 标记（CA 摘要注入）
- 多条摘要用 `\n\n` 拼接返回
- Hermes 会将返回文本注入到 user message 中

**返回格式示例**：
```
[~/1] CA插件运行正常钩子路径完整
[~/1/3] search_files: 无返回数据
[~/1/5] terminal: 配置项 `context.engine` 为 compressor
```

### `_on_post_llm_call` — 数据积累（C-stage）

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

行为：异步调 `engine.process_turn_async(user_message, assistant_response, history_copy)`
`conversation_history` 传副本（列表拷贝），避免竞态。

## 已知修复与改进（相对于源项目）

### 初始部署修复（2026-06-04）

| 修复 | 说明 |
|------|------|
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，已改为 profile 感知。断路器状态文件写入 `~/.hermes/profiles/tester/` |
| `sys.path` 保障本地 `ca/` 优先 | 插件目录加入 `sys.path.insert(0, ...)`，确保导入走本地 `ca/` 子目录 |
| 添加 `register(ctx)` 函数 | 原插件无 `register()` 函数，所有 hook 回调永不注册 |
| Hook 签名适配 | 所有 hook 回调改为 `**kwargs: Any` 模式。`on_session_start` 移除对 `kwargs["hermes_home"]` 的依赖。`pre_llm_call` 返回 `Optional[str]` |
| 模块级引擎注册表 | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁，支持多 session 并发 |

### Bug 修复（2026-06-05）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| C-stage Executor shutdown 竞态 | `_shutdown_cache_executor()` 没设 `_destroyed`。修复：`destroy()` 和 `reset()` 改用 `cache.destroy()`（同时设 `_destroyed` + cancel retry + shutdown executor） | `ca/__init__.py` 第 245, 1148 行 |
| Head 保护区方向错误 | `_compute_layers_v2()` 取 `sorted(...)[-HEAD_AUTO_L1_COUNT:]` 取了末尾 3 轮。修复：`[-N:]` → `[:N]` | `ca/__init__.py` 第 662 行 |

### ToolSummarizer 结构化摘要 10 handlers（2026-06-06）

`ca/tool_summarizer.py` 内按工具名分派 handler，替代通用字段提取。

| # | Handler | 行号 | L0 摘要示例 |
|---|---------|------|------------|
| 1 | `_summarize_terminal` | 129 | `terminal: find /home -name "*.py" (4 lines)`，内建 pytest 检测 → `pytest: 8 passed, 2 skipped — 0.44s` |
| 2 | `_summarize_execute_code` | 228 | 代理到 terminal handler，替换 tool_name |
| 3 | `_summarize_write_file` | 235 | `write_file: /home/i1j/test.txt`（只保留文件路径） |
| 4 | `_summarize_patch` | 266 | `patch: /home/i1j/tool_summarizer.py` + replace_all 标记 |
| 5 | `_summarize_read_file` | 302 | `read_file: /tmp/test.py`（文件名 + 行数范围） |
| 6 | `_summarize_search_files` | 342 | `search_files: *.py → 0 hits` 或 `66 matches [ca:30, tests:25, docs:11]` |
| 7 | `_summarize_skills_list` | 404 | `skills_list: 3 skills (tester-workflow, ...)` |
| 8 | `_summarize_skill_view` | 444 | `skill_view: tester-workflow — 12 lines` |
| 9 | `_summarize_skill_manage` | 496 | `skill_manage: patch tester-workflow (error)`（提取 action/name/file_path） |
| 10 | `_summarize_memory` | 547 | `memory: replace memory (error)`（action/target/old_text + content 80 字符预览） |

分发机制：`summarize()` → `getattr(self, f"_summarize_{sanitized}", None)` → handler。工具名中 `.`/`-` 自动映射为 `_`。handler 失败时 `try/except` 回退到通用字段提取。

通用增强：
- **terminal 错误标记**：`_ERROR_RE` 正则（Traceback/Error:/Exception/...）扫描输出，匹配时前缀 `[ERROR]`
- **search_files 目录分组**：total_count > 3 时 `Counter` 按父目录名聚合，取前 4 组

## 存储结构

### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：`~/.hermes/profiles/tester/ca_cache/{session_id}.db`

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

- **L2 是递增快照**：每条 subturn 存独立消息切片，非全量拼接
- **L0 100 字符上限**：超过自动截断
- **全量嵌入**：成功时 l0 + l1 均为 4096 字节 BLOB
- **所有 turn 独立存储**：40 条 subturn = 40 行 turn_cache

## 关键环境变量

| 变量 | 当前值 | 说明 |
|------|--------|------|
| `CA_EMBED_BACKEND` | `ollama` | 嵌入后端 |
| `CA_EMBED_MODEL` | `qwen3-embedding:0.6b` | 嵌入模型 |
| `CA_EMBED_ENDPOINT` | `http://localhost:11439` | 嵌入服务端点 |
| `CA_LLM_MODEL` | `qwen3-4b-instruct` | L1 摘要生成模型 |
| `CA_LLM_ENDPOINT` | `http://localhost:11440` | LLM 服务端点 |
| `CA_EMBED_TIMEOUT` | 30 | 嵌入超时（秒） |
| `CA_LLM_TIMEOUT` | 180 | LLM 超时（秒） |

## 断路器

| 项目 | 值 |
|------|------|
| 文件路径 | `{hermes_home}/.ca_assembler_state_{PID}.json` |
| 状态格式 | `{"failures": N, "retry_after": null \| ISO_timestamp}` |
| 触发阈值 | 连续 3 次失败 |
| 冷却时间 | 1 小时 |
| 恢复 | 1 次成功 reset 或超时后自动清除 |

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
engine = session_manager.get(session_id, str(db_path))
result = engine.assemble("测试", 32000)
ca_count = sum(1 for m in result
    if isinstance(m.get("content",""), str) and m["content"].startswith("[~/"))
print(f"{ca_count} CA summaries in assembled context")
```

## 当前遗留状态（2026-06-14）

| 问题 | 说明 | 优先级 |
|------|------|--------|
| `_shutdown_cache_executor()` 死代码 | `ca/__init__.py` 第 254-262 行，全项目无调用方。低风险 | 低 |
| 空摘要 BM25 排除 | `result_summary="无返回数据"` 的工具轮仍进入检索/升级候选 | 低 |
| 系统消息降级 | `background_review` ContextVar 路径已修。其他系统触发消息仍可能被 LLM 误判 | 低 |
| bare `except:` 吞异常 | 在 `tests/conftest.py`，不影响被测代码 | 低 |

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

| 差异点 | source 项目 | 当前部署 |
|--------|-----------|---------|
| 代码位置 | `~/projects/context-assembler/` | `plugins/ca_assembler/` 自包含副本 |
| `register()` | 不存在 | 已添加，注册 5 个 hooks |
| `_state_file_path()` | `Path.home() / ".hermes"` | `get_hermes_home()`（profile 感知） |
| `sys.path` | 无特殊处理 | 本地 `ca/` 子目录优先 |
| 激活方式 | `context.engine: ca_assembler` | `plugins.enabled: [ca_assembler]` |

## 相关文档

| 文档 | 路径 | 内容 |
|------|------|------|
| 技术方案 | `docs/technical-plan.md` | 完整设计文档：架构、数据结构、C/A/L 三阶段 |
| 调试报告验证工作流 | `docs/ca-debug-report-fix-verification.md` | 修复验证流程、关键管线代码位置 |
| ToolSummarizer 结构改进 | `docs/tool-summary-improvements.md` | 10 handlers 清单 + 待改进/已排除 |
| L0 摘要质量观察 | `ca-ctx-inspect/references/l0-summary-quality-observations.md` | 对话轮 L0 摘要质量实测 |
| system_overhead 分析 | `docs/system-overhead-measurement-analysis.md` | 测量代码移除分析 |
