# 需求规格 v2: 工具轮数据重构 — 基于双钩子混合架构

## 1. 功能概述

重构 CA 插件的工具轮数据存储结构，使其严格遵循 OpenAI Chat Completions API 标准消息格式，并根除 C-stage 中每轮重复遍历全量 history 的性能问题。

## 2. 现状分析

### 2.1 当前数据流（有问题）

```
post_llm_call:
  conversation_history → _extract_tool_calls(全量) → 逐工具拆 N 条独立记录
  每条记录 l2_text = [assistant{all_calls}, tool{N}]
  
  → _rebuild_messages_from_cache 展开为:
    [asst{calls_a,b}, tool_a, asst{calls_a,b}, tool_b]  ❌ assistant 重复
```

### 2.2 根因

`post_llm_call` 的 `conversation_history` 包含全量历史消息。`_extract_tool_calls` 每次迭代全部历史工具调用写入 DB。每轮写全部历史工具调用。20× 重复。

## 3. 重构方案

### 3.1 双钩子混合架构

利用两个独立的 Hermes hook 提供各自的数据来源：

```
post_api_request     → 提供工具组结构（thought + tool_calls 定义数组）
post_tool_call       → 提供单工具执行结果（result, status, duration_ms）

在引擎中按 api_request_id + tool_call_id 合并 → 重建完整 OpenAI 标准 L2
```

#### Hook 1: `post_api_request`

**触发时机**：每次 API 调用完成后立刻触发，**在工具执行之前**。

**来源**（`conversation_loop.py:3580-3607`）：

| 参数 | 内容 | 用途 |
|------|------|------|
| `assistant_message` | 完整的 LLM 响应消息对象（含 `.content` + `.tool_calls`） | ✅ 工具组结构 + thought |
| `finish_reason` | `"stop"` / `"tool_calls"` / `"length"` | ✅ 过滤：仅 tool_calls 时处理 |
| `api_request_id` | 本次 API 调用唯一 ID | ✅ 归组依据 |
| `response` | 原始 API 响应 payload | 可选备用 |

`assistant_message` 是 Python 对象，属性：
- `.content` — **thought（思考过程文本）**，即 OpenAI 标准消息的 `role: assistant → content` 字段
- `.tool_calls` — `[ToolCall(id, function.name, function.arguments)]` 数组

**这正是 OpenAI 标准 assistant 消息的完整结构。**

#### Hook 2: `post_tool_call`

**触发时机**：每个工具执行完毕后触发。

| 参数 | 内容 | 用途 |
|------|------|------|
| `tool_name` | 工具名 | `function.name` |
| `args` | 参数字典（已 parse，dict 类型） | `function.arguments` |
| `result` | 执行结果文本 | `tool.content` |
| `tool_call_id` | 工具调用 ID | 关联到 `tool_calls[].id` |
| `api_request_id` | 所属 API 调用 | 关联到 `post_api_request` 组 |
| `status` | `ok`/`error`/`cancelled` | L1/L0 摘要用 |
| `duration_ms` | 耗时 | L1/L0 摘要用 |

### 3.2 数据合并与 L2 重建

```
post_api_request(finish_reason="tool_calls", assistant_message={content, tool_calls}):
  → buffer.structure[api_request_id] = {
      "thought": assistant_message.content,
      "tool_calls": assistant_message.tool_calls  # [ToolCall(id, name, args_json), ...]
    }

post_tool_call(api_request_id, tool_call_id, result, status, ...):
  → buffer.results[api_request_id][tool_call_id] = {
      "result": result,
      "status": status,
      "duration_ms": duration_ms,
    }

flush(api_request_id):
  struct = buffer.structure[api_request_id]
  results = buffer.results[api_request_id]
  
  # 重建 OpenAI 标准 L2
  l2 = [
    {
      "role": "assistant",
      "content": struct["thought"],         # ← 完整保留 thought
      "tool_calls": struct["tool_calls"]    # ← 完整保留定义
    },
    ...  # 对每个 tool_call，生成 tool 响应消息
    {"role": "tool", "tool_call_id": id, "content": results[id]["result"]},
    ...
  ]
```

### 3.3 L2 格式（OpenAI 标准）

每个工具组（一个 `api_request_id`）存为**一条** `turn_cache` 记录：

```json
[
  {
    "role": "assistant",
    "content": "我需要先查一下这个文件的内容和搜索结果",
    "tool_calls": [
      {"id": "call_1", "function": {"name": "read_file", "arguments": "{\"path\": \"/tmp/x\"}"}},
      {"id": "call_2", "function": {"name": "search_files", "arguments": "{\"pattern\": \"*.py\"}"}}
    ]
  },
  {"role": "tool", "tool_call_id": "call_1", "content": "文件内容..."},
  {"role": "tool", "tool_call_id": "call_2", "content": "[a.py, b.py]"}
]
```

### 3.4 写入时机

**flush 触发点**：`post_llm_call` 时。

引擎维护当前活跃的 `api_request_id` 集合。每个 `post_api_request` 注册一个新组，`post_tool_call` 填充结果，`post_llm_call` 触发 flush。

```
时序：
  ① post_api_request(assistant_message={thought, tool_calls}) 
     → buffer.structure[aid] = {thought, tool_calls}
  
  ② post_tool_call(tool_call_id="call_1", result="...")
     → buffer.results[aid]["call_1"] = {result, status}
  
  ③ post_tool_call(tool_call_id="call_2", result="...")
     → buffer.results[aid]["call_2"] = {result, status}
  
  ④ post_llm_call()
     → flush(aid) → 重建 L2 → ToolSummarizer 逐工具 L1/L0 → write_turn
     → process_turn_async() → 对话轮 C-stage（不再有 messages 参数）
```

### 3.5 单工具 L1/L0 在单条记录中的承载

一条工具组记录需要承载组内 N 个工具的独立 L1/L0。方案：**`l1_text` 和 `l0_text` 存为 JSON list**。

```python
# l1_text (JSON)
{
  "tools": [
    {"tool_name": "read_file", "result_summary": "文件内容...", "tool_args": {...}, ...},
    {"tool_name": "search_files", "result_summary": "2 matches", "tool_args": {...}, ...}
  ]
}

# l0_text (JSON list of strings, 100 char each)
["read: x.txt (120L)", "search: *.py=2"]
```

**A-stage 兼容**：`tool_l1_texts[(turn, 1)]` 和 `tool_l1_texts[(turn, 2)]` 需要从单条记录的 list 中按索引拆分。`get_tool_snapshot_data` 和 `add_tool_turn` 的签名需适配。

`read_turn_texts` 当前按 `(turn_index, turn_type, tool_sub_index)` 查询。改为：`tool_sub_index=0` 读取整组 l1_text，由调用方按 sub_index 取列表元素。

### 3.6 A-stage 重建兼容

`_rebuild_messages_from_cache`：
- 工具组记录展开为 `[assistant{thought, tool_calls}, tool1, tool2]`（单条记录 → 1 + N 条消息）
- 不再有重复的 assistant 消息

`_build_tool_key_map`：
- 同组 N 个 tool_calls 归属同一个 `start_index`（同一条 assistant 消息）
- 对每个 `tool_calls[sub_idx]` 生成 `(turn, sub_idx)` key

## 4. 功能需求

### [REQ-1] 注册 `post_api_request` hook

CA 插件注册 `post_api_request` hook。当 `finish_reason == "tool_calls"` 时，捕获 `assistant_message`（含 `.content` 和 `.tool_calls`）存入引擎 buffer。

**验收标准**：
- `register()` 注册 7 个 hooks（+ `post_api_request` + `post_tool_call`）
- `_on_post_api_request` 收到带 `finish_reason="tool_calls"` 的请求时，提取 `assistant_message.content`（thought）和 `assistant_message.tool_calls` 写入 buffer

### [REQ-2] 注册 `post_tool_call` hook

CA 插件注册 `post_tool_call` hook。每次工具执行完后捕获 `tool_name`、`args`、`result`、`tool_call_id`、`api_request_id`、`status`、`duration_ms` 存入引擎 buffer，按 `api_request_id` + `tool_call_id` 索引。

**验收标准**：
- `register()` 共注册 8 个 hooks
- 参数完整映射到内部 buffer

### [REQ-3] buffer → L2 重建 → 单次写入

在 `post_llm_call` 触发时 flush buffer：合并 `post_api_request` 的结构数据和 `post_tool_call` 的结果数据，重建 OpenAI 标准 L2，逐工具调用 ToolSummarizer，写入 `turn_cache`。

**验收标准**：
- 3 轮对话 × 每轮 2 工具调用分成 1-2 组 → DB 中 1-2 条工具组记录
- 每条工具组记录的 `l2_text` 含完整 thought + 所有 tool_calls + 所有 tool 响应
- C-stage 不再有 `if messages:` 分支

### [REQ-4] thought 完整保留

`post_api_request.assistant_message.content` 设置为 L2 的 `assistant[0].content`。

**验收标准**：
- thought 非空时，L2 `assistant[0].content == thought`
- thought 为空字符串时，L2 `assistant[0].content == ""`

### [REQ-5] 逐工具 L1/L0

单条工具组记录内，每个工具独立调用 `ToolSummarizer.summarize()` 生成 `(l1, l0)`。`l1_text` 存为 `{"tools": [l1_1, l1_2, ...]}`，`l0_text` 存为 `[l0_1, l0_2, ...]`。

**验收标准**：
- `tool_l1_texts[(turn, 1)]` 可独立读取第 1 个工具的 L1
- `tool_l1_texts[(turn, 2)]` 可独立读取第 2 个工具的 L1
- A-stage `_compute_turn_plan_v2` 逐工具调度不变

### [REQ-6] A-stage 端到端兼容

`_rebuild_messages_from_cache` 展开工具组记录为 `[assistant{tc}, tool1, tool2]`（无重复）。
`_build_tool_key_map` 中 N 个 tool 映射到同一个 assistant 消息索引。
rest 不变。

### [REQ-7] 向后兼容

旧 DB 中老格式数据（per-tool 格式）与新格式并存。`_rebuild_messages_from_cache` 能同时处理两种格式。

**明确不做的**：
- 不修改 Hermes 宿主
- 不重构对话轮处理
- 不修改断路器 / 生命周期代码
