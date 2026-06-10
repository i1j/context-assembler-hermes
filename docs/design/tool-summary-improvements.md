# ToolSummarizer 结构化摘要设计 v2

**日期**: 2026-06-09
**版本**: v2（v1 发布于 2026-06-06，现重写以反映架构实况）

---

## 核心架构

```
Hermes tool 返回 JSON     ToolSummarizer handler      per-tool L0 + L1       group 层
                         ┌───────────────────┐
tool_1 raw content  →    │ _summarize_XXX    │ →  L0_1 + L1_1            ┐
tool_2 raw content  →    │ _summarize_YYY    │ →  L0_2 + L1_2            ├ → group_l0
tool_3 raw content  →    │ (通用 fallback)   │ →  L0_3 + L1_3            ┘   (L0_1 | L0_2 | L0_3)
                         └───────────────────┘
                                                     group_l1 + group_l0 → group 显示
                                                     (L0：拼接；L1：_format_group_summary)
```

**本质**: 工具组不是自上而下的变换，是自下而上的拼接。各层只是把下层的结果按工具顺序串联，不做额外截断或改写。

---

## 分层职责

### 第 0 层：Hermes 返回的 raw content

每个工具调用返回 JSON 结构，已知字段包括：
- `output` (terminal), `total_count` + `files` (search_files)
- `bytes_written` + `dirs_created` (write_file)
- `success` + `message` (skill_manage)
- `todos` + `summary` (todo)
- `content` + `total_lines` (read_file)

**handler 从已知字段提取**，不做文本清洗。

### 第 1 层：handler 构造单工具 L1 + L0

**L1** 是结构化 JSON dict（存入 `turn_cache.l1_text`），用于组级格式化与未来检索：

```python
{"tool_name": "search_files", "result_summary": "8 hits [debug:8]",
 "tool_args": {...}, "error": None, ...}
```

**L0** 是 ≤100 字符的文本（存入 `turn_cache.l0_text`），用于快速展示：

```
[tool_name]: {信息密度最大化的摘要}
```

### 第 2 层：buffer flush 拼组摘要

`generate_group_summary()` 遍历组内工具，提取 `tool_name` + `status` + `result_summary`：

```python
group_result = "；".join([tr["result_summary"] for tr in tool_results[:3]])
group_l0 = " | ".join([s["l0"] for s in per_tool_summaries[:5]])
```

**字符限制基于工具轮而非工具组。** 每个 handler 产出的 L0（≤100 字符）和 result_summary 已在 handler 层各自截断，组级只做 `[:3]` / `[:5]` 的数量限制，**不做二次截断**。

### 第 3 层：注入显示（_format_group_summary）

```python
工具组：{intent}→{group_result}（{N}个，{state}）
```

**不包含 thought**（历史 AGENT 运行时状态，信息价值为零）。

---

## L0 信息密度原则

每 token 必须传达有用信息。按优先级：

1. **结果优先** — `exit=0` / `found 3 files` 而不是 `[cmd] ...`
2. **省略命令文本** — L0 v4 原则，terminal 不讲 `[python3 -c "..."]`，只讲 `exit=N` 或关键输出
3. **路径脱敏** — `/home/i1j/` → `~/`
4. **去尾不掐头** — `read_file: …/ca/__init__.py (2017 lines)`，保留文件名
5. **同类型折叠** — `search_files × 3` 优于 `search_files | search_files | search_files`
6. **无 raw JSON / Python repr** — `{'total': 5, ...}` 或 `{"bytes_written": 2171}` 必须从字段提取

---

## Handler 清单（12 handlers）

| # | Handler | Hermes 返回格式 | L0 格式 | L1 result_summary | 状态 |
|---|---------|----------------|---------|-------------------|------|
| 1 | `_summarize_terminal` | `{"output": "...", "exit_code": N}` | `exit=N` 或关键输出行 | 结果优先，无 `[{cmd}]` | ✅ v4 |
| 2 | `_summarize_execute_code` | 同 terminal | `exit=N` 或关键输出行 | 跳转到 terminal | ✅ v4 |
| 3 | `_summarize_write_file` | `{"bytes_written": N, "dirs_created": bool}` | `写入：{path}（{bytes_written} 字节）` | 提取 bytes_written + path | ✅ v4 |
| 4 | `_summarize_patch` | `{"success": bool}` | `patch: {path}` | `patch: {path}(success/error)` | ✅ v4 |
| 5 | `_summarize_read_file` | `{"content": "...", "total_lines": N}` | `read_file: …{fname} ({total_lines} lines)` | 文件名 + 行数 | ✅ v4 |
| 6 | `_summarize_search_files` | `{"total_count": N, "files": [...], "matches": [...]}` | `search_files: {pattern} → {N} hits` | 命中数 + 目录分布 | ✅ v4 |
| 7 | `_summarize_skills_list` | `{"skills": [{name, ...}]}` | `skills_list: {N} skills` | 技能名列表（前5） | ✅ v4 |
| 8 | `_summarize_skill_view` | 直接 text | `skill_view: {name} — {N} lines` | 技能名 + 行数 | ✅ v4 |
| 9 | `_summarize_skill_manage` | `{"success": bool, "message": str}` | `skill_manage: {action} {name} (ok/error)` | 提取 success + message | ✅ v4 |
| 10 | `_summarize_memory` | `{"success": bool, "error": str}` | `memory: {action} {target} (ok/error)` | 提取 action/target/old_text | ✅ v4 |
| 11 | `_summarize_skill_view` | 同 skill_view | — | — | ✅ v4 |
| 12 | `_summarize_todo` | `{"todos": [...], "summary": {"total": N, ...}}` | `todo: N 项（M pending...）` | 提取 summary 计数 | ✅ v5.0 |

**命名映射**：工具名中的 `.` / `-` 自动转为 `_`（如 `execute-code` → `_summarize_execute_code`），`summarize()` 通过 `getattr(self, f"_summarize_{sanitized}", None)` 分发。

---

## 组级显示格式

统一通过 `_format_group_summary(l1_json)`：

```
工具组：{intent}→{tool_result}；{tool_result}（{N}个，{state}）
```

示例：
```
工具组：查文件→search_files: 8 hits [debug:8]；search_files: 4 hits [ca:4]（2个，ok）
工具组：→exit=0（1个，ok）
工具组：→todo: 5 项（4 pending, 1 in_progress）（1个，ok）
工具组：搜索代码→search_files: *.md → 1 hits；search_files: debug* → 8 hits（2个，ok）
```

所有级别（L0/L1/L2 → L0 fallback）统一此格式，不再有 `工具组：{raw_l0}` 和 `工具组：{intent}→{result}` 两套形式。

---

## 设计决策

| 问题 | 讨论 | 结论 |
|------|------|------|
| thought 在 L1 组显示 | 对历史认知无用，只是 AGENT 运行时状态 | ❌ 不展示 |
| L1/L2 时 `text[:200]` | 各工具摘要已在 handler 各自截断，组级不应再追加总上限 | ❌ 不要 |
| L0 组是否走 `_format_group_summary` | 统一显示格式；l1 与 l0 同时写入 cache，l1 一定存在 | ✅ 统一走 |
| terminal result_summary 命令前缀 | 命令独占 60 字符，真实输出被截 | ❌ 结果优先 |
| handler 内 JSON 提取 | 从已知字段提取，不做文本清洗 | ✅ 正确做法 |
| Python repr fallback (`{'total': 5}`) | ast.literal_eval 处理 JSON 失败后的单引号 Python 语法 | ✅ fallback |
|| 组内同类型工具折叠 | `search_files × 3` 代替三次同工具 | ⏳ 未来改进 |

---

## Hermes 原生工具响应结构（不含 CA）

> **补注**（2026-06-10）：本节澄清 Hermes agent loop 原生层面的工具响应构成，说明 CA 与 Hermes 的边界。

### Agent Loop 内工具响应流程

```
LLM 返回 assistant{content(thought), tool_calls:[{name, args}]}
↓
agent loop 提取 tool_calls
↓↓ (for each tool)
handle_function_call(function_name, function_args)
↓ 执行具体工具逻辑（读文件、查搜索、跑命令等）
↓ 返回 JSON 字符串（完整的原始响应）
↓
messages.append({
    "role": "tool",
    "content": "{完整的原始 JSON}",
    "tool_call_id": "call_xxx",
    "tool_name": "read_file",
})
↓
下一轮 API call 将 messages[]（含 tool 响应）传给 LLM
```

**关键点：** 不经过 CA 时，LLM 直接看到工具的完整原始输出。Hermes 不对工具响应做摘要或按类型区分处理。唯一的硬上限是 `file_read_max_chars=100K`（字符数，非 token 数），超出会被截断。

### Hermes 持久化层：state.db

Hermes 有自己的 SQLite 持久化 `state.db`（`SessionDB`），消息表 schema：

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,          -- 'user' / 'assistant' / 'tool' / 'system'
    content TEXT,                -- 原始消息全文 JSON/文本
    tool_call_id TEXT,           -- 仅 role='tool' 时有值
    tool_calls TEXT,             -- assistant 的 tool_calls 定义 JSON
    tool_name TEXT,              -- 仅 role='tool' 时有值
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    active INTEGER DEFAULT 1
);
```

| 特性 | 值 |
|------|-----|
| 目的 | 会话管理、/resume、/history、FTS5 搜索 |
| 摘要处理 | ❌ 无，原始 content 原样存入 |
| 按工具类型区分 | ❌ 无，全部混在 content 字段 |
| token 估算 | `token_count` 列（估算值，非精确） |
| 生命周期 | 永不过期（由 active/archived 标记控制） |

**CA 不写 state.db**（解耦原则）。两者是独立的持久化路径。

### CA 层的工具响应存储：turn_cache

CA 通过 3 个工具采集 hooks（pre/post_tool_call + post_api_request）增量捕获工具响应，存入自己的 `ca_cache/{session_id}.db`：

```sql
-- turn_cache 表（v5 schema）
CREATE TABLE turn_cache (
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,       -- 对话轮次
    api_call_count INTEGER NOT NULL,   -- API 调用序号（工具组标识）
    seq_index INTEGER NOT NULL,         -- 组内消息序号
    role TEXT NOT NULL,                 -- 'user' / 'assistant' / 'tool'
    content TEXT,                       -- 原始消息（同 state.db 语义）
    tool_call_id TEXT,
    tool_name TEXT,                     -- 按此字段分发 handler
    tool_calls_json TEXT,
    finish_reason TEXT,
    l0_text TEXT DEFAULT '',            -- 极简摘要（~60 字符）
    l1_text TEXT DEFAULT '',            -- 结构化 JSON 摘要
    l0_embedding BLOB,                  -- L0 嵌入向量
    l1_embedding BLOB,                  -- L1 嵌入向量
    ...
);
```

| 特性 | 值 |
|------|-----|
| 目的 | CA 的上下文组装与摘要分层 |
| 摘要处理 | ✅ 三级（content/l1_text/l0_text） |
| 按工具类型区分 | ✅ 通过 `tool_name` 字段 + ToolSummarizer handler 分发 |
| handler 分支 | 12 个 handler（read_file / terminal / write_file / patch / search_files / memory / skill_view / skill_manage / skills_list / execute_code / todo / 通用 fallback） |
| 生命周期 | 按 session 组织，session 结束后保留供后续检索 |

### 对比总结

| 维度 | Hermes state.db | CA turn_cache | 关系 |
|------|----------------|---------------|------|
| 存什么 | 完整会话（所有 role） | 完整会话（所有 role）+ 摘要 | 数据冗余（各自独立） |
| tool 摘要 | 无 | 12 个 handler 按类型分支 | CA 是增强层 |
| 按类型区分 | 无 | tool_name 级区分 + handler 级分支 | CA 提供此能力 |
| 用途 | 会话管理/搜索 | 上下文组装/预算控制 | 互补 |
| 写入机制 | agent loop 每次 API 调用后 | Hook 采集（pre/post_tool_call + post_api_request）/ 异步摘要 | CA 旁路采集 |
| 写入时机 | 每次 LLM API call 后 | 增量采集（tool） + 异步处理（dialogue） | CA 可与 agent loop 并发 |

### 对"历史记忆表"问题的直接回答

**Hermes 层：** 有持久化历史表（`state.db.messages`），但不按工具类型做区别处理——所有 `role='tool'` 的消息的原始 JSON 原样存入 `content` 字段，与 user/assistant 消息混在同表同列。

**CA 层：** 有独立的历史表（`turn_cache`），除原始 content 外额外维护 l1_text（结构化 JSON 摘要）和 l0_text（极简文本），通过 `tool_name` 列 + ToolSummarizer handler 分发实现按工具类型的不同侧重摘要。

**不经过 CA 时，** 模型永远看到原始 500 行文件内容的 JSON，没有摘要层，没有按类型区分。
|