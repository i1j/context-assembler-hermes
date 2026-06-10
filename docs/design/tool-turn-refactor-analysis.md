# 工具轮数据重构 — 分析报告

> 本文档按四章结构组织：正常流程 → 理想数据结构 → 当前问题 → 改造方案

---

<!-- auto-generated TOC -->
# design/tool-turn-refactor-analysis.md

## 目录

- [一、云端大模型与 Hermes 对话（含调用工具）的完整链路](#一云端大模型与-hermes-对话含调用工具的完整链路)
- [二、理想 L2 全量数据结构](#二理想-l2-全量数据结构)
- [三、当前实现与理想模型的差距分析](#三当前实现与理想模型的差距分析)
- [四、重构整体方案](#四重构整体方案)

---
## 一、云端大模型与 Hermes 对话（含调用工具）的完整链路

### 1.1 三层嵌套结构

一次用户提问，Hermes 按三层嵌套循环处理：

```
第 1 层 ─ 对话轮 (Turn)         ← pre_llm_call ... post_llm_call
  │                                run_conversation() 一次调用 = 一轮
  │
  ├─ 第 2 层 ─ API 调用 (API)   ← pre_api_request ... post_api_request
  │    │                            while 循环，直到 LLM 返回纯文本
  │    │
  │    └─ 第 3 层 ─ 工具执行 (Tool)
  │                              ← pre_tool_call ... post_tool_call
  │                                 该次 API 返回的每个 tool_call 各一对
```

**数量关系（一次用户提问）**：

| 层级 | Hook | 触发次数 | 说明 |
|------|------|---------|------|
| 对话轮 | `pre_llm_call` / `post_llm_call` | **1 对** | 一头一尾，中间循环 |
| API 调用 | `pre_api_request` / `post_api_request` | **N 对** | 直到 `finish_reason="stop"` |
| 工具执行 | `pre_tool_call` / `post_tool_call` | **M 对** | M ≥ N，每次 API 返回可能有多个工具 |

### 1.2 三层嵌套的代码骨架

`conversation_loop.py` 中的 `run_conversation()` 伪代码：

```python
def run_conversation():
    """处理一次用户输入（一轮对话）。"""

    # ═══════════════════════════════════════
    # Level 1 入口：pre_llm_call（一轮一次）
    # ═══════════════════════════════════════
    invoke_hook("pre_llm_call",
        session_id=agent.session_id,
        turn_id=turn_id,
        user_message=original_user_message,
        conversation_history=list(messages),
        is_first_turn=(not bool(conversation_history)),
        model=agent.model, platform=..., sender_id=...,
    )
    # 返回值作为上下文注入到 user message

    # ── 初始化 ──
    final_response = None
    api_call_count = 1    # 第 1 次 API 调用（1-based；api_call_count=0 保留给 user 行，最终文本用 999999）
    interrupted = False

    # ═══════════════════════════════════════
    # Level 2：API 调用循环
    # ═══════════════════════════════════════
    while (api_call_count < max_iterations and budget > 0) or grace_call:

        # ── 第 2 层入口：pre_api_request ──
        invoke_hook("pre_api_request",
            api_request_id=uid, model=agent.model,
            tool_count=N, message_count=N,
            approx_input_tokens=N,
            request={"body": {"messages": [...]}},
        )

        # ── 调用 LLM API ──
        assistant_message, response = call_llm(api_messages)

        # ── 第 2 层出口：post_api_request ──
        invoke_hook("post_api_request",
            api_request_id=uid,
            assistant_message=assistant_message,  # ★ 工具组结构
            finish_reason=finish_reason,           # "tool_calls"|"stop"|"length"
            response=response, api_call_count=api_call_count,
            usage=usage, turn_id=turn_id, ...
        )

        # ── 判断 LLM 返回了什么 ──
        if finish_reason == "tool_calls":
            assistant_dict = _build_assistant_message(assistant_message, finish_reason)
            messages.append(assistant_dict)

            # ═══════════════════════════════════
            # Level 3：逐工具执行
            # ═══════════════════════════════════
            for tool_call in assistant_message.tool_calls:

                # 第 3 层入口：pre_tool_call
                block = get_pre_tool_call_block_message(
                    tool_name=tool_call.name,
                    args=json.loads(tool_call.arguments),
                    tool_call_id=tool_call.id,
                    api_request_id=uid, ...
                )
                if block:
                    result = json.dumps({"error": block})
                    _emit_post_tool_call_hook(..., status="blocked", ...)
                else:
                    result = _invoke_tool(tool_call.name,
                                          json.loads(tool_call.arguments), ...)

                    # 第 3 层出口：post_tool_call
                    _emit_post_tool_call_hook(
                        tool_name=tool_call.name,
                        args=json.loads(tool_call.arguments),  # 已 parse 的 dict
                        result=result,                          # 原始结果字符串
                        tool_call_id=tool_call.id,
                        api_request_id=uid,
                        status="ok"|"error",
                        duration_ms=N, ...
                    )

                messages.append({"role": "tool",
                                 "tool_call_id": tc.id,
                                 "content": result})

            api_call_count += 1
            continue   # → Level 2 继续

        elif finish_reason == "stop":
            final_response = assistant_message.content
            break

        else:  # finish_reason == "length" | "content_filter" | etc.
            # 截断或异常结束：仍尝试保存已有数据。
            # - 若有 content → 当作文本回复保存（但标记截断）
            # - 若有 tool_calls → 当作工具组保存（但标记截断，可能不完整）
            # 两种情况下都 break，不再继续 API 循环。
            if assistant_message.tool_calls:
                # 有工具调用定义 —— 截断发生在工具列表上
                # 仍然 flush buffer，但标记 _assemble_status=1
                # （CA 引擎在 flush 时标记截断状态）
                _truncated_tool_calls = True
            if assistant_message.content:
                final_response = assistant_message.content or "(truncated)"
            break

    # ═══════════════════════════════════════
    # Level 1 出口：post_llm_call（一轮一次）
    # ═══════════════════════════════════════
    if final_response and not interrupted:
        invoke_hook("post_llm_call",
            session_id=agent.session_id, turn_id=turn_id,
            user_message=original_user_message,
            assistant_response=final_response,       # 纯文本字符串
            conversation_history=list(messages),     # 完整消息列表
            model=agent.model, platform=..., ...)
```

### 1.3 三层嵌套——6 个关键 Hook 的详细参数

#### Level 2（API 调用）：`pre_api_request` / `post_api_request`

##### `pre_api_request` — API 请求前触发

**CA 用途**：❌ 只有请求信息，无响应数据。

```
pre_api_request(
    task_id, turn_id, api_request_id,
    session_id, platform,
    model, provider, base_url, api_mode,
    api_call_count, message_count,
    tool_count, approx_input_tokens,
    request_char_count, max_tokens,
    request,                    # API 请求体（含 messages）
)
```

##### `post_api_request` — API 响应后立即触发（★ 工具组结构来源）

| 参数 | 类型 | 内容 | 说明 |
|------|------|------|------|
| `assistant_message` | `NormalizedResponse` | **完整 LLM 响应对象** | 含 thought + tool_calls 定义 |
| `.content` | `str \| None` | **thought（思考过程）** | tool_calls 时的思考文本 |
| `.tool_calls` | `list[ToolCall] \| None` | **工具调用定义数组** | 每个 ToolCall 含 id, name, arguments(JSON) |
| `.finish_reason` | `str` | `"tool_calls"` \| `"stop"` \| `"length"` | 区分本次 API 调用了工具否 |
| `api_request_id` | `str` | API 调用唯一 ID | 工具组的归组标识 |
| `response` | `dict` | 原始 API 响应 payload | 含 SDK 原生 tool_calls 对象 |
| `usage` | `dict \| None` | Token 用量 | - |
| `api_call_count` | `int` | 本轮第几次 API 调用 | 多轮调用的顺序 |
| `turn_id` | `str` | 所属对话轮 ID | 映射到 CA 的 turn_index |

**`NormalizedResponse` 结构**：

```
NormalizedResponse
├── content: str | None          ← thought（finish_reason=="tool_calls" 时）
├── tool_calls: [ToolCall] | None
├── finish_reason: str           ← "tool_calls" | "stop" | "length"
├── reasoning: str | None
└── usage: Usage | None

ToolCall
├── id: str                     ← tool_call_id
├── name: str                   ← 工具名
├── arguments: str              ← JSON 字符串参数
└── function -> self            ← 兼容 tc.function.name / tc.function.arguments
```

---

#### Level 3（工具执行）：`pre_tool_call` / `post_tool_call`

##### `pre_tool_call` — 每个工具执行前触发

```
pre_tool_call(
    tool_name,                  # 工具名
    args,                       # 参数字典（已 parse 的 dict）
    task_id, session_id,
    tool_call_id,               # 工具调用 ID
    turn_id,                    # 所属对话轮
    api_request_id,             # 所属 API 请求（关联到 post_api_request 的组）
)
```

**CA 用途**：预注册 `tool_call_id` → buffer，标记为 pending。

##### `post_tool_call` — 每个工具执行后触发（★ 执行结果来源）

| 参数 | 类型 | 内容 | 说明 |
|------|------|------|------|
| `tool_name` | `str` | 工具名 | `function.name` |
| `args` | `dict` | **已 parse 的参数字典** | 无需再 JSON.parse |
| `result` | `str` | **工具原始执行结果** | 通常为 JSON string |
| `tool_call_id` | `str` | 工具调用 ID | 关联到 ToolCall.id |
| `api_request_id` | `str` | 所属 API 调用 ID | 关联到 post_api_request |
| `status` | `str` | `"ok"` / `"error"` / `"blocked"` / `"cancelled"` | 执行状态 |
| `error_type` | `str \| None` | 错误类型 | - |
| `error_message` | `str \| None` | 错误消息 | - |
| `duration_ms` | `int` | 执行耗时（毫秒） | - |
| `turn_id` | `str` | 对话轮 ID | - |
| `session_id` | `str` | 会话 ID | - |

---

#### Level 1（对话轮）：`pre_llm_call` / `post_llm_call`

##### `pre_llm_call` — 每轮对话开始前触发（★ 入口触发点）

| 参数 | 类型 | 内容 | 说明 |
|------|------|------|------|
| `session_id` | `str` | 会话 ID | - |
| `turn_id` | `str` | 本轮唯一 ID | - |
| `user_message` | `str` | 原始用户输入 | - |
| `conversation_history` | `list[dict]` | 历史消息列表（不含本轮 user） | 供注入上下文用 |
| `is_first_turn` | `bool` | 是否首轮对话 | - |
| `model` | `str` | 模型名 | - |

**CA 用途**：在此注入上下文（已实现 A-stage）。

##### `post_llm_call` — 对话轮结束触发（★ 保存触发器）

| 参数 | 类型 | 内容 | 说明 |
|------|------|------|------|
| `session_id` | `str` | 会话 ID | - |
| `turn_id` | `str` | 本轮 ID | - |
| `user_message` | `str` | 原始用户输入 | → L2 user 消息 |
| `assistant_response` | `str` | **最终回复文本**（纯字符串） | → L2 assistant 消息 |
| `conversation_history` | `list[dict]` | **全量消息列表** | ❌ CA 当前从这里提取工具，但不应依赖 |
| `model` | `str` | 模型名 | - |

**CA 用途**：
- `user_message` + `assistant_response` → 对话轮 C-stage（当前正确）
- **工具轮 flush 触发器**：此时 buffer 中已有完整工具组数据，一次性写入 DB

---

### 1.4 三层嵌套的时序全景图

```
  TIME →
  ──────────────────────────────────────────────────────
  Level 1 (对话轮):
    pre_llm_call │·····························│ post_llm_call
                  │                               │
                  ▼                               ▲
  Level 2 (API调用):
          pre_api_request ─ post_api_request ─ pre_api_request ─ post_api_request
          (第 1 次 API)     (返回 tool_calls)    (第 2 次 API)     (返回 stop)
                            │                                       │
                            ▼                                       ▼
  Level 3 (工具执行):
                      pre_tc→post_tc                           (无工具)
                      pre_tc→post_tc
```

> **总结**：
> - `post_llm_call` = **保存触发器**，不是数据来源
> - `post_api_request` = **工具组结构来源**（thought + tool_calls 定义）
> - `post_tool_call` = **工具执行结果来源**（result + status + duration）
> - `pre_tool_call` = **辅助注册**（预标记 tool_call_id）

### 1.5 实操穿透：一次用户输入的完整数据流

**场景**：用户说 `"帮我查 /tmp/x 的内容"`

**初始状态**：`conversation_history = [{"role": "user", "content": "帮我查 /tmp/x 的内容"}]`

---

#### Step ①：`pre_llm_call` — Level 1 入口

| 参数 | 值 |
|------|-----|
| `session_id` | `"ses_abc"` |
| `turn_id` | `"turn_001"` |
| `user_message` | `"帮我查 /tmp/x 的内容"` |
| `conversation_history` | `[{"role":"user", "content":"帮我查 /tmp/x 的内容"}]` |

---

#### Step ②：Level 2 — 第一次 API 调用

**`pre_api_request`**（CA 不处理）：

| 参数 | 值 |
|------|-----|
| `api_request_id` | `"req_001"` |
| `api_call_count` | `1` |

**LLM API 返回**：

```
NormalizedResponse:
  content: "我来查一下这个文件的内容..."
  tool_calls: [
    ToolCall(id="call_A", name="read_file", arguments='{"path": "/tmp/x"}'),
  ]
  finish_reason: "tool_calls"
```

**`post_api_request`** — ★ 工具组结构数据：

| 参数 | 值 |
|------|-----|
| `api_request_id` | `"req_001"` |
| `api_call_count` | `1` |
| `turn_id` | `"turn_001"` |
| `finish_reason` | `"tool_calls"` |
| `assistant_message.content` | `"我来查一下这个文件的内容..."` |
| `assistant_message.tool_calls[0].id` | `"call_A"` |
| `assistant_message.tool_calls[0].name` | `"read_file"` |
| `assistant_message.tool_calls[0].arguments` | `'{"path":"/tmp/x"}'` |

---

#### Step ③：Level 3 — 逐工具执行

**`pre_tool_call`**（工具执行前）：

| 参数 | 值 |
|------|-----|
| `tool_name` | `"read_file"` |
| `args` | `{"path": "/tmp/x"}` |
| `tool_call_id` | `"call_A"` |
| `api_request_id` | `"req_001"` |

→ CA buffer 注册：`call_A → {status: "pending"}`

**工具执行**：`read_file(path="/tmp/x")` → `"文件内容：hello world"`

**`post_tool_call`** — ★ 执行结果：

| 参数 | 值 |
|------|-----|
| `tool_name` | `"read_file"` |
| `args` | `{"path": "/tmp/x"}` |
| `result` | `"文件内容：hello world"` |
| `status` | `"ok"` |
| `tool_call_id` | `"call_A"` |
| `api_request_id` | `"req_001"` |
| `duration_ms` | `150` |

→ 追加到 messages：`{"role": "tool", "tool_call_id": "call_A", "content": "文件内容：hello world"}`

→ `continue` 回到 Level 2。

> **注**：`pre_tool_call` 可能返回 block 消息（例如工具被安全策略拦截），此时仍会触发 `post_tool_call` 但 `status="blocked"`，`result` 包含错误描述而非工具实际返回。`post_tool_call` 的 `status` 字段可取值：`"ok"` / `"error"` / `"blocked"` / `"cancelled"`。

---

#### Step ④：Level 2 — 第二次 API 调用（带工具结果）

**`post_api_request`**：

| 参数 | 值 |
|------|-----|
| `api_request_id` | `"req_002"` |
| `api_call_count` | `2` |
| `finish_reason` | `"stop"` |
| `assistant_message.content` | `"文件内容是 hello world"` |
| `assistant_message.tool_calls` | `None` |

`finish_reason == "stop"` → `final_response = "文件内容是 hello world"` → `break`。

---

#### Step ④b：多工具同组场景（替换上面场景——当第 1 次 API 返回多个工具时）

**场景**：用户说 `"帮我查 /tmp/x 并搜 .py 文件"`

**LLM API 返回（第 1 次）**：

```
NormalizedResponse:
  content: "我来查文件并搜索 Python 文件..."
  tool_calls: [
    ToolCall(id="call_A", name="read_file", arguments='{"path": "/tmp/x"}'),
    ToolCall(id="call_B", name="search_files", arguments='{"pattern": "*.py", "root": "/tmp"}'),
  ]
  finish_reason: "tool_calls"
```

**`post_api_request`**（第 1 次 API — 含 2 个 ToolCall）：

| 参数 | 值 |
|------|-----|
| `api_request_id` | `"req_001"` |
| `api_call_count` | `1` |
| `finish_reason` | `"tool_calls"` |
| `assistant_message.content` | `"我来查文件并搜索 Python 文件..."` |
| `assistant_message.tool_calls[0].id` | `"call_A"` |
| `assistant_message.tool_calls[0].name` | `"read_file"` |
| `assistant_message.tool_calls[1].id` | `"call_B"` |
| `assistant_message.tool_calls[1].name` | `"search_files"` |

**`pre_tool_call` × 2**（工具执行前）：

| 参数 | 第 1 个工具 | 第 2 个工具 |
|------|-----------|-----------|
| `tool_name` | `"read_file"` | `"search_files"` |
| `args` | `{"path": "/tmp/x"}` | `{"pattern": "*.py", "root": "/tmp"}` |
| `tool_call_id` | `"call_A"` | `"call_B"` |
| `api_request_id` | `"req_001"` | `"req_001"` |

→ CA buffer 注册：`call_A → {status: "pending"}`, `call_B → {status: "pending"}`

**`post_tool_call` × 2**（工具执行后）：

| 参数 | 第 1 个工具 | 第 2 个工具 |
|------|-----------|-----------|
| `tool_name` | `"read_file"` | `"search_files"` |
| `args` | `{"path": "/tmp/x"}` | `{"pattern": "*.py", "root": "/tmp"}` |
| `result` | `"文件内容：hello world"` | `"找到：a.py, b.py"` |
| `status` | `"ok"` | `"ok"` |
| `tool_call_id` | `"call_A"` | `"call_B"` |
| `api_request_id` | `"req_001"` | `"req_001"` |
| `duration_ms` | `150` | `200` |

→ 追加到 messages 两条 tool 记录。

**第 2 次 API 调用**（带两个工具结果）：

`finish_reason == "stop"` → `final_response = "文件是 hello world，找到 a.py 和 b.py"` → `break`。

---

#### Step ⑤：`post_llm_call` — Level 1 出口

| 参数 | 值 |
|------|-----|
| `assistant_response` | `"文件内容是 hello world"` |
| `conversation_history` | 见下方 |
| `turn_id` | `"turn_001"` |

`conversation_history` 最终状态：

```python
[
    {"role": "user", "content": "帮我查 /tmp/x 的内容"},
    {"role": "assistant", "content": "我来查一下这个文件的内容...",
     "tool_calls": [{"id": "call_A", "function": {"name": "read_file",
                      "arguments": '{"path":"/tmp/x"}'}}]},
    {"role": "tool", "tool_call_id": "call_A",
     "content": "文件内容：hello world"},
    {"role": "assistant", "content": "文件内容是 hello world"},
]
```

---

#### Step ⑥：`post_llm_call` — Level 1 出口（最终可用数据汇总）

| 参数 | 值 |
|------|-----|
| `assistant_response` | `"文件内容是 hello world"` |
| `conversation_history` | `[user, assistant{tc}, tool{call_A}, assistant{text}]` |
| `turn_id` | `"turn_001"` |

至此，本轮对话产生的所有数据均已在各个 hook 中被发出。汇总：

| 数据 | 来源 Hook | 归属层级 |
|------|-----------|---------|
| `user_message` | `pre_llm_call` / `post_llm_call` | Level 1 |
| `assistant_message.content` (thought) + `.tool_calls` | `post_api_request` (req_001) | Level 2 |
| `tool_name` + `args` | `post_tool_call` (call_A) | Level 3 |
| `result` + `status` + `duration_ms` | `post_tool_call` (call_A) | Level 3 |
| `assistant_response` (最终文本) | `post_llm_call` | Level 1 |
| `conversation_history` (全量消息列表) | `post_llm_call` | Level 1（仅备份） |

---

## 二、理想 L2 全量数据结构

### 2.1 设计原则：每行 = 一条 JSONL 消息

turn_cache 表中，**每行存储一条完整的 JSONL 消息**，所有字段拆为独立的列。

> 存储 ≠ 传输。行是物理存储，`ORDER BY` 后逐行输出即得传输格式。不需要 `l2_text` 这种 JSON 数组字段。

```
JSONL 消息的一条记录 = turn_cache 的一行

列分类：
┌─ 消息字段 ─── 直接对应 JSONL 格式的属性
├─ 元数据字段 ── 消息的上下文属性，不入 JSONL
├─ CA 摘要字段 ─ CA 引擎的衍生数据（L1/L0/embedding）
└─ 内置字段 ─── SQLite 必备（主键、创建时间）
```

### 2.2 turn_cache 表完整 Schema

```sql
CREATE TABLE turn_cache (
    -- ═══════════════════════════════════════
    -- 消息字段：直接对应 JSONL 格式的属性
    -- ═══════════════════════════════════════
    role            TEXT NOT NULL,        -- "user" | "assistant" | "tool"

    -- 消息正文（3 种角色通用）
    content         TEXT,                  -- 文本内容

    -- tool 消息专用
    tool_call_id    TEXT,                  -- 关联到 tool_calls[].id
    tool_name       TEXT,                  -- tool 消息的 "name" 字段

    -- assistant 消息专用
    tool_calls_json TEXT,                  -- tool_calls 数组的 JSON 序列化
    finish_reason   TEXT,                  -- "stop" | "tool_calls" | "length"

    -- ═══════════════════════════════════════
    -- 元数据字段：消息的上下文属性，不入 JSONL
    -- ═══════════════════════════════════════
    session_id      TEXT NOT NULL,         -- 会话 ID
    turn_index      INTEGER NOT NULL,      -- 对话轮序号
    api_call_count  INTEGER NOT NULL DEFAULT 0,  -- 本 turn 内第几次 API 调用
                                              --   0   = user 行起始（最终文本用 999999）
                                              --   ≥1  = 工具组（对应第 N 次 API 调用）
    seq_index       INTEGER NOT NULL DEFAULT 0,  -- 同 api_call_count 内序号用于排序

    -- 归属数据（辅助归组 / 排错）
    api_request_id  TEXT,                  -- 工具组所属 API 调用唯一 ID
    duration_ms     INTEGER,               -- 工具执行耗时（tool 消息用）
    status          TEXT,                  -- 工具执行状态："ok" | "error" | "blocked" | "cancelled"
    error_type      TEXT,                  -- 错误类型
    error_message   TEXT,                  -- 错误描述
    usage_json      TEXT,                  -- Token 用量 JSON 字符串（assistant 消息用）

    -- ═══════════════════════════════════════
    -- CA 摘要字段：引擎的衍生摘要数据
    -- ═══════════════════════════════════════
    l1_text         TEXT,                  -- 结构化摘要 JSON：
                                          --   dialogue: {core_change, new_materials, ...}
                                          --   tool_group assistant: {group_intent, group_result, tool_count, state}
                                          --   tool: {tool_name, tool_args, result, status, ...}
    l0_text         TEXT,                  -- 单行摘要 ≤100 字符
    l0_embedding    BLOB,                  -- L0 嵌入向量
    l1_embedding    BLOB,                  -- L1 嵌入向量
    bm25_tokens     TEXT,                  -- BM25 分词
    _assemble_status    INTEGER DEFAULT 0, -- 0=成功, 1=降级, 2=永久跳过
    backfill_attempts   INTEGER DEFAULT 0, -- L-stage 尝试次数

    -- ═══════════════════════════════════════
    -- 内置字段
    -- ═══════════════════════════════════════
    created_at      TEXT DEFAULT (datetime('now')),

    -- ═══════════════════════════════════════
    -- 主键：三层路由
    -- ═══════════════════════════════════════
    PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)
);

-- 索引：按三层结构排序即可重建 JSONL 消息序列
CREATE INDEX idx_msg_order ON turn_cache
    (session_id, turn_index, api_call_count, seq_index);
```

### 2.3 一条对话的完整存储示例

**场景**：用户 `"查 /tmp/x"` → LLM 调用 read_file → 返回内容 → LLM 回复 `"文件是..."`

```
turn_index=5 的对话，共 4 条消息：

session_id="ses_abc", turn_index=5

── api_call_count=0 ───────────────────────────────────────────
  seq=0: role="user", content="查 /tmp/x"

── api_call_count=1（第 1 次 API 调用，finish_reason="tool_calls"）
  seq=0: role="assistant",
         content="我来查一下这个文件的内容...",
         tool_calls_json='[{"id":"call_A","type":"function","function":{"name":"read_file","arguments":"{\\"path\\":\\"/tmp/x\\"}"}}]',
         finish_reason="tool_calls",
         api_request_id="req_001"
  seq=1: role="tool",
         content="文件内容：hello world",
         tool_call_id="call_A",
         tool_name="read_file",
         status="ok", duration_ms=150,
         api_request_id="req_001"

── api_call_count=999999（最后一条消息）
  seq=0: role="assistant",
         content="文件内容是 hello world",
         finish_reason="stop"
```

**排序**：`ORDER BY turn_index, api_call_count, seq_index`

```
  ┌─ turn=5,api=0,seq=0: user "查 /tmp/x"
  │
  ├─ turn=5,api=1,seq=0: assistant "我来查..." (tool_calls, thought)
  ├─ turn=5,api=1,seq=1: tool "文件内容：hello world"
  │
  └─ turn=5,api=999999,seq=0: assistant "文件内容是 hello world"
```

**重建的 JSONL 传输格式**（直接逐行输出即可）：

```jsonl
{"role": "user", "content": "查 /tmp/x"}
{"role": "assistant", "content": "我来查一下这个文件的内容...",
 "tool_calls": [{"id":"call_A","function":{"name":"read_file","arguments":"{\"path\":\"/tmp/x\"}"}}]}
{"role": "tool", "tool_call_id": "call_A", "content": "文件内容：hello world"}
{"role": "assistant", "content": "文件内容是 hello world"}
```

### 2.4 三类消息的行结构详解

#### ① user 消息

| 字段 | 值 | 来源 |
|------|-----|------|
| `role` | `"user"` | 常量 |
| `content` | `"查 /tmp/x"` | `post_llm_call.user_message` |
| `turn_index` | `5` | CA 引擎维护 |
| `api_call_count` | `0` | 常量（用户消息不在任何 API 调用中） |
| `seq_index` | `0` | 常量 |

#### ② assistant 消息（工具调用）

| 字段 | 值 | 来源 |
|------|-----|------|
| `role` | `"assistant"` | 常量 |
| `content` | thought 文本 | `post_api_request.assistant_message.content` |
| `tool_calls_json` | `[{"id":"call_A", "function":{...}}]` | 从 `assistant_message.tool_calls` 重建 |
| `finish_reason` | `"tool_calls"` | `post_api_request.finish_reason` |
| `api_call_count` | `1` | `post_api_request.api_call_count`（第 1 次 API） |
| `api_request_id` | `"req_001"` | `post_api_request.api_request_id` |
| `seq_index` | `0` | 常量（每组第 1 条） |

**CA 摘要**：`l1_text` 存**工具组级摘要**（含 `group_intent`, `group_result` 和 `tools[]` 列表）

#### ③ tool 消息

| 字段 | 值 | 来源 |
|------|-----|------|
| `role` | `"tool"` | 常量 |
| `content` | `"文件内容：hello world"` | `post_tool_call.result` |
| `tool_call_id` | `"call_A"` | `post_tool_call.tool_call_id` |
| `tool_name` | `"read_file"` | `post_tool_call.tool_name` |
| `api_call_count` | `1` | `post_tool_call.api_request_id` → 查所属组 |
| `api_request_id` | `"req_001"` | `post_tool_call.api_request_id` |
| `status` | `"ok"` | `post_tool_call.status` |
| `duration_ms` | `150` | `post_tool_call.duration_ms` |
| `seq_index` | `1` | 递增（同组内按顺序） |

**CA 摘要**：`l1_text` 存**单工具摘要**（`tool_name, tool_args, result, status, ...`）

#### ④ assistant 消息（文本回复）

| 字段 | 值 | 来源 |
|------|-----|------|
| `role` | `"assistant"` | 常量 |
| `content` | `"文件内容是 hello world"` | `post_llm_call.assistant_response` |
| `finish_reason` | `"stop"` | 常量 |
| `api_call_count` | `999999` | 常量（确保排在所有工具组之后） |
| `seq_index` | `0` | 常量 |

### 2.5 字段分类总览

| 类别 | 包含字段 | 说明 |
|------|---------|------|
| **消息字段** | `role`, `content`, `tool_call_id`, `tool_name`, `tool_calls_json`, `finish_reason` | 直接映射到 JSONL，按角色可选 |
| **元数据字段** | `turn_index`, `api_call_count`, `seq_index`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message`, `usage_json` | 不入 JSONL，用于排序/归组/排错 |
| **CA 摘要字段** | `l1_text`, `l0_text`, `l0_embedding`, `l1_embedding`, `bm25_tokens`, `_assemble_status`, `backfill_attempts` | CA 引擎的衍生数据 |
| **内置字段** | `session_id`, `created_at` | SQLite 必备 |

### 2.6 消息字段 vs 元数据字段的区分原则

```python
def is_message_field(field: str) -> bool:
    """该字段是否应出现在重建的 JSONL 消息中。"""
    return field in {"role", "content", "tool_call_id", "tool_name", "tool_calls_json", "finish_reason"}
```

> **`tool_name`** 是 JSONL 中 tool 消息的 `name` 字段（OpenAI 标准）。
> **未列出的字段**（turn_index, api_call_count 等）均为元数据，不参与 JSONL 序列化。

### 2.7 摘要归组规则

不是每行都独立生成 L1/L0。摘要按**三级层级**归并：

| 层级 | 包含行 | 摘要内容 | l1_text 存储位置 |
|------|-------|---------|-----------------|
| **对话轮** | `role='user'` + `role='assistant'(api_call_count=999999)` | 现有 5 字段 `{core_change, new_materials, objective_facts, consensus, todo}` | user 行或 final assistant 行 |
| **工具组** | `role='assistant'(api_call_count=N≥1)` **一行** | `{group_intent, group_result, tool_count, state}` | 该 assistant{tc} 行 |
| **工具轮** | 每个 `role='tool'` 行自身 | `{tool_name, tool_args, result, status, error, duration_ms}` | 各 tool 行 |

---

## 三、当前实现与理想模型的差距分析

### 3.1 当前存储结构（现状）

当前 `turn_cache` 表的核心定义（`ca/store.py:39-56`）：

```sql
PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)

-- turn_type: 'dialogue' | 'tool'
-- tool_sub_index: 0=dialogue, 1..N=tool 在该轮中的序号

-- 对话轮：l2_text = [user_message, final_assistant_response]  ← 一个数组存两条消息
-- 工具轮：l2_text = [assistant{thought, all_tool_calls}, tool_response]  ← 每组每条 assistant 重复
```

**对比 §2.2 理想**：
- 理想主键：`PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)`
- 当前主键：`PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)`
- 理想每行 = 一条 JSONL 消息，字段独立
- 当前 `l2_text` = 一个 JSON 数组塞多条消息

### 3.2 差距逐项对比

#### 差距 1：消息存储粒度

| 消息 | 理想存储（§2.2） | 当前存储 |
|------|----------------|---------|
| user 消息 | 独立行 `(api=0, seq=0)` | 嵌入 dialogue 的 `l2_text[0]` |
| assistant{tc} | 独立行 `(api=N≥1, seq=0)` | 嵌入 tool 的 `l2_text[0]`，每个工具重复一次 |
| tool 响应 | 独立行 `(api=N, seq≥1)` | 独立行 `l2_text[1]`，但嵌在 `(turn_type='tool')` 中 |
| assistant{text} | 独立行 `(api=999999, seq=0)` | 嵌入 dialogue 的 `l2_text[1]` |

**后果**：当前 `l2_text` 字段是一个 JSON blob，A-stage 必须 `json.loads` 再拆开，无法直接 SQL 排序输出合法序列。

#### 差距 2：对话轮 L2 的分离

**理想**（§2.3 示例）：

```
turn=5, api=0,    seq=0: role=user,      content="查 /tmp/x"
turn=5, api=1,    seq=0: role=assistant,  content=thought, tool_calls_json=...
turn=5, api=1,    seq=1: role=tool,       content="文件内容..."
turn=5, api=999999,seq=0: role=assistant,  content="文件是..."
```

**当前**：

```
turn=5, type='dialogue', sub=0:
  l2_text = [
    {"role":"user", "content":"查 /tmp/x"},
    {"role":"assistant", "content":"文件是..."}
  ]

turn=5, type='tool', sub=1:
  l2_text = [
    {"role":"assistant", "content":thought, "tool_calls":[...]},
    {"role":"tool", "content":"文件内容..."}
  ]
```

**三个子问题**：
- **(a)** user 和 final_text 捆在同一条记录的 `l2_text` 数组中，无法独立排序
- **(b)** 重建时 `ORDER BY turn_type` 按字母序 `'dialogue' < 'tool'`，dialogue 整个数组排在最前 → **final_text 出现在 tool_group 之前**，时序错误
- **(c)** 当前 `_rebuild_messages_from_cache` 只是简单拼接 JSON 数组，不做时序重排

#### 差距 3：工具组数据结构

**理想**（§2.4 四类消息分解）：

| 行 | role | content | tool_calls_json | api_call_count | seq |
|---|------|---------|---------------|---------------|-----|
| 1 | assistant | thought | `[{id, function}]` | 1 | 0 |
| 2 | tool | result_A | — | 1 | 1 |
| 3 | tool | result_B | — | 1 | 2 |

**当前**：

| 行 | turn_type | tool_sub_index | l2_text[0] (assistant) | l2_text[1] (tool) |
|---|----------|---------------|----------------------|------------------|
| 1 | tool | 1 | `{thought, tool_calls:[A,B]}` | `{tool_A result}` |
| 2 | tool | 2 | `{thought, tool_calls:[A,B]}` | `{tool_B result}` |

**三个子问题**：
- **(a)** 同一个 `assistant{tc}` 消息被重复 N 次（N=工具数），重建得 `[asst{A,B}, tool_A, asst{A,B}, tool_B]`——**OpenAI 非法序列**
- **(b)** 没有 `api_call_count`，无法在 turn 内排序（多个工具组时无法区分先后）
- **(c)** 没有 `api_request_id`，无法按 API 调用归组

#### 差距 4：数据采集来源

**理想**（§1.7 实操穿透）：

| 数据 | 来源 Hook | 时机 |
|------|-----------|------|
| user content | `post_llm_call.user_message` | 对话轮结束 |
| assistant{tc} content(thought) + tool_calls | `post_api_request.assistant_message` | API 返回后，工具执行前 |
| tool result + status + duration | `post_tool_call` | 每个工具执行后 |
| assistant{text} content | `post_llm_call.assistant_response` | 对话轮结束 |

**当前**：

```
post_llm_call:
  conversation_history (全量历史, 含 user + 所有中间消息)
    → process_turn_async(..., messages=history_copy)
      → _run_c_stage:
          if messages:
            _extract_tool_calls(messages)  ← 遍历整个 history
            → 提取所有 assistant{tc} + tool 消息
            → 逐工具写入 DB
```

**三个子问题**：
- **(a)** 用 `post_llm_call.conversation_history` 提取工具——这是**全量历史**，不是"本轮新增"。每轮 C-stage 把历史上所有工具调用重写一遍
- **(b)** `post_api_request` 已携带精确的 `assistant_message`（thought + tool_calls），但 CA **未注册该 hook**
- **(c)** `post_tool_call` 已携带精确的 `result`、`status`、`duration_ms`，但 CA **未注册该 hook**

#### 差距 5：摘要结构（三级分离缺失）

**理想**（§2.7 三级摘要）：

| 层级 | 存储行 | 摘要内容 | 状态 |
|------|-------|---------|------|
| **工具组摘要** | `assistant{tc}` 行 | `{group_intent, group_result, tool_count, state}` | 应新增 |
| **工具轮摘要** | 各 `tool` 行 | `{tool_name, tool_args, result, status, error, duration_ms}` | 应改造 |

**当前**：

```python
# ca/__init__.py:414 — 当前只有单工具摘要，且嵌入了 thought
tool_l1, tool_l0 = self.tool_summarizer.summarize(
    turn["tool_call"], turn["tool_responses"]
)
# tool_l1 = {
#     "tool_name": "read_file",
#     "tool_args": {"path": "/tmp/x"},
#     "thought_process": "我来查一下这个文件的内容...",  ← thought 来自 assistant.content
#     "result_summary": "tmp/x (120 lines)",
#     ...
# }
```

**两个子问题**：
- **(a)** 没有工具组级摘要（`group_intent`, `group_result`），无法从工具组整体层面做升级决策
- **(b)** 工具轮摘要中嵌入了 `thought_process`，该字段是**工具组的意图**（`assistant.content`），N 个工具就有 N 份相同的 thought

### 3.3 根因总结

| # | 根因 | 影响 | 理想路径 |
|---|------|------|---------|
| 1 | 主键用 `(turn_type, tool_sub_index)` 而非 `(api_call_count, seq_index)` | 无法精确排序，消息粒度错 | 改主键 + 拆列 |
| 2 | `l2_text` 存 JSON 数组而非独立列 | 消息字段与元数据混存，需 parse 才能用 | 拆为独立列：`role`, `content`, `tool_calls_json` 等 |
| 3 | 对话轮 user 和 final_text 捆在一条记录 | 重建时序错误 | user 和 final_text 各存一行 |
| 4 | 未注册 `post_api_request`, `post_tool_call`, `pre_tool_call` | 使用 `conversation_history` 全量遍历 → 20× 膨胀 | 三钩子各司其职 |
| 5 | 当前 do not 认识 `api_request_id` | 无法按 API 调用归组 | 存储 `api_request_id` |
| 6 | 工具组摘要缺失，工具轮摘要含多余 thought_process | 无法按组决策，thought 重复 N 次 | assistant{tc} 行存组级摘要，tool 行删 thought_process |

### 3.4 实测后果

2026-06-08 会话 `20260608_103138_f7d0f8` 实测：

| 指标 | 值 | 对比理想 |
|------|-----|---------|
| 工具轮 DB 记录数 | **2,367 条** | ~116 条（实际工具调用数） |
| 按内容去重后唯一工具 | ~116 个 | 116 个 |
| 重复倍数 | **20.4×** | 1× |
| 每轮重写工具比例 | ~90%（每轮只有 ~12 个新工具） | 0（每轮只写新增） |
| DB 膨胀 | **41 MB** | ~2 MB |
| turn 36 工具子索引 | 141（实际新增 ~2 个） | 2 |

### 3.5 差距对照总表

| 维度 | 理想（第二章） | 当前 | 差距等级 |
|------|--------------|------|---------|
| 消息粒度 | 每行 = 一条 JSONL 消息 | `l2_text` JSON 数组捆多条 | 🔴 |
| 主键 | `(turn_index, api_call_count, seq_index)` | `(turn_index, turn_type, tool_sub_index)` | 🔴 |
| 消息字段 | 独立列：`role`, `content`, `tool_call_id`, `tool_name`, `tool_calls_json`, `finish_reason` | 全部埋在 `l2_text` JSON blob | 🔴 |
| 元数据字段 | 独立列：`api_call_count`, `api_request_id`, `duration_ms`, `status`, `error_*`, `usage_json` | 全部丢失或埋在 `l2_text` | 🔴 |
| 对话轮存储 | user 和 final_text 分两行 | `dialogue.l2_text = [user, final_text]` 一条记录 | 🟡 |
| 工具组存储 | assistant{tc}+tool_N 分 N+1 行，共享 `api_call_count` | 每工具 1 条 `turn_type='tool'`，assistant 重复 | 🔴 |
| 数据来源 | `post_api_request` + `post_tool_call` + `post_llm_call` | 仅 `post_llm_call.conversation_history` | 🔴 |
| `api_request_id` | 存储、用于归组 | 不存在、不认识 | 🔴 |
| thought | 仅一次，在 assistant{tc} 的 `content` 列 | 每个工具 L1 中重复 N 次 | 🟡 |
| 组级摘要 | `assistant{tc}` 行 `{group_intent, group_result, tool_count, state}` | 不存在 | 🟡 |
| 工具轮摘要 | tool 行 `{tool_name, result, status}`，无 thought_process | tool 行带 `thought_process`，字段错位 | 🟡 |
| DB 膨胀率 | O(N) | O(N×M) (M=C-stage 次数) | 🔴 |


## 四、重构整体方案

### 4.1 重构目标

基于第二章的理想数据结构和第三章的差距分析，本次重构的目标是：

| # | 目标 | 对应差距 | 理想路径 |
|---|------|---------|---------|
| 1 | **存储结构重构**：turn_cache 从 `(turn_type, sub_index)` 主键 + `l2_text` JSON blob 改为每行一条 JSONL 消息，独立列存储 | 差距 1, 2, 3 | §2.2 schema |
| 2 | **数据采集重定向**：从仅依赖 `post_llm_call.conversation_history` 改为三钩子分工 | 差距 4 | §1.7 实操穿透 |
| 3 | **摘要体系升级**：从逐工具平铺改为三级分离——对话轮 + 工具组 + 工具轮 | 差距 5 | §2.7 摘要归组 |
| 4 | **消除重复写入**：C-stage 不再遍历全量 history | — | 零重复 |
| 5 | **上下文中注入三级摘要**：当前 A-stage `_build_messages_from_plan` 仅对对话轮摘要做注入（`[~/N/0]`），工具组摘要和工具轮摘要虽然存储在 `l1_text` 中但从未被拉起注入。需实现三级摘要全部参与 `_compute_turn_plan_v2` 的升级判定和 `_build_messages_from_plan` 的上下文注入：对话轮 `[~/N/0]`、工具组 `[~/N/g]`（与对话轮同级，按 `api_call_count` 锚定）、工具轮 `[~/N/M]` | — | 三级独立注入 |

### 4.2 总体架构

```
┌─ Hermes Hook 层（数据采集）─────────────────────────────────┐
│                                                              │
│  post_api_request → engine._on_api_response() ← 工具组结构    │
│  pre_tool_call  → engine._on_pre_tool_call()  ← 预注册占位   │
│  post_tool_call → engine._on_post_tool_call() ← 执行结果填充  │
│  post_llm_call  → flush_tool_buffer() + process_turn_async() │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─ CA 引擎 Buffer 层（临时合并）──────────────────────────────┐
│                                                              │
│  _tool_buffer: Dict[api_request_id, ToolGroupBuffer]         │
│    ├── thought + tool_defs         ← post_api_request        │
│    └── results[tool_call_id]       ← post_tool_call          │
│                                                              │
│  post_llm_call 触发 flush：                                   │
│    ① 合并 → 重建多行独立记录（按 §2.2 schema）                 │
│    ② ToolSummarizer → 逐工具 L1/L0                           │
│    ③ write_turn → turn_cache                                 │
│    ④ 清空 buffer                                              │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─ turn_cache 存储层（每行 = 一条 JSONL 消息）─────────────────┐
│                                                              │
│  PRIMARY KEY (session_id, turn_index, api_call_count,        │
│               seq_index)                                      │
│  消息字段独立列：role, content, tool_call_id, tool_name,      │
│                tool_calls_json, finish_reason                 │
│  元数据独立列：api_request_id, duration_ms, status, error_*   │
│  CA 摘要字段：l1_text, l0_text, embedding, bm25_tokens        │
│                                                              │
│  ORDER BY turn_index, api_call_count, seq_index → JSONL      │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─ A-stage（三级注入）──────────────────────────────────────┐
│                                                              │
│  _rebuild_messages_from_cache → 按 (turn, api, seq) 排序     │
│  _build_tool_key_map → 适配新主键                             │
│  _compute_turn_plan_v2 → 三级决策：                          │
│    对话轮 L2/L1/L0                                          │
│    工具组 L1 → 整体升级判定（topic_boost 等）                │
│    工具轮 L2/L1/L0 → 逐工具调度（retrieved / middle）       │
│  _build_messages_from_plan → 直接取独立列                    │
│  [~/N/0]  = 对话轮摘要                                       │
│  [~/N/g]  = 工具组摘要（api_call_count≧1, seq=0）             │
│  [~/N/M]  = 工具轮摘要（seq_index≧1）                         │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 数据流：从 Hook 到 DB

以 §1.7 场景为例（一轮对话，一次工具调用）：

```
时序：post_api_request(req_001) → pre_tool_call(call_A)
      → post_tool_call(call_A, result) → post_llm_call
```

**① post_api_request(finish_reason="tool_calls") 触发**

```python
engine._on_api_response(
    api_request_id="req_001",
    assistant_message=NormalizedResponse(
        content="我来查一下这个文件的内容...",
        tool_calls=[ToolCall(id="call_A", name="read_file",
                    arguments='{"path":"/tmp/x"}')],
        finish_reason="tool_calls",
    ),
    api_call_count=1,
    turn_id="turn_001",
)
# → _tool_buffer["req_001"] = {
#     "thought": "我来查一下这个文件的内容...",
#     "tool_defs": [ToolCall(id="call_A", ...)],
#     "api_call_count": 1,
#     "turn_index": 5,
#     "results": {},
# }
```

**② pre_tool_call(call_A) 触发**

```python
engine._on_pre_tool_call(tool_call_id="call_A", api_request_id="req_001")
# → _tool_buffer["req_001"].results["call_A"] = {"completed": False}
```

**③ post_tool_call(call_A) 触发**

```python
engine._on_post_tool_call(
    tool_call_id="call_A",
    tool_name="read_file", args={"path": "/tmp/x"},
    result="文件内容：hello world",
    status="ok", duration_ms=150,
    api_request_id="req_001",
)
# → _tool_buffer["req_001"].results["call_A"] = {
#     "tool_name": "read_file", "args": {"path": "/tmp/x"},
#     "result": "文件内容：hello world",
#     "status": "ok", "duration_ms": 150,
#     "completed": True,
# }
```

**④ post_llm_call 触发 → flush_tool_buffer()**

从 buffer 重建并写入 4 条 turn_cache 记录：

```python
# 行 1 — user 消息
#   (session="ses_abc", turn=5, api=0, seq=0,
#    role="user", content="帮我查 /tmp/x 的内容")

# 行 2 — assistant{tc}（工具组行）
#   (session="ses_abc", turn=5, api=1, seq=0,
#    role="assistant", content="我来查...",
#    tool_calls_json='[{"id":"call_A","function":{...}}]',
#    finish_reason="tool_calls", api_request_id="req_001")
#    l1_text = {group_intent, group_result, tool_count, state}  ← 工具组摘要

# 行 3 — tool 行
#   (session="ses_abc", turn=5, api=1, seq=1,
#    role="tool", content="文件内容：hello world",
#    tool_call_id="call_A", tool_name="read_file",
#    status="ok", duration_ms=150, api_request_id="req_001")
#    l1_text = {tool_name, result, status}         ← 工具轮摘要

# 行 4 — final assistant 文本行
#   (session="ses_abc", turn=5, api=999999, seq=0,
#    role="assistant", content="文件内容是 hello world",
#    finish_reason="stop")

# 清空 buffer
_tool_buffer.clear()
```

**⑤ A-stage 重建**

```python
rows = db.execute("""
    SELECT role, content, tool_call_id, tool_name,
           tool_calls_json, finish_reason
    FROM turn_cache
    WHERE session_id=?
    ORDER BY turn_index, api_call_count, seq_index
""", (session_id,))

for row in rows:
    msg = {"role": row["role"]}
    if row["content"]:         msg["content"] = row["content"]
    if row["tool_call_id"]:   msg["tool_call_id"] = row["tool_call_id"]
    if row["tool_name"]:      msg["name"] = row["tool_name"]
    if row["tool_calls_json"]: msg["tool_calls"] = json.loads(row["tool_calls_json"])
    if row["finish_reason"]:  msg["finish_reason"] = row["finish_reason"]
    messages.append(msg)
```

### 4.4 摘要采集框架

post_llm_call 触发时，三种摘要的采集链路：

| 摘要层级 | 来源行 | 生成方式 | 写入位置 |
|---------|-------|---------|---------|
| **对话轮 L1** | user 行 + final assistant 行 | 现有 `_call_llm_for_l1()`（不变） | final assistant 行的 `l1_text` |
| **工具组 L1** | assistant{tc} 行 | 新增 `_generate_group_summary()`，从 `content`(thought) + 各 tool 结果合并 → `{group_intent, group_result, tool_count, state}` | 该 assistant{tc} 行的 `l1_text` |
| **工具轮 L1** | 各 tool 行 | 现有 `ToolSummarizer.summarize()`，删除 `thought_process` | 各 tool 行的 `l1_text` |

### 4.5 模块改动清单

| 模块 | 改动 | 说明 |
|------|------|------|
| `__init__.py` (插件入口) | **修改** | `register()` 注册 2 个新增 hook；新增 2 个 `_on_*` 分发函数 |
| `ca/__init__.py` (引擎) | **修改** | 新增 buffer + 4 个 handler；`process_turn_async` 去 messages 参数；`_run_c_stage` 去 `if messages:`；`_rebuild_messages_from_cache` 按新主键排序 |
| `ca/store.py` | **重构** | `_create_tables` 新 schema；`write_turn` 适配新列；`read_session` 按 `(turn, api, seq)` 排序 |
| `ca/cache.py` | **修改** | `add_tool_turn` 适配新主键 |
| `ca/tool_summarizer.py` | **修改** | 删除 `thought_process`；新增 `generate_group_summary()` |
| `ca/lstage.py` | **重写** | 按新主键读取已有 L2 数据，适配新格式 |
| `ca/ooda_parser.py` / `post_process.py` | **不变** | 对话轮 L1 逻辑不受影响 |

### 4.6 迁移策略

旧 DB 格式（`(turn_type, tool_sub_index)` 主键 + `l2_text` JSON blob）通过一次性迁移脚本升级到新 schema：

```sql
-- 旧 dialogue → 拆为 user + final_assistant 两行
INSERT INTO turn_cache_new
SELECT ... FROM turn_cache_old WHERE turn_type = 'dialogue';

-- 旧 tool → 去重 assistant{tc} + tool 行展开
INSERT INTO turn_cache_new
SELECT ... FROM turn_cache_old WHERE turn_type = 'tool';
```

**不做双向兼容**。迁移完成后旧表删除，A-stage 只读新格式。

### 4.7 旧 → 新对照总览

| 维度 | 旧 | 新 |
|------|-----|-----|
| 数据来源 | `post_llm_call.conversation_history`（全量） | `post_api_request`（结构）+ `post_tool_call`（结果）+ `post_llm_call`（文本） |
| 主键 | `(session, turn, turn_type, sub_index)` | `(session, turn, api_call_count, seq_index)` |
| 消息字段 | 全部在 `l2_text` JSON blob | 独立列：`role`, `content`, `tool_call_id`, `tool_name`, `tool_calls_json`, `finish_reason` |
| 元数据 | 丢失 | 独立列：`api_call_count`, `api_request_id`, `duration_ms`, `status`, `error_*`, `usage_json` |
| 对话轮 | user + final_text 捆一条记录 | 分两行 `api=0` + `api=999999` |
| 工具组 | per-tool 记录，assistant 重复 N 次 | assistant{tc} + tool × N 共享 `api_call_count` |
| 摘要层级 | 仅逐工具平铺 | 三级：对话轮 + 工具组 + 工具轮 |
| 上下文注入 | 对话轮 `[~/N/0]` + 工具轮 `[~/N/M]` 平铺注入 | 三级独立注入：对话轮 `[~/N/0]`、工具组 `[~/N/g]`、工具轮 `[~/N/M]` |
| 工具轮 thought | 每个工具 L1 嵌一份 | 归入工具组摘要，工具 L1 删除 |
| 工具组归组 | 不认识 `api_request_id` | 存 `api_request_id`，天然归组 |
