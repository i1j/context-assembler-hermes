# 实现技术方案 v1: 工具轮摘要流程重构

## 1. 实现策略

### 核心思路：混合钩子架构

当前 C-stage 中 `_run_c_stage` 的 `if messages:` 分支遍历**全量 conversation_history** 提取工具调用，导致每轮 20× 重复写入。

重构后：**`post_tool_call` 提供结构化工具数据（零重复） + `post_llm_call` 仅提取最后一个 assistant.tool_calls 消息的 thought（单次）→ 合并为完整 OpenAI 标准 L2 一次写入。**

```
重构前：post_llm_call → _extract_tool_calls(所有history) → 每轮写141条工具记录
重构后：post_tool_call → 缓存单个工具数据 | post_llm_call → 提取thought → 合并写完整L2
```

### 关键设计决策

| 问题 | 方案 |
|------|------|
| thought 缺失 | post_llm_call 中从 conversation_history 提取最后一个 assistant.tool_calls 的 content |
| 工具分组 | 按 api_request_id 分组，同一批 tool_calls 共享一个 assistant 消息 |
| 写入时机 | post_llm_call 中合并写入（对话轮 L2 + 工具轮 L2 一次性完成） |
| L-stage 兼容 | backfill 逻辑不变，仍从 L2 做 _extract_tool_calls（L2 格式不变） |
| A-stage 兼容 | _rebuild_messages_from_cache 不变，L2 格式完全相同 |
| 线程安全 | 引擎加 _tool_buffer_lock，per-session 原子操作 |

### 数据流

```
新流程：
post_tool_call (每工具执行完触发)
  │  tool_name + args + result + tool_call_id + api_request_id + status + duration_ms
  ▼
_on_post_tool_call (插件层)
  │  → 查 session_id → 查引擎 → engine.buffer_tool_call(...)
  │  → 按 api_request_id 分组存入 _pending_tool_buffer[turn_id]
  ▼
post_llm_call (对话轮结束触发)
  │  user_message + assistant_response + conversation_history（含thought）
  ▼
_on_post_llm_call (插件层)
  │  → 从 conversation_history 提取最后一条 assistant.tool_calls 的 content (thought)
  │  → 调用 engine.flush_tool_buffer(turn_id, thought)
  │  → 调用 engine.process_turn_async(..., messages=None)  ← 删掉 messages=history_copy
  ▼
_run_c_stage
  │  → 写入对话轮 L2/L1/L0（不变）
  │  → **不再有 `if messages:` 分支**（已删除）
  │  → 工具轮 L2 由 flush_tool_buffer 独立写入
  ▼
flush_tool_buffer (在 process_turn_async 前后或内部)
  │  重建 OpenAI 标准格式 L2:
  │  [
  │    {"role": "assistant", "content": thought, "tool_calls": [...]},
  │    {"role": "tool", "tool_call_id": "...", "content": "..."},
  │    ...
  │  ]
  │  → 对每组工具调用 ToolSummarizer.summarize() → L1/L0 → write_turn
  │  → 清空 buffer
```

---

## 2. 文件级改动清单

| 操作 | 文件路径 | 改动内容 |
|------|---------|---------|
| **修改** | `__init__.py`（项目根） | 注册 `post_tool_call` hook；新增 `_on_post_tool_call` 分发函数；新增 buffer 管理 |
| **修改** | `plugins/ca_assembler/__init__.py`（等同根 `__init__.py`） | 同上（这里是部署版本） |
| **修改** | `ca/__init__.py`（引擎） | 新增 `_pending_tool_buffer` 和 `buffer_tool_call()` / `flush_tool_buffer()` 方法；修改 `_run_c_stage` 删除 `if messages:` 分支；修改 `process_turn_async` 签名去掉 `messages` 参数 |
| **修改** | `ca/__init__.py`（引擎） | 新增 `_extract_current_tool_thought()` 从 conversation_history 提取 thought |
| **修改** | `ca/lstage.py` | 适配 `_extract_tool_calls` 移至 `_backfill_dialogue` / `_backfill_tool`（调用点不变，函数位置不变） |
| **修改** | `tests/test_plugin.py` | register 检查从 5 → 6 个 hooks；新增 `TestPostToolCall` 测试类 |
| **修改** | `tests/test_v440.py` | 适配 `process_turn_async` 签名变更；新增零重复验证 |
| **修改** | `AGENTS.md` | 更新注册接口说明；更新变更历史 |
| **修改** | `docs/changelog.md` | 记录变更 |

---

## 3. 接口设计

### 3.1 新增/修改的公共接口

#### 插件层（`__init__.py`）

```python
# 新增注册：
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call)
    ctx.register_hook("post_llm_call",    _on_post_llm_call)
    ctx.register_hook("post_tool_call",   _on_post_tool_call)  # ← 新增
```

```python
def _on_post_tool_call(**kwargs: Any) -> None:
    """每次工具调用完成后即时捕获。

    kwargs:
        session_id: str
        tool_name: str
        args: dict          # 已 parse 好的结构化参数
        result: str         # 执行结果文本
        status: str         # ok | error | cancelled
        error_type: Optional[str]
        error_message: Optional[str]
        tool_call_id: str
        api_request_id: str
        turn_id: str
        duration_ms: int
    """
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    plugin.on_post_tool_call(**kwargs)
```

#### CAContextAssemblerPlugin 类

```python
def on_post_tool_call(self, **kwargs: Any) -> None:
    """分发到引擎的 buffer_tool_call。"""
    if not self._engine:
        return
    self._engine.buffer_tool_call(
        tool_name=kwargs.get("tool_name", ""),
        args=kwargs.get("args", {}),
        result=kwargs.get("result", ""),
        status=kwargs.get("status", "ok"),
        tool_call_id=kwargs.get("tool_call_id", ""),
        api_request_id=kwargs.get("api_request_id", ""),
        duration_ms=kwargs.get("duration_ms", 0),
    )
```

#### 引擎层（`ca/__init__.py`）

```python
# 新增属性
self._pending_tool_buffer: Dict[str, List[Dict]] = {}  # api_request_id → [tools]
self._tool_buffer_lock = threading.Lock()

def buffer_tool_call(self, tool_name: str, args: dict, result: str,
                      status: str, tool_call_id: str,
                      api_request_id: str, duration_ms: int) -> None:
    """缓存单个工具调用数据，按 api_request_id 分组。"""
    with self._tool_buffer_lock:
        if api_request_id not in self._pending_tool_buffer:
            self._pending_tool_buffer[api_request_id] = []
        self._pending_tool_buffer[api_request_id].append({
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "status": status,
            "tool_call_id": tool_call_id,
            "duration_ms": duration_ms,
        })

def flush_tool_buffer(self, thought: str = "") -> int:
    """将缓存中的工具数据写入 turn_cache。

    重建 OpenAI 标准 L2 消息格式：
    - 同一 api_request_id 的工具归组到一个 assistant.tool_calls 消息
    - 每个工具对应一个 tool 响应消息
    - thought 注入 assistant.content
    - 对每组调用 ToolSummarizer.summarize() 生成 L1/L0

    返回写入的工具记录数。
    """
    ...

@staticmethod
def _extract_current_tool_thought(conversation_history: List[Dict]) -> str:
    """从 conversation_history 提取最后一个 assistant.tool_calls 的 content（thought）。

    从末尾向前扫描，找到第一个含 tool_calls 的 assistant 消息，
    返回其 content 字段（可能为空字符串）。
    """
    ...
```

#### `process_turn_async` 签名变更

```python
# 旧:
def process_turn_async(self, user_message, assistant_response,
                        conversation_history=None, messages=None) -> int:

# 新:
def process_turn_async(self, user_message, assistant_response,
                        conversation_history=None) -> int:
    # 已删除 messages 参数
    # 工具轮写入由 flush_tool_buffer 独立完成
```

#### `_run_c_stage` 变更

```python
# 旧: 参数含 messages，函数内有 if messages: 分支
def _run_c_stage(self, session_id, turn_index, prev_l1, l2_text,
                  token_offset, messages=None, ...):

# 新: 删除 messages 参数和 if messages: 分支
def _run_c_stage(self, session_id, turn_index, prev_l1, l2_text,
                  token_offset, ...):
    # 工具轮不再在此写入
```

### 3.2 L2 重建格式

从 buffer 重建的 OpenAI 标准格式：

```python
# 输入 buffer (同一 api_request_id)
[
    {"tool_name": "read_file", "args": {"path": "/x"}, "result": "内容...",
     "status": "ok", "tool_call_id": "call_1"},
    {"tool_name": "search_files", "args": {"pattern": "*.py"}, "result": "[a.py]",
     "status": "ok", "tool_call_id": "call_2"},
]

# 重建 L2（OpenAI 标准）
[
    {
        "role": "assistant",
        "content": thought,  # 从 conversation_history 提取
        "tool_calls": [
            {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path":"/x"}'}},
            {"id": "call_2", "function": {"name": "search_files", "arguments": '{"pattern":"*.py"}'}},
        ]
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "内容..."},
    {"role": "tool", "tool_call_id": "call_2", "content": "[a.py]"},
]
```

### 3.3 ToolSummarizer 适配

当前 `ToolSummarizer.summarize()` 签名接受 `(tool_call_msg: Dict, tool_responses: List[Dict])`，其中 `tool_call_msg` 的 `function.arguments` 可能是 dict 或 JSON string。

新增适配路径：`flush_tool_buffer` 在调用 summarize 前，先将 buffer 中的结构化数据重建为 OpenAI 标准格式，再传入 summarize，**不修改 ToolSummarizer 本身**。

---

## 4. 关键实现细节

### 4.1 时序控制

post_tool_call 和 post_llm_call 的触发顺序：
```
post_tool_call → ... → post_tool_call → post_llm_call
（N 次工具调用）                        （对话轮结束）
```

CA 引擎在 `_on_post_tool_call` 中只 buffer 不入库。`_on_post_llm_call` 中：
1. 调用 `flush_tool_buffer(thought)` 写工具轮 → 写入 turn_cache
2. 调用 `process_turn_async(...)` 写对话轮 → 写入 turn_cache
3. 此时 turn_cache 中已有完整数据 → A-stage 重建正常

### 4.2 turn_index 映射

`post_tool_call` 的 `turn_id` 是 Hermes 层面的对话轮 ID。CA 需要映射到自己的 `turn_index`。

方案：在 `_on_post_llm_call` 时设置 `self._active_turn_index`（当前对话轮的 CA turn_index），`_on_post_tool_call` 读取该值。中间如果有多个 post_tool_call 触发，它们共享同一个 active_turn_index。

边界：post_llm_call 清空 active_turn_index 防止越界污染。

### 4.3 `_extract_current_tool_thought` 实现

```python
@staticmethod
def _extract_current_tool_thought(conversation_history: List[Dict]) -> str:
    """从末尾找第一个 assistant.tool_calls 消息的 content。"""
    for msg in reversed(conversation_history):
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            return msg.get("content", "") or ""
    return ""
```

### 4.4 异常处理

| 场景 | 处理 |
|------|------|
| post_tool_call 触发但无活跃引擎 | 静默丢弃（`_engines` 中查不到 session） |
| post_tool_call 触发但无活跃 turn_index | buffer 但标记待定，post_llm_call 时补填 |
| 同工具重复调用（retry） | 不同 tool_call_id → 不同记录；同一 id → INSERT OR REPLACE |
| L-stage 回填 | 不变，仍从已存的 L2 提取 |
| 工具结果超大 | result 截断至 Config.TOOL_RESULT_MAX_LENGTH（默认 10000 字符） |

### 4.5 向后兼容

| 影响面 | 措施 |
|--------|------|
| DB schema | 不变，turn_cache 已有 `turn_type='tool'` 和 `tool_sub_index` |
| A-stage 重建 | 不变，_rebuild_messages_from_cache 按 turn_index 顺序拼接 L2 |
| L-stage backfill | 不变，已有 L2 格式完全一致 |
| _extract_tool_calls | 保留该静态方法，L-stage 继续使用 |
| pre_llm_call (A-stage) | 不变 |

---

## 5. 接口契约（供测试 Agent 使用）

| 接口 | 签名 | 预期行为 |
|------|------|---------|
| `buffer_tool_call()` | `(tool_name, args, result, status, tool_call_id, api_request_id, duration_ms)` | 缓存到 `_pending_tool_buffer[api_request_id]`，线程安全 |
| `flush_tool_buffer(thought)` | `(thought: str) → int` | 重建 L2 → summarize → write_turn → 清空 buffer，返回写入记录数 |
| `_extract_current_tool_thought(history)` | `(List[Dict]) → str` | 从末尾找 assistant.tool_calls 的 content |
| `process_turn_async()` | `(user_message, assistant_response, conversation_history) → int` | 删除 messages 参数，工具轮已独立写入 |
| `_run_c_stage()` | `(..., without messages) → None` | 删除 `if messages:` 分支 |
| `_on_post_tool_call(**kwargs)` | 接收 Hermes hook 参数 | 分发到引擎 buffer_tool_call |
| `on_post_tool_call(**kwargs)` | 插件类方法 | 检查引擎可用性后调用 buffer_tool_call |

---

## 6. 实施步骤

### Step 1: 引擎新增 buffer + flush 方法
```bash
python -c "from ca import ContextAssembler; print('buffer methods OK')"
```

### Step 2: 新增 _extract_current_tool_thought
```bash
python -c "from ca import ContextAssembler; ca=ContextAssembler(':memory:','test'); print(ca._extract_current_tool_thought([]))"
```

### Step 3: 修改 _run_c_stage 删除 messages 分支
```bash
python -m pytest tests/test_c.py -v -k "test_c_stage"
```

### Step 4: 修改 process_turn_async 签名
```bash
python -m pytest tests/test_v440.py::TestToolTurnCStage -v
```

### Step 5: 插件层注册 post_tool_call hook
```bash
python -m pytest tests/test_plugin.py -v
```

### Step 6: 适配 _on_post_llm_call 调用 flush
```bash
python -m pytest tests/ -v --ignore=tests/test_system.py
```

### Step 7: 适配 L-stage 保持 _extract_tool_calls
```bash
python -m pytest tests/test_v440.py::TestLStageBackfill -v
```

### Step 8: 全量测试
```bash
python -m pytest tests/ -v --ignore=tests/test_system.py 2>&1 | tail -20
```

## 自检
- [x] L2 按 OpenAI 标准格式保存（含 thought 在 assistant.content）
- [x] 零重复：每个工具调用只写入一次 DB
- [x] A-stage 重建不需要修改
- [x] L-stage backfill 不需要修改
- [x] ToolSummarizer 不需要修改（适配层在 flush 中完成）
- [x] 断路器/生命周期不受影响
- [x] 时序：post_tool_call 先到 → buffer → post_llm_call 时 flush
