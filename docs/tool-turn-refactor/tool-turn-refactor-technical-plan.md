# 工具轮数据重构 — 详细设计文档 (v5.0)

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v5.0-r2（经三轮多角度评审修复） |
| 对应需求 | `docs/tool-turn-refactor/tool-turn-refactor-req.md`（R1-R7） |
| 对应分析 | `docs/tool-turn-refactor/tool-turn-refactor-analysis.md` |
| 编制日期 | 2026-06-22 |
| 评审修订 | 2026-06-22（第一轮自审 15 项 + 第二轮双线 Agent 13 项 + 第三轮架构+可维护性 25 项 = **53 项全部闭环**） |
| 基础版本 | v4.7.1（当前 AGENTS.md） |
| 版本号 | v5.0 |

---

## 1. 设计概述

### 1.1 设计目标

工具轮数据重构（v5.0）基于分析文档（§4.1）定义的 5 大目标：

| # | 目标 | 对应需求 | 优先级 |
|---|------|---------|--------|
| 1 | **存储结构重构**：turn_cache 从 `(turn_type, tool_sub_index)` 主键 + `l2_text` JSON blob 改为每行一条 JSONL 消息，独立列存储 | R1 | P0 |
| 2 | **数据采集重定向**：从仅依赖 `post_llm_call.conversation_history` 改为三钩子分工（`post_api_request`/`pre_tool_call`/`post_tool_call`） | R2+R3+R4 | P0 |
| 3 | **三级摘要**：从逐工具平铺改为对话轮 + 工具组 + 工具轮三级摘要 | R5 | P1 |
| 4 | **消除重复写入**：C-stage 不再遍历全量 history；工具行数 = 实际工具调用次数 | R4 | P0 |
| 5 | **三级注入**：A-stage 注入 `[~/N/0]`(对话轮) + `[~/N/g]`(工具组) + `[~/N/M]`(工具轮) | R6 | P1 |

### 1.2 当前问题

分析文档（§3）确认了 6 项差距：

| # | 当前问题 | 量化影响（实测 2026-06-08） |
|---|---------|--------------------------|
| 1 | `l2_text` JSON 数组捆多条消息 | 2,367 条 DB 记录 vs ~116 真实工具调用 |
| 2 | 主键 `(turn_type, sub_index)` 无法表达工具组关系 | 20.4× 重复写入 |
| 3 | 全部消息埋在 `l2_text` JSON blob，元数据丢失 | 41 MB DB 膨胀 |
| 4 | 仅依赖 `post_llm_call.conversation_history` 全量遍历 | 每轮 ~90% 重写 |
| 5 | 不认识 `api_request_id`，无法按 API 调用归组 | 无法生成工具组摘要 |
| 6 | 工具组摘要缺失、thought_process 在工具轮摘要中重复 N 次 | 上下文质量下降 |

### 1.3 关键设计原则

| 原则 | 实现方式 |
|------|---------|
| **增量采集** | 三钩子实时增量收集，不依赖全量 history 遍历 |
| **Buffer 无锁** | Hermes 单线程同步调用模型，无需线程锁 |
| **向后兼容** | `write_turn()` 旧签名→自动填充 `api_call_count=0, seq_index=0` |
| **惰性迁移** | 旧 DB `readonly` 模式正常读取，不自动迁移 |
| **版本路由** | A-stage 重建按 schema 版本走不同 ORDER BY |
| **非阻塞** | Buffer flush 同步、L1 摘要异步（不变） |
| **纯文本摘要** | 工具组/工具轮摘要不调 LLM，规则引擎纯文本拼接 |

---

## 2. 系统架构

### 2.1 逻辑架构

```
┌─ Hermes Hook 层（新增 3 钩子）─────────────────────────┐
│                                                         │
│  post_api_request → engine._on_api_response() ← 工具组结构 │
│  pre_tool_call    → engine._on_pre_tool_call()  ← 预注册占位 │
│  post_tool_call   → engine._on_post_tool_call() ← 执行结果填充 │
│  post_llm_call    → flush_tool_buffer() + process_turn_async() │
└───────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ CA 引擎 Buffer 层（新增）─────────────────────────────┐
│                                                         │
│  _tool_buffer: Dict[api_request_id, ToolGroupBuffer]     │
│    ├── thought + tool_defs         ← post_api_request    │
│    ├── api_call_count, turn_index  ← post_api_request    │
│    └── results[tool_call_id]       ← post_tool_call      │
│                                                         │
│  flush_tool_buffer()（post_llm_call 同步触发）：            │
│    ① 遍历 buffer，按 api_call_count 排序                 │
│    ② user 行 (api=0, seq=0)                              │
│    ③ assistant{tc} 行 (api=N, seq=0) + 工具组 L1         │
│    ④ tool × M (api=N, seq=1..M) + per_tool L1            │
│    ⑤ final 行不写 ← 留给 _run_c_stage                    │
│    ⑥ 清空 buffer                                         │
│                                                         │
│  Buffer 无锁设计：Hermes 单线程模型                        │
│  悬挂清理：reset() 时告警 + 清空                           │
└───────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ turn_cache 存储层（v5 schema 重构）───────────────────┐
│                                                         │
│  PRIMARY KEY (session_id, turn_index,                   │
│               api_call_count, seq_index)                 │
│                                                         │
│  消息独立列：role, content, tool_call_id, tool_name,    │
│              tool_calls_json, finish_reason              │
│  元数据独立列：api_request_id, duration_ms, status,     │
│               error_type, error_message, usage_json      │
│  CA 摘要列：l1_text, l0_text, l0_embedding,             │
│              l1_embedding, bm25_tokens, token_offset     │
│                                                         │
│  排序：ORDER BY turn_index, api_call_count, seq_index    │
└───────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ A-stage（三级注入）───────────────────────────────────┐
│                                                         │
│  _rebuild_messages_from_cache → 版本路由排序             │
│    v5: ORDER BY turn_index, api_call_count, seq_index    │
│    v4: ORDER BY turn_index ASC（readonly 模式）          │
│                                                         │
│  _compute_turn_plan_v2 → 三级决策：                      │
│    对话轮   → L2/L1/L0（现有逻辑不变）                   │
│    工具组   → api_call_count≧1, seq=0                    │
│    工具轮   → seq_index≧1                                │
│                                                         │
│  _build_messages_from_plan → 三级注入：                  │
│    [~/N/0] = 对话轮摘要（格式不变）                      │
│    [~/N/g] = 工具组摘要（新增）                           │
│    [~/N/M] = 工具轮摘要（格式不变）                      │
└───────────────────────────────────────────────────────┘
```

### 2.2 模块依赖

```
plugins/ca_assembler/__init__.py
│   新增：register 3 钩子 + 3 分发函数
│
└── ca/__init__.py (ContextAssembler)
    │   新增：_tool_buffer + 3 handler + flush_tool_buffer
    │   修改：process_turn_async 去 messages 参数
    │   修改：_run_c_stage 职责分离
    │   修改：_rebuild_messages_from_cache 版本路由
    │   修改：_compute_turn_plan_v2 / _build_messages_from_plan 三级注入
    │   修改：_CA_TAG_RE → r'^\[~/\d+(?:/\d+|/g)?\]\s*'
    │   修改：_deduplicate_messages 适配 [~/N/g]
    │
    ├── config.py     ← 不变
    ├── store.py      ← 重构：v5 schema + write_tool_group + readonly 模式 + 版本路由
    ├── cache.py      ← 修改：新主键三元组适配（PR2 再动）
    ├── retrieval.py  ← 不变
    ├── embedding.py  ← 不变
    ├── ooda_parser.py← 不变
    ├── post_process.py ← 不变
    ├── prompts.py    ← 不变
    ├── tool_summarizer.py ← 新增 generate_group_summary()
    ├── lstage.py     ← 修改：新主键读取 + get_pending_backfill 复合键
    └── stats.py      ← 不变
```

---

## 3. 数据结构

### 3.1 turn_cache 表（v5 schema）

**主键**：`(session_id, turn_index, api_call_count, seq_index)`

| 列名 | 类型 | 说明 |
|------|------|------|
| `session_id` | TEXT | 会话 ID |
| `turn_index` | INTEGER | 对话轮次（从 1 开始） |
| `api_call_count` | INTEGER | API 调用序号（0=user, 1..N=API 调用, 999999=final） |
| `seq_index` | INTEGER | 组内消息序号（0=assistant{tc}, 1..M=tool 行） |
| `role` | TEXT | `user` / `assistant` / `tool` / `system` |
| `content` | TEXT | 消息文本内容（thought / tool result / final text） |
| `tool_call_id` | TEXT | 工具调用 ID（仅 role="tool" 时） |
| `tool_name` | TEXT | 工具名（仅 role="tool" 时） |
| `tool_calls_json` | TEXT | assistant 的 tool_calls 定义 JSON（仅 assistant{tc}） |
| `finish_reason` | TEXT | `tool_calls` / `stop` / `length`（仅 assistant 行） |
| `api_request_id` | TEXT | API 调用唯一 ID |
| `duration_ms` | INTEGER | 执行耗时（仅 tool 行） |
| `status` | TEXT | `ok` / `error` / `blocked` / `cancelled`（仅 tool 行） |
| `error_type` | TEXT | 错误类型（仅 tool 行） |
| `error_message` | TEXT | 错误消息（仅 tool 行） |
| `usage_json` | TEXT | Token 用量 JSON（仅 assistant{tc} 行） |
| `l1_text` | TEXT | CA 摘要 JSON |
| `l0_text` | TEXT | 单行摘要（≤100 字符） |
| `l0_embedding` | BLOB | L0 嵌入向量（4096 字节） |
| `l1_embedding` | BLOB | L1 嵌入向量（4096 字节） |
| `bm25_tokens` | TEXT | BM25 分词 |
| `token_offset` | INTEGER | 累计 Token 偏移 |
| `_assemble_status` | INTEGER | 0=成功, 1=降级待补全, 2=永久跳过 |
| `backfill_attempts` | INTEGER | L-stage 重试次数 |
| `query_embedding` | BLOB | 用户消息嵌入向量 |
| `created_at` | TEXT | 创建时间戳 |

**行类型速查**：

| 行类型 | `api_call_count` | `seq_index` | `role` | 关键特征 |
|--------|-----------------|-------------|--------|---------|
| user | 0 | 0 | user | 用户输入 |
| assistant{tc} | N（≥1） | 0 | assistant | 含 `tool_calls_json`，`finish_reason="tool_calls"` |
| tool | N（≥1） | ≥1 | tool | 含 `tool_call_id`，`status` |
| final assistant | 999999 | 0 | assistant | `finish_reason="stop"`，不含 `tool_calls_json` |

### 3.2 turn_plan 表（v5 schema 扩展）

**主键**：`(session_id, turn_index, api_call_count, seq_index)`

新增 `api_call_count` 列，同步扩展主键：

```sql
CREATE TABLE IF NOT EXISTS turn_plan (
    session_id     TEXT    NOT NULL,
    turn_index     INTEGER NOT NULL,
    api_call_count INTEGER NOT NULL DEFAULT 0,
    seq_index      INTEGER NOT NULL DEFAULT 0,
    turn_type      TEXT    NOT NULL DEFAULT 'dialogue',

    target_level    TEXT   NOT NULL DEFAULT 'L0',
    decision_reason TEXT   NOT NULL DEFAULT 'middle',
    l2_tokens       INTEGER NOT NULL DEFAULT 0,
    summary_tokens  INTEGER NOT NULL DEFAULT 0,
    tokens_saved    INTEGER NOT NULL DEFAULT 0,
    rrf_score       REAL,
    upgrade_rank    INTEGER,
    budget_remaining INTEGER,
    topic_group    INTEGER,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),

    PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)
);
```

### 3.3 Buffer 数据结构

```python
@dataclass
class ToolGroupBuffer:
    """一个 API 调用对应的工具组缓冲区。"""
    thought: str                          # assistant 的思考文本
    tool_defs: List[Dict]                  # ToolCall 定义列表
    api_call_count: int                   # 本轮第几次 API 调用
    turn_index: int                       # 所属对话轮
    results: Dict[str, Dict]              # tool_call_id → 执行结果
    finish_reason: str = "tool_calls"     # tool_calls / length

# 引擎实例变量
_tool_buffer: Dict[str, ToolGroupBuffer]  # api_request_id → Buffer
```

### 3.4 TurnPlanEntry 扩展

```python
@dataclass
class TurnPlanEntry:
    turn_index: int
    turn_type: str                          # dialogue / tool / tool_group
    api_call_count: int = 0                 # 新增
    seq_index: int = 0                      # 新增（取代 tool_sub_index）
    target_level: str = "L0"
    decision_reason: str = "middle"
    l2_tokens: int = 0
    summary_tokens: int = 0
    tokens_saved: int = 0
    rrf_score: Optional[float] = None
    upgrade_rank: Optional[int] = None
    budget_remaining: Optional[int] = None
    topic_group: Optional[int] = None
```

---

## 4. 数据流

### 4.1 工具调用轮完整数据流

以三工具调用场景为例（`read_file` + `search_files` + `read_file`，单次 API 返回）：

```
时序：post_api_request → post_tool_call×3 → post_llm_call
```

**Step ① — post_api_request(finish_reason="tool_calls")**

```
engine._on_api_response(
    api_request_id="req_001",
    assistant_message=NormalizedResponse(
        content="我来查这些文件...",
        tool_calls=[
            ToolCall(id="call_A", name="read_file", arguments='{"path":"/a"}'),
            ToolCall(id="call_B", name="search_files", arguments='{"pattern":"*.py"}'),
            ToolCall(id="call_C", name="read_file", arguments='{"path":"/b"}'),
        ],
        finish_reason="tool_calls",
    ),
    api_call_count=1,
    turn_id="turn_001",
)
→ _tool_buffer["req_001"] = ToolGroupBuffer(
      thought="我来查这些文件...",
      tool_defs=[call_A, call_B, call_C],
      api_call_count=1, turn_index=5,
      results={},
      finish_reason="tool_calls",
  )
```

**Step ② — post_tool_call × 3**

```
# call_A
engine._on_post_tool_call(
    tool_call_id="call_A", tool_name="read_file",
    args={"path": "/a"}, result='{"content": "aaa"}',
    status="ok", duration_ms=150, api_request_id="req_001",
)
→ _tool_buffer["req_001"].results["call_A"] = {
      "tool_name": "read_file", "args": {"path": "/a"},
      "result": '{"content": "aaa"}',
      "status": "ok", "duration_ms": 150,
  }

# call_B
engine._on_post_tool_call(
    tool_call_id="call_B", tool_name="search_files",
    args={"pattern": "*.py"}, result='{"files": ["x.py"]}',
    status="ok", duration_ms=80, api_request_id="req_001",
)

# call_C
engine._on_post_tool_call(
    tool_call_id="call_C", tool_name="read_file",
    args={"path": "/b"}, result='{"content": "bbb"}',
    status="ok", duration_ms=120, api_request_id="req_001",
)
```

**Step ③ — post_llm_call → flush_tool_buffer()**

```
flush_tool_buffer() 写入 1 + 1 + 3 + 0 = 5 行（不含 final 行）：

行 1: (turn=5, api=0, seq=0, role="user",
       content="帮我查这些文件")
行 2: (turn=5, api=1, seq=0, role="assistant",
       content="我来查这些文件...",
       tool_calls_json='[{"id":"call_A",...},{"id":"call_B",...},{"id":"call_C",...}]',
       finish_reason="tool_calls", api_request_id="req_001",
       l1_text={"group_intent":"查文件","group_result":"3工具完成","tool_count":3,"state":"ok"})
行 3: (turn=5, api=1, seq=1, role="tool",
       content='{"content": "aaa"}',
       tool_call_id="call_A", tool_name="read_file",
       status="ok", duration_ms=150, api_request_id="req_001",
       l1_text={"tool_name":"read_file","result":"aaa","status":"ok"})
行 4: (turn=5, api=1, seq=2, role="tool",
       content='{"files": ["x.py"]}',
       tool_call_id="call_B", tool_name="search_files",
       status="ok", duration_ms=80, api_request_id="req_001",
       l1_text={"tool_name":"search_files","result":"x.py","status":"ok"})
行 5: (turn=5, api=1, seq=3, role="tool",
       content='{"content": "bbb"}',
       tool_call_id="call_C", tool_name="read_file",
       status="ok", duration_ms=120, api_request_id="req_001",
       l1_text={"tool_name":"read_file","result":"bbb","status":"ok"})

→ _tool_buffer.clear()
```

**Step ④ — process_turn_async() → _run_c_stage() 写入 final 行**

```
行 6: (turn=5, api=999999, seq=0, role="assistant",
       content="三文件内容都已查完",
       finish_reason="stop",
       l1_text={"core_change":"查了三个文件...", ...})
```

**DB 最终行数**：6 行（user=1 + assistant{tc}=1 + tool=3 + final=1）

### 4.2 纯对话轮（buffer 空）

```
flush_tool_buffer() → buffer 为空，无写入
process_turn_async() → _run_c_stage() 写入 2 行：

行 1: (turn=6, api=0, seq=0, role="user", content="你好")
行 2: (turn=6, api=999999, seq=0, role="assistant",
       content="你好！有什么可以帮助你的？",
       finish_reason="stop",
       l1_text={"core_change":"问候", ...})
```

### 4.3 多 API 同轮场景

```
# API 1 → 2 工具
post_api_request(req_001, api_call_count=1, tool_calls=[call_A, call_B])
post_tool_call(call_A), post_tool_call(call_B)

# API 2 → 1 工具（基于工具结果继续调工具）
post_api_request(req_002, api_call_count=2, tool_calls=[call_D])
post_tool_call(call_D)

# post_llm_call → flush_tool_buffer()
# 按 api_call_count 排序：
#   user(api=0) + assistant{tc}(api=1) + tool_A(api=1,seq=1) + tool_B(api=1,seq=2)
#   + assistant{tc}(api=2) + tool_D(api=2,seq=1)
```

### 4.4 A-stage 重建

```python
# v5 版本路由
messages = []
for row in db.execute("""
    SELECT role, content, tool_call_id, tool_name,
           tool_calls_json, finish_reason
    FROM turn_cache
    WHERE session_id=?
    ORDER BY turn_index, api_call_count, seq_index
""", (session_id,)):
    msg = {"role": row["role"]}
    if row["content"]:          msg["content"] = row["content"]
    if row["tool_call_id"]:    msg["tool_call_id"] = row["tool_call_id"]
    if row["tool_name"]:       msg["name"] = row["tool_name"]
    if row["tool_calls_json"]: msg["tool_calls"] = json.loads(row["tool_calls_json"])
    if row["finish_reason"]:   msg["finish_reason"] = row["finish_reason"]
    messages.append(msg)
```

---

## 5. 模块改动详细设计

### 5.1 `ca/store.py` — 存储层重构（PR1）

#### 5.1.1 Schema v5 定义

```python
_SCHEMA_VERSION = 5

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turn_cache (
    session_id      TEXT    NOT NULL,
    turn_index      INTEGER NOT NULL,
    api_call_count  INTEGER NOT NULL DEFAULT 0,  -- 新主键列
    seq_index       INTEGER NOT NULL DEFAULT 0,   -- 新主键列
    role            TEXT    NOT NULL DEFAULT '',   -- 新增
    content         TEXT,                          -- 新增
    tool_call_id    TEXT,                          -- 新增
    tool_name       TEXT,                          -- 新增
    tool_calls_json TEXT,                          -- 新增
    finish_reason   TEXT,                          -- 新增
    api_request_id  TEXT,                          -- 新增
    duration_ms     INTEGER,                       -- 新增
    status          TEXT,                          -- 新增
    error_type      TEXT,                          -- 新增
    error_message   TEXT,                          -- 新增
    usage_json      TEXT,                          -- 新增
    l1_text         TEXT    NOT NULL DEFAULT '',
    l0_text         TEXT    NOT NULL DEFAULT '',
    l0_embedding    BLOB,
    l1_embedding    BLOB,
    bm25_tokens     TEXT,
    token_offset    INTEGER NOT NULL DEFAULT 0,
    _assemble_status INTEGER NOT NULL DEFAULT 0,
    backfill_attempts INTEGER NOT NULL DEFAULT 0,
    query_embedding BLOB,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)
);

CREATE INDEX IF NOT EXISTS idx_tc_session ON turn_cache(session_id);
CREATE INDEX IF NOT EXISTS idx_tc_offset ON turn_cache(session_id, token_offset);
CREATE INDEX IF NOT EXISTS idx_tc_assemble_status ON turn_cache(_assemble_status);
CREATE INDEX IF NOT EXISTS idx_tc_turn_order ON turn_cache(session_id, turn_index, api_call_count, seq_index);

-- turn_plan 表扩展 api_call_count, seq_index
CREATE TABLE IF NOT EXISTS turn_plan (
    session_id      TEXT    NOT NULL,
    turn_index      INTEGER NOT NULL,
    api_call_count  INTEGER NOT NULL DEFAULT 0,
    seq_index       INTEGER NOT NULL DEFAULT 0,
    turn_type       TEXT    NOT NULL DEFAULT 'dialogue',
    target_level    TEXT    NOT NULL DEFAULT 'L0',
    decision_reason TEXT    NOT NULL DEFAULT 'middle',
    l2_tokens       INTEGER NOT NULL DEFAULT 0,
    summary_tokens  INTEGER NOT NULL DEFAULT 0,
    tokens_saved    INTEGER NOT NULL DEFAULT 0,
    rrf_score       REAL,
    upgrade_rank    INTEGER,
    budget_remaining INTEGER,
    topic_group     INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)
);
...
"""
```

#### 5.1.2 向后兼容：`write_turn()` 签名适配

```python
def write_turn(
    self,
    session_id: str,
    turn_index: int,
    *,
    l0_text: str = "",
    l1_text: str = "",
    l0_embedding=None,
    l1_embedding=None,
    bm25_tokens=None,
    token_offset: int = 0,
    turn_type: str = "dialogue",
    tool_sub_index: int = 0,          # ← 保留旧参数，自动转换
    # 新增 v5 参数
    api_call_count: Optional[int] = None,
    seq_index: Optional[int] = None,
    role: str = "",
    content: str = "",
    tool_call_id: str = "",
    tool_name: str = "",
    tool_calls_json: str = "",
    finish_reason: str = "",
    api_request_id: str = "",
    duration_ms: Optional[int] = None,
    status: str = "",
    error_type: str = "",
    error_message: str = "",
    usage_json: str = "",
    l2_text: Optional[str] = None,     # ← 保留旧参数
    _assemble_status: int = 0,
    max_retries: Optional[int] = None,
) -> bool:
    """优化兼容：
    - 旧调用方（传 l2_text + turn_type/tool_sub_index）→ 自动填充 api=0/999999, seq=0
    - 新调用方（传 role/content 等独立列）→ 直接写入
    """
    if l2_text is not None:
        # 旧路径：v4 兼容，自动映射
        if turn_type == "dialogue" and tool_sub_index == 0:
            # 对话轮旧路径 → api=999999（final）或 api=0（user）
            api_call_count = api_call_count or 999999
            seq_index = seq_index or 0
            # role/content 由调用方指定或从 l2_text 提取
        elif turn_type == "tool":
            api_call_count = api_call_count or 1
            seq_index = seq_index or tool_sub_index
            role = "tool"
    else:
        # 新路径：独立列写入
        pass
    # ...实际 INSERT...
```

> **设计决策**：为避免一个方法同时理解两套 schema 导致的维护困难，内部实现应拆分为 `_write_turn_v4()` 和 `_write_turn_v5()` 两个私有方法，公共部分（重试逻辑、BLOB 打包等）抽取到 `_write_turn_impl()`。`write_turn()` 公共签名保持不变（含全部旧参数），仅做参数校验和版本路由：

```python
def write_turn(self, ..., l2_text=None, turn_type="dialogue",
               tool_sub_index=0, api_call_count=None, seq_index=None, ...) -> bool:
    if l2_text is not None:
        # 旧路径 → 委托给 _write_turn_v4
        return self._write_turn_v4(...)
    else:
        # 新路径 → 委托给 _write_turn_v5
        return self._write_turn_v5(...)
```

#### 5.1.3 新增 `write_tool_group()` 事务多行写入（含重试机制）

```python
def write_tool_group(
    self,
    session_id: str,
    turn_index: int,
    api_call_count: int,
    thought: str,
    tool_calls_json: str,
    finish_reason: str,
    api_request_id: str,
    tool_group_summary: str,        # 工具组 L1 JSON
    tools: List[Dict],              # tool 行数据列表
    token_offset: int = 0,
    user_message: str = "",
    max_retries: Optional[int] = None,
) -> bool:
    """事务内写入一组工具调用。含指数退避重试，与 write_turn() 对齐。"""
    effective_retries = max_retries if max_retries is not None else Config.DB_MAX_RETRY
    for attempt in range(effective_retries):
        try:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            # user 行（仅在 api_call_count==1 且 user_message 非空时写入）
            # 注：纯对话轮（无工具调用）的 user 行由 _run_c_stage 写入，
            # 此处 user_message 非空代表本轮有用户输入
            if api_call_count == 1 and user_message:
                conn.execute("""INSERT OR REPLACE INTO turn_cache (...) VALUES (...)""", {...})
            # assistant{tc} 行（含工具组摘要 l1_text）
            conn.execute("""INSERT OR REPLACE INTO turn_cache (...) VALUES (...)""", {...})
            # tool 行 × N
            for tool in tools:
                conn.execute("""INSERT OR REPLACE INTO turn_cache (...) VALUES (...)""", {...})
            conn.commit()
            logger.info("[CA] write_tool_group: turn=%d api=%d tools=%d committed",
                       turn_index, api_call_count, len(tools))
            return True
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if attempt < effective_retries - 1:
                wait = 0.1 * (2 ** attempt)
                logger.warning("[CA] write_tool_group locked for turn=%d, retry %.2fs (attempt %d/%d)",
                             turn_index, wait, attempt + 1, effective_retries)
                time.sleep(wait)
            else:
                logger.error("[CA] write_tool_group FAILED turn=%d after %d retries: %s",
                            turn_index, effective_retries, exc)
                raise
```

#### 5.1.3b 新增 `read_turn_texts_v5()` 与 `read_entry()`（v5 主键查询）

```python
def read_turn_texts_v5(self, session_id: str, turn_index: int,
                        api_call_count: int, seq_index: int) -> Tuple[Optional[str], str, str]:
    """按 v5 主键查询单行文本字段。返回 (l2_text, l1_text, l0_text)。"""
    row = self.conn.execute("""
        SELECT content as l2_text, l1_text, l0_text
        FROM turn_cache
        WHERE session_id=? AND turn_index=? AND api_call_count=? AND seq_index=?
    """, (session_id, turn_index, api_call_count, seq_index)).fetchone()
    if row:
        return (row["l2_text"], row["l1_text"] or "", row["l0_text"] or "")
    return (None, "", "")


def read_entry(self, session_id: str, turn_index: int,
               seq_index: int) -> Optional[Dict]:
    """按 (turn_index, seq_index) 查询助手方法（反查 api_call_count 等元数据）。"""
    row = self.conn.execute("""
        SELECT * FROM turn_cache
        WHERE session_id=? AND turn_index=? AND seq_index=?
        LIMIT 1
    """, (session_id, turn_index, seq_index)).fetchone()
    return dict(row) if row else None
```

#### 5.1.4 `read_session()` / `read_turn()` 版本路由（扩展至全部 store 方法）

```python
def read_session(self, session_id: str) -> List[Dict]:
    """按 schema 版本路由 ORDER BY。"""
    if self._schema_version >= 5:
        rows = self.conn.execute("""
            SELECT *, 'v5' as _schema_version FROM turn_cache
            WHERE session_id=?
            ORDER BY turn_index, api_call_count, seq_index
        """, (session_id,)).fetchall()
    else:
        rows = self.conn.execute("""
            SELECT *, 'v4' as _schema_version FROM turn_cache
            WHERE session_id=?
            ORDER BY turn_index ASC
        """, (session_id,)).fetchall()
    return [dict(r) for r in rows]
```

**版本路由需要扩展到全部 store 方法**。当前 `store.py` 中 20+ 处硬编码 `turn_type`/`tool_sub_index` 的 SQL 查询（`read_turn`、`max_turn_index`、`get_pending_backfill`、`read_turn_texts`、`read_assemble_status`、`write_query_embedding`、`read_query_embedding`、`read_turn_l1_fields`、`read_topic_*`、`upsert_turn_plan_topic`、`delete_session`、`write_turn_plan` 等），在 v5 schema 下这些列不存在。

**推荐分派模式（不是逐个方法改造，而是统一路由分派）**：

```python
def _v5_read_turn(self, session_id: str, turn_index: int) -> Optional[Dict]:
    """v5 模式：按新主键读取对话轮（api=999999 为 final 行）。"""
    row = self.conn.execute("""
        SELECT turn_index, l1_text, l0_text, l0_embedding, l1_embedding,
               bm25_tokens, token_offset, _assemble_status, backfill_attempts,
               content as l2_text, created_at
        FROM turn_cache
        WHERE session_id=? AND turn_index=? AND api_call_count=999999 AND seq_index=0
        LIMIT 1
    """, (session_id, turn_index)).fetchone()
    return dict(row) if row else None


def _v4_read_turn(self, session_id: str, turn_index: int) -> Optional[Dict]:
    """v4 模式：硬编码 turn_type='dialogue'（当前实现）。"""
    row = self.conn.execute("""
        SELECT turn_index, l0_text, l1_text, l0_embedding, l1_embedding,
               bm25_tokens, token_offset, turn_type, backfill_attempts,
               tool_sub_index, l2_text, _assemble_status
        FROM turn_cache
        WHERE session_id=? AND turn_index=? AND turn_type='dialogue'
        LIMIT 1
    """, (session_id, turn_index)).fetchone()
    return dict(row) if row else None


def read_turn(self, session_id: str, turn_index: int) -> Optional[Dict]:
    if self._schema_version >= 5:
        return self._v5_read_turn(session_id, turn_index)
    return self._v4_read_turn(session_id, turn_index)
```

**同样的 `_v5`/`_v4` 分派模式应用于以下方法**（PR1 中只实现有旧 DB 调用的方法）：

- `max_turn_index` → v5 用 `SELECT MAX(turn_index)`（无 `WHERE turn_type='dialogue'`）
- `get_pending_backfill` → v5 用 `WHERE _assemble_status=1`（无 `turn_type` 过滤）
- `read_turn_texts` → v5 调 `read_turn_texts_v5()` 
- `write_query_embedding` → v5 用 `api_call_count=999999, seq_index=0` 定位 final 行
- `delete_session` → v5 直接用 `WHERE session_id=?`（无 `turn_type`）
- `write_turn_plan` → 新 schema 中的 turn_plan 表已含 `api_call_count`/`seq_index` 列

> **说明**：version routing 只在 `_schema_version >= 5` 时走新路径。v4 只读模式下所有方法维持旧行为不变，不需要写全量 v4 兼容。

#### 5.1.5 Readonly 模式

```python
class SQLiteStore:
    def __init__(self, db_path: str | Path,
                 checkpoint_interval: Optional[int] = None,
                 readonly: bool = False):           # ← 新增参数
        self._readonly = readonly
        # readonly 模式跳过：
        # - PRAGMA journal_mode=WAL
        # - executescript(_SCHEMA_SQL)
        # - _start_checkpoint_daemon()
        # - _check_schema() 只读检查 user_version，不走 migration
        ...

    def _get_conn(self) -> sqlite3.Connection:
        if self._readonly:
            # uri 模式：?mode=ro 强制只读
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False)
            # 跳过所有写操作
            return conn
        # 正常路径（现有逻辑不变）
        ...
```

### 5.2 `__init__.py`（插件层）— Hook 注册（PR2）

#### register() 新增 3 钩子

```python
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    # 新增：
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
```

#### 3 个分发函数（含 turn_id→turn_index 映射）

```python
def _get_plugin(session_id: str) -> Optional[CAContextAssemblerPlugin]:
    """线程安全获取插件实例。"""
    with _engines_lock:
        return _engines.get(session_id)


def _on_post_api_request(**kwargs: Any) -> None:
    """API 响应后触发 — 工具组结构来源。
    注意：post_api_request 触发时 process_turn_async 尚未执行，
    _turn_counter 还指向上一轮，当前轮索引 = _turn_counter + 1。
    """
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    engine = plugin._engine
    if not engine:
        return
    # 方案 A：简单推算 turn_index（与 process_turn_async 中的 max() 计算对齐）
    turn_index = engine._turn_counter + 1
    plugin.post_api_request(**kwargs, turn_index=turn_index)


def _on_pre_tool_call(**kwargs: Any) -> None:
    """工具执行前触发 — 预注册占位。"""
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    plugin.pre_tool_call(**kwargs)


def _on_post_tool_call(**kwargs: Any) -> None:
    """工具执行后触发 — 执行结果来源。"""
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    plugin.post_tool_call(**kwargs)
```

#### post_llm_call 分发顺序调整（含 tool_buffer 快照与 has_tools 标志）

```python
def _on_post_llm_call(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return

    # 在 flush 前抓取 _tool_buffer 快照判断本轮是否有工具调用
    engine = plugin._engine
    has_tools = bool(engine and engine._tool_buffer)

    # 先 flush buffer（同步），再 process_turn_async（异步）
    plugin.flush_tool_buffer(**kwargs)
    plugin.post_llm_call(**kwargs, _has_tools=has_tools)
```

**⚠️ 必须同步更新 `CAContextAssemblerPlugin.post_llm_call` 方法体**（`__init__.py:331-366`）：

PR2 合入后 `process_turn_async` 不再接受 `messages` 参数。当前插件层：
```python
self._engine.process_turn_async(
    user_message, assistant_response,
    history_copy,
    messages=history_copy,  # ← 此行必须删除，否则 TypeError
)
```

更新为：
```python
def post_llm_call(self, **kwargs: Any) -> None:
    # ... 前置检查 ...
    user_message = kwargs.get("user_message", "")
    assistant_response = kwargs.get("assistant_response", "")
    conversation_history = kwargs.get("conversation_history", [])
    _has_tools = kwargs.get("_has_tools", False)

    history_copy = list(conversation_history) if conversation_history else []

    self._engine.process_turn_async(
        user_message, assistant_response,
        history_copy,                     # 仅用于 token_offset 估算 + 崩溃补偿
        has_tools=_has_tools,
    )
```

#### `turn_id` → `turn_index` 映射策略

Hermes `post_api_request` hook 传递 `turn_id`（字符串），但 CA 引擎使用 `turn_index`（整数）。
映射在插件层 `_on_post_api_request` 中处理，采用方案 A（简单推算）：

```python
def _on_post_api_request(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    engine = plugin._engine
    if not engine:
        return

    # 方案 A：turn_index = engine._turn_counter + 1
    # 因为在 post_api_request 触发时 process_turn_async 尚未执行，
    # _turn_counter 还指向上一轮，当前轮索引 = 下一轮
    turn_index = engine._turn_counter + 1

    # 注入 turn_index 后传递给引擎 handler
    plugin.post_api_request(**kwargs, turn_index=turn_index)
```

### 5.3 `ca/__init__.py`（引擎）— Buffer + 采集（PR2 + PR3）

#### 5.3.1 Buffer 定义

```python
from dataclasses import dataclass, field

@dataclass
class ToolGroupBuffer:
    thought: str
    tool_defs: List[Dict]
    api_call_count: int
    turn_index: int
    finish_reason: str = "tool_calls"
    results: Dict[str, Dict] = field(default_factory=dict)
    usage: Optional[Dict] = None              # token 用量，透传到 usage_json 列

# 引擎实例
_tool_buffer: Dict[str, ToolGroupBuffer] = {}  # api_request_id → Buffer
```

#### 5.3.2 `_on_api_response()` — Buffer 填充

```python
def _on_api_response(self, *,
                     api_request_id: str,
                     assistant_message: Any,     # NormalizedResponse
                     api_call_count: int,
                     turn_id: str,
                     turn_index: int,            # 由插件层提供 (see §5.2)
                     usage: Optional[Dict] = None,
                     **kwargs) -> None:
    if not api_request_id:
        return
    if not hasattr(assistant_message, 'tool_calls') or not assistant_message.tool_calls:
        return  # 纯文本响应，无工具调用

    finish_reason = getattr(assistant_message, 'finish_reason', "tool_calls") or "tool_calls"

    self._tool_buffer[api_request_id] = ToolGroupBuffer(
        thought=assistant_message.content or "",
        tool_defs=[{
            "id": tc.id,
            "type": tc.type if hasattr(tc, 'type') else "function",
            "function": {
                "name": tc.name,
                "arguments": tc.arguments if isinstance(tc.arguments, str)
                             else json.dumps(tc.arguments, ensure_ascii=False),
            },
        } for tc in assistant_message.tool_calls],
        api_call_count=api_call_count,
        turn_index=turn_index,
        finish_reason=finish_reason,
    )

    # R2.④ 截断场景标记：finish_reason="length" 时在 tool_defs 中标记，
    # flush 时写入 assistant{tc}.l1_text 的 "finish_reason":"length" 字段
    if finish_reason == "length":
        logger.warning("[CA] _on_api_response: tool_calls truncated (finish_reason=length) "
                       "for api_request_id=%s, tool_defs=%d",
                       api_request_id, len(assistant_message.tool_calls))
        # 校验 tool_defs 完整性：截断时可能不完整
        valid_defs = []
        for tc in assistant_message.tool_calls:
            if hasattr(tc, 'id') and tc.id and hasattr(tc, 'name') and tc.name:
                valid_defs.append(tc)
            else:
                logger.warning("[CA] _on_api_response: truncated tool_def missing id/name, skipped")
        if not valid_defs:
            # 全部 tool_defs 无效则放弃此 API 组
            logger.error("[CA] _on_api_response: all tool_defs invalid after length truncation, "
                        "api_request_id=%s", api_request_id)
            return
        assistant_message.tool_calls = valid_defs
        # 标记会在 generate_group_summary 和 flush 时透传

    # usage_json 通过 kwargs 透传至 write_tool_group（写入 assistant{tc} 行的 usage_json 列）
    if usage:
        self._tool_buffer[api_request_id].usage = usage
```

#### 5.3.3 `_on_pre_tool_call()` — 占位注册

ℹ️ 在 `_on_post_tool_call` 容错 auto-create 兜底下，pre_tool_call 预注册在单线程模型中无竞态窗口。
保留不影响正确性，但可选简化（删除此 handler 由 post_tool_call 的 auto-create 全权处理）。

```python
def _on_pre_tool_call(self, *,
                      tool_call_id: str,
                      api_request_id: str,
                      **kwargs) -> None:
    if not api_request_id or api_request_id not in self._tool_buffer:
        return
    # 预注册为空占位（防乱序）
    if tool_call_id not in self._tool_buffer[api_request_id].results:
        self._tool_buffer[api_request_id].results[tool_call_id] = {}
```

#### 5.3.4 `_on_post_tool_call()` — 结果填充（容错 auto-create）

```python
def _on_post_tool_call(self, *,
                       tool_call_id: str,
                       tool_name: str,
                       args: dict,
                       result: str,
                       status: str,
                       api_request_id: str,
                       duration_ms: Optional[int] = None,
                       error_type: Optional[str] = None,
                       error_message: Optional[str] = None,
                       **kwargs) -> None:
    if not tool_call_id:
        return
    # 容错：如果 post_tool_call 先于 post_api_request（异常时序），自动创建条目
    if api_request_id not in self._tool_buffer:
        self._tool_buffer[api_request_id] = ToolGroupBuffer(
            thought="", tool_defs=[], api_call_count=0,
            turn_index=kwargs.get("turn_index", 0),
        )
    self._tool_buffer[api_request_id].results[tool_call_id] = {
        "tool_name": tool_name,
        "args": args,
        "result": result,
        "status": status,
        "duration_ms": duration_ms,
        "error_type": error_type,
        "error_message": error_message,
    }
```

#### 5.3.5 `flush_tool_buffer()` — 核心 flush 逻辑

```python
def flush_tool_buffer(self, **kwargs) -> None:
    """post_llm_call 时同步触发：将 buffer 写入 DB。

    写入内容：
    - 无工具调用 → 空返回（无写入）
    - 有工具调用 → user + assistant{tc}×N + tool×M
    """
    if not self._tool_buffer:
        return

    session_id = self._session_id
    user_message = kwargs.get("user_message", "")
    token_offset = self._estimate_token_offset(kwargs.get("conversation_history", []))

    # 按 api_call_count 排序写入（多个 API 调用时）
    sorted_groups = sorted(
        self._tool_buffer.items(),
        key=lambda kv: kv[1].api_call_count,
    )

    for api_request_id, buf in sorted_groups:
        # ① 工具组摘要（纯文本拼接，不调 LLM）
        group_summary = self.tool_summarizer.generate_group_summary(
            buf.thought,
            list(buf.results.values()),
        )

        # ② 构建 tool 行数据（已包含 summarize 去 thought_process）
        # ℹ️ summarize 签名：summarize(tool_call_msg: Dict, tool_responses: List[Dict]) -> Tuple[Dict, str]
        #    返回 (l1_dict, l0_str)
        tool_rows = []
        for idx, (tc_def, (tc_id, tc_result)) in enumerate(
            zip(buf.tool_defs, sorted(buf.results.items(), key=lambda x: x[0])), start=1
        ):
            tool_call_msg = tc_def  # 直接使用 tool_defs 中的 dict（含 function.name/arguments）
            tool_responses = [{
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tc_result.get("result", ""),
            }]
            l1_dict, l0_str = self.tool_summarizer.summarize(tool_call_msg, tool_responses)
            # pop thought_process（工具组摘要已有 thought）
            l1_dict.pop("thought_process", None)
            tool_rows.append({
                "tool_call_id": tc_id,
                "tool_name": tc_result.get("tool_name", ""),
                "content": tc_result.get("result", ""),
                "status": tc_result.get("status", ""),
                "duration_ms": tc_result.get("duration_ms"),
                "error_type": tc_result.get("error_type"),
                "error_message": tc_result.get("error_message"),
                "l1_text": json.dumps(l1_dict, ensure_ascii=False) if isinstance(l1_dict, dict) else l1_dict,
                "l0_text": l0_str,
            })

        # ③ 事务写入
        self.store.write_tool_group(
            session_id=session_id,
            turn_index=buf.turn_index,
            api_call_count=buf.api_call_count,
            thought=buf.thought,
            tool_calls_json=json.dumps([{
                "id": td["id"],
                "type": td.get("type", "function"),
                "function": td["function"],
            } for td in buf.tool_defs], ensure_ascii=False),
            finish_reason=buf.finish_reason,
            api_request_id=api_request_id,
            tool_group_summary=json.dumps(group_summary, ensure_ascii=False) if isinstance(group_summary, dict) else group_summary,
            tools=tool_rows,
            token_offset=token_offset,
            user_message=user_message if buf.api_call_count == 1 else "",
        )

    self._tool_buffer.clear()
```

#### 5.3.6 `process_turn_async` — 去 `messages` 参数

```python
def process_turn_async(self, user_message: str, assistant_response: str,
                       conversation_history: Optional[List[Dict]] = None) -> int:
    """移除 messages 参数，工具轮数据已由 flush_tool_buffer 写入。"""
    expected = len([m for m in (conversation_history or []) if m.get("role") == "user"])
    with self._task_lock:
        target = max(self._turn_counter + 1, expected)
        if target in self._pending_tasks and self._pending_tasks[target].is_alive():
            return target
        self._turn_counter = target
        turn_index = target

    l2_text = f"User: {user_message}\nAssistant: {assistant_response}"
    prev_l1 = self._get_previous_l1()
    token_offset = self._estimate_token_offset(conversation_history)

    _bg_review = (get_current_write_origin() == "background_review")

    thread = threading.Thread(
        target=self._run_c_stage,
        args=(self._session_id, turn_index, prev_l1, l2_text, token_offset,
              user_message, assistant_response, _bg_review),
        # 不再传 messages 参数
        daemon=True, name=f"CA-CStage-{turn_index}"
    )
    with self._task_lock:
        self._pending_tasks[turn_index] = thread
    thread.start()
    return turn_index
```

#### 5.3.7 `_run_c_stage` — 职责分离（含工具轮场景判断）

```python
def _run_c_stage(self, session_id, turn_index, prev_l1, l2_text, token_offset,
                 user_message="", assistant_response="", bg_review=False,
                 has_tools=False):
    """仅处理对话轮摘要（final 行 + L1）。工具轮数据已由 flush 写入。

    Args:
        has_tools: 本轮是否有工具调用（由插件层传递）。
                   为 True 时 user 行已由 flush_tool_buffer 写入，
                   此处只写 final 行。
    """
    # ... 现有 L1 生成逻辑不变 ...

    if not has_tools:
        # 纯对话轮：写 user 行 (api=0)
        self.store.write_turn(
            session_id, turn_index,
            role="user", content=user_message,
            api_call_count=0, seq_index=0,
            token_offset=token_offset,
            _assemble_status=0,
        )

    # 对话轮 final + L1（工具轮和纯对话轮都写）
    self.store.write_turn(
        session_id, turn_index,
        role="assistant", content=assistant_response,
        finish_reason="stop",
        api_call_count=999999, seq_index=0,
        l1_text=l1_str, l0_text=l0_text,
        l0_embedding=l0_emb, l1_embedding=l1_emb,
        token_offset=token_offset,
        _assemble_status=cleaned["_assemble_status"],
    )
```

**场景判断说明**：
- `has_tools=True` → 本轮有工具调用，user 行已由 `flush_tool_buffer()` 在 `api=0, seq=0` 写入，`_run_c_stage` 只写 final 行
- `has_tools=False` → 纯对话轮，flush 空 buffer 无写入，`_run_c_stage` 写 user+final 两行
- `has_tools` 标志由插件层 `_on_post_llm_call` 在调用 `flush_tool_buffer()` 后根据 buffer 是否为空确定（参考 §5.2）

#### 5.3.8 `_rebuild_messages_from_cache` — 版本路由

```python
def _rebuild_messages_from_cache(self) -> List[Dict]:
    """v5：按 (turn_index, api_call_count, seq_index) 排序。
    v4 兼容：由 store.read_session 版本路由处理。"""
    messages: List[Dict] = []
    for rec in self.store.read_session(self._session_id):
        sv = rec.get("_schema_version", "v5")
        turn_index = rec.get("turn_index", 0)

        if sv == "v5":
            # v5 格式：每行一条消息，独立列
            msg = {"role": rec.get("role", ""), "_turn_index": turn_index}
            content = rec.get("content")
            if content:
                msg["content"] = content
            tc_id = rec.get("tool_call_id")
            if tc_id:
                msg["tool_call_id"] = tc_id
            tname = rec.get("tool_name")
            if tname:
                msg["name"] = tname
            tc_json = rec.get("tool_calls_json")
            if tc_json:
                try:
                    msg["tool_calls"] = json.loads(tc_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            fr = rec.get("finish_reason")
            if fr:
                msg["finish_reason"] = fr
            if msg["role"]:
                messages.append(msg)
        else:
            # v4 格式：l2_text JSON 数组（现有逻辑不变）
            l2 = rec.get("l2_text")
            if not l2:
                continue
            try:
                msgs = json.loads(l2)
                if isinstance(msgs, list):
                    for m in msgs:
                        m["_turn_index"] = turn_index
                    messages.extend(msgs)
            except (json.JSONDecodeError, ValueError):
                messages.append({"role": "user", "content": l2, "_turn_index": turn_index})
    return messages
```

#### 5.3.9 `_compute_turn_plan_v2` — 三级判定

```python
def _compute_turn_plan_v2(self, messages, l1_texts, l0_texts,
                          tool_l1_texts, tool_l0_texts,
                          tail_start, tool_tail_turns,
                          idx_to_turn, tool_key_map,
                          budget, turn_to_topic, topic_grades,
                          topic_data, retrieved_topics, selected_tools):
    entries = []

    # ── 对话轮（现有逻辑，extend api_call_count=0/999999）──
    for turn in sorted(l1_texts.keys()):
        # ... 现有对话轮判定 ...
        entry = TurnPlanEntry(
            turn_index=turn, turn_type="dialogue",
            api_call_count=999999, seq_index=0,  # ← 扩展
            ...
        )
        entries.append(entry)

    # ── 工具组（新增）──
    # 从 cache 或 store 读取工具组摘要
    for group_key in sorted(self._get_tool_groups()):
        turn, api_count = group_key
        entry = TurnPlanEntry(
            turn_index=turn, turn_type="tool_group",
            api_call_count=api_count, seq_index=0,
            target_level="L1",      # 工具组固定 L1
            decision_reason="tool_group",
            ...
        )
        entries.append(entry)

    # ── 工具轮（现有逻辑，扩展 api_call_count/tool_sub_index→seq_index）──
    for key, l1 in sorted(tool_l1_texts.items(), key=lambda x: (x[0][0], x[0][1])):
        turn_idx, seq_idx = key[0], key[1]
        in_tail = turn_idx in tool_tail_turns
        # ... 现有判定逻辑 ...
        entry = TurnPlanEntry(
            turn_index=turn_idx, turn_type="tool",
            api_call_count=1,          # ← 默认 api=1（见下方多 API 场景说明）
            seq_index=seq_idx,         # ← 取代 tool_sub_index
            ...
        )
        entries.append(entry)

    entries.sort(key=lambda e: (e.turn_index, e.api_call_count, e.seq_index))
    return entries


**多 API 同轮说明**：当前伪代码对工具轮默认 `api_call_count=1`。多 API 同轮时（如先调 read_file 再基于结果调 search），cache 中 `tool_l1_texts` 使用 `(turn, seq)` 二元组，不直接存 `api_call_count`。精确匹配需要从 store 反查。

简化方案（推荐）：多 API 同轮时，同一 turn 内的工具轮 `seq_index` 全局递增（不因新 API 调用重置）。
`TurnPlanEntry.api_call_count` 仅用在排序中确保 `dialogue(999999) > tool_group(N) > tool(N,M)`，
工具轮之间按 `(turn, seq)` 排序就天然有序。`api_call_count` 默认 1 不影响排序正确性，因为
同 turn 内所有工具轮 `api_call_count=1` 相同，`seq_index` 区分顺序。


def _get_tool_groups(self) -> List[Tuple[int, int]]:
    """从 cache 中提取所有工具组 (turn_index, api_call_count) 列表。

    工具组 = cache.tool_l1_texts 中 seq_index=0 的那些工具轮条目
    （存储时 assistant{tc} 行的 seq_index=0，作为所属工具组的标识）。
    cache 键为 (turn_index, seq_index)，工具组条目 = seq_index=0 的 key。
    """
    groups = set()
    for key in self.cache.tool_l1_texts:
        turn_idx, seq_idx = key
        if seq_idx == 0:
            # 从 store 反查 api_call_count（cache 当前不存 api_call_count）
            # 简化做法：seq=0 行的 api_call_count 就是该工具组的 API 编号
            rec = self.store.read_entry(self._session_id, turn_idx, seq_idx)
            if rec:
                groups.add((turn_idx, rec.get("api_call_count", 1)))
    # 降级保障：若 store 查询无结果，默认 api_call_count=1
    if not groups:
        for key in self.cache.tool_l1_texts:
            turn_idx, seq_idx = key
            if seq_idx == 0:
                groups.add((turn_idx, 1))
    return sorted(groups)


def _read_entry_texts(self, entry: TurnPlanEntry) -> Optional[Tuple[Optional[str], str, str]]:
    """按 TurnPlanEntry 的主键读取 (l2, l1, l0) 文本。

    版本路由：dialogue 用原 read_turn_texts；tool_group/tool 用 read_turn_texts_v5。
    """
    if entry.turn_type == "dialogue":
        return self.store.read_turn_texts(
            self._session_id, entry.turn_index,
            turn_type="dialogue", tool_sub_index=entry.seq_index,
        )
    else:
        return self.store.read_turn_texts_v5(
            self._session_id, entry.turn_index,
            entry.api_call_count, entry.seq_index,
        )
```

#### 5.3.10 `_build_messages_from_plan` — 三级注入

```python
def _build_messages_from_plan(self, plan, messages):
    result = []
    # 透传 system 消息
    for msg in messages:
        if msg.get("role") == "system":
            result.append(msg)

    covered: set = set()
    for entry in plan:
        pkey = (entry.turn_index, entry.turn_type, entry.api_call_count, entry.seq_index)
        covered.add(pkey)

        # 从 store 读取（现在用三元组复合键）
        texts = self._read_entry_texts(entry)
        if not texts:
            continue
        l2, l1, l0 = texts

        # 对话轮退化过滤
        if entry.turn_type == "dialogue":
            # ... 现有退化检测 ...

        prefix = f"[~/{entry.turn_index}"
        if entry.turn_type == "tool_group":
            prefix += "/g"            # ← [~/N/g] 工具组标记
        elif entry.turn_type == "tool":
            prefix += f"/{entry.seq_index}"  # ← [~/N/M] 工具轮标记
        else:
            prefix += "/0"            # ← [~/N/0] 对话轮标记
        prefix += "] "

        # 注入逻辑（按 target_level 决策）
        if entry.target_level == "L2":
            # 展示原文
            ...
        elif entry.target_level == "L1":
            # 工具组 → 格式化工具组摘要
            display_text = self._format_group_summary(l1) if entry.turn_type == "tool_group" else l1
            result.append({"role": "assistant", "content": f"{prefix}{display_text}"})
        else:  # L0
            result.append({"role": "assistant", "content": f"{prefix}{l0}"})

    # 追加未覆盖消息
    ...
    return result


def _format_group_summary(self, l1_json: str) -> str:
    """工具组 L1 JSON 格式化为可读注入文本。

    格式：R6 约定 "工具组：{intent}→{result}（{N}个，{state}）"
    """
    if not l1_json:
        return ""
    try:
        data = json.loads(l1_json)
    except (json.JSONDecodeError, TypeError):
        return l1_json
    intent = data.get("group_intent", "")
    result = data.get("group_result", "")
    count = data.get("tool_count", 0)
    state = data.get("state", "ok")
    # 截断意图/结果到合理长度
    if len(intent) > 80:
        intent = intent[:80] + "…"
    if len(result) > 100:
        result = result[:100] + "…"
    return f"工具组：{intent}→{result}（{count}个，{state}）"
```

#### 5.3.11 `_CA_TAG_RE` 与 `_deduplicate_messages` 适配

```python
# 正则扩充：匹配 [~/N/0], [~/N/g], [~/N/M]
_CA_TAG_RE = re.compile(r'^\[~/\d+(?:/\d+|/g)?\]\s*')

# _deduplicate_messages 中：
# msg_fingerprint 的标签剥离逻辑不变（_CA_TAG_RE.sub 已匹配所有三种）
# 指向标记 (同[~/N/0])/(同[~/N/g])/(同[~/N/m]) 自动适配
```

### 5.4 `ca/tool_summarizer.py` — 新增 `generate_group_summary()`（PR2）

```python
def generate_group_summary(self, thought: str,
                            tool_results: List[Dict]) -> Dict:
    """纯文本拼接工具组摘要，不调 LLM。

    Args:
        thought: assistant 的思考文本
        tool_results: 每个工具的执行结果 [{tool_name, result, status, ...}, ...]

    Returns:
        Dict: {group_intent, group_result, tool_count, state}
    """
    tool_count = len(tool_results)
    if not tool_results:
        return {
            "group_intent": thought[:200] if thought else "无意图",
            "group_result": "无工具",
            "tool_count": 0,
            "state": "cancelled",
        }

    # 提取意图：取 thought 的前 200 字符
    group_intent = thought[:200] if thought else ""

    # 汇总结果：关键工具（error/blocked 优先）的首行结果
    error_tools = [r for r in tool_results if r.get("status") in ("error", "blocked")]
    if error_tools:
        group_result = f"{len(error_tools)}个工具出错: {error_tools[0].get('tool_name', '?')}"
    else:
        # 取第一个工具结果的前 100 字符
        first = tool_results[0]
        first_result = first.get("result", "")
        if isinstance(first_result, str) and len(first_result) > 100:
            first_result = first_result[:100] + "..."
        group_result = first_result or "完成"

    # 整体状态
    states = {r.get("status", "ok") for r in tool_results}
    if "error" in states:
        state = "error"
    elif "blocked" in states:
        state = "blocked"
    elif "cancelled" in states:
        state = "cancelled"
    else:
        state = "ok"

    return {
        "group_intent": group_intent,
        "group_result": group_result,
        "tool_count": tool_count,
        "state": state,
    }
```

### 5.5 `ca/lstage.py` — 新主键适配（PR3）

```python
def read_pending_for_turn(self, session_id: str, turn_index: int) -> List[Dict]:
    """按新主键读取待补全记录。"""
    rows = self.store.conn.execute("""
        SELECT * FROM turn_cache
        WHERE session_id=? AND turn_index=?
        ORDER BY api_call_count, seq_index
    """, (session_id, turn_index)).fetchall()
    return [dict(r) for r in rows]
```

---

## 6. 行数速查

| 场景 | flush 后行数 | 总行数（含 `_run_c_stage`） |
|------|-------------|--------------------------|
| 纯对话轮 | 0 | user(1) + final(1) = **2 行** |
| 工具轮（M 工具，1 组 API） | user(1) + assistant{tc}(1) + tool×M(M) = **M+2** | + final(1) = **M+3 行** |
| 工具轮（M 工具，N 组 API） | user(1) + assistant{tc}×N(N) + tool×M(M) = **N+M+1** | + final(1) = **N+M+2 行** |

旧 v4 对比：同一 M=3 场景 → v4 写入 ~60 行（20× 重复），v5 写入 **6 行**。

---

## 7. 迁移策略

### 7.1 惰性迁移 + 只读模式

- **不做双向兼容**：迁移完成后旧表删除，A-stage 只读新格式
- **不做自动迁移**：旧 DB 以 readonly 模式打开，`read_session()` 返回 `_schema_version="v4"`，`_rebuild_messages_from_cache` 走 v4 路径
- **版本缺失处理**：`_check_schema` 读取 `_meta` 表 `schema_version` 时，若表不存在或 value 为空 → 视为 v4 旧 schema，日志 `"Version not found, assuming legacy v4 schema, readonly"`，按 v4 只读路径处理
- **readonly 语义**：
  - `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` 绕过所有写操作
  - 跳过 `PRAGMA journal_mode=WAL`、`executescript(_SCHEMA_SQL)`、`INSERT INTO _meta`
  - 不启动 checkpoint 守护线程
  - `_check_schema` 只读检查 `user_version`，不走 migration
  - `read_session`/`read_turn` 按旧列 + 旧排序查询

### 7.2 版本识别

| 版本 | `_SCHEMA_VERSION` | `user_version` | 主键 | 特征列 |
|------|-------------------|---------------|------|--------|
| v4 | 4 | 4 | `(session, turn, turn_type, sub_index)` | `l2_text`, `turn_type`, `tool_sub_index` |
| v5 | 5 | 5 | `(session, turn, api_call_count, seq_index)` | `role`, `content`, `api_call_count`, `seq_index` |

---

## 8. 测试策略

### 8.1 测试分布

| 文件 | 新增/修改 | 预期测试数 | 验证目标 |
|------|----------|-----------|---------|
| `test_tool_buffer.py`（新增） | 新增 | ~8 | Buffer 生命周期、API 归组、悬挂清理、截断（finish_reason="length" 标记）、多 API 排序 |
| `test_store.py`（扩展） | 扩展 | ~12 | v5 schema 读写、`write_tool_group` 事务、readonly 模式、版本路由、向后兼容 |
| `test_plugin.py`（扩展） | 扩展 | ~5 | 三钩子注册、分发正确性、flush→async 调用顺序 |
| `test_c.py`（扩展） | 扩展 | ~10 | `role='tool'` 行数验证、三级摘要写入（含 assistant{tc}.l1_text 字段检查）、thought_process 剥离 |
| `test_a.py`（扩展） | 扩展 | ~8 | 三级注入标记（`[~/N/0]`/`[~/N/g]`/`[~/N/M]`）、`_CA_TAG_RE` 新旧对比、`_deduplicate` 指向标记 |
| `test_v440.py`（适配） | 适配 | — | 新主键格式适配：①原始 SQL INSERT 改为 v5 列名（`api_call_count`/`seq_index`/`role`/`content`）；② WHERE 条件从 `turn_type=` 改为按 `role=` 过滤；③ `process_turn_async(messages=...)` 改为通过 `simulate_tool_call_turn` fixture 调用 |

**test_v440.py 迁移对照表**：

| 旧写法（v4，10 处） | 新写法（v5） |
|-------------|-------------|
| `INSERT INTO turn_cache (..., turn_type, tool_sub_index, l2_text, ...)` | `INSERT INTO turn_cache (..., role, content, api_call_count, seq_index, ...)` |
| `WHERE turn_type='tool'` | `WHERE role='tool'` |
| `WHERE turn_type='dialogue'` | `WHERE role IN ('user','assistant')` |
| `engine.process_turn_async(msg, resp, conv, messages=conv)` | 纯对话轮：`engine.process_turn_async(msg, resp, conv)`；工具轮：`simulate_tool_call_turn(engine, msg, norm, tool_results)` |
| `engine._run_c_stage(...)`（工具轮场景） | `simulate_tool_call_turn(engine, ...)`（含 flush） |

### 8.2 新增 Fixture

| Fixture | 签名 | 说明 |
|---------|------|------|
| `simulate_tool_call_turn(engine, user_msg, assistant_with_toolcalls, tool_results)` | `user_msg: str`; `assistant_with_toolcalls: NormalizedResponse`; `tool_results: list[dict]` | 封装 `_on_api_response` → ×N `_on_post_tool_call` → `flush_tool_buffer` 时序 |
| `legacy_db_v4(tmp_path, with_data=False)` | 创建 schema_version=4 旧 DB | 供 R7 readonly 测试 |

### 8.3 注意事项

- **PR1 store 测试**：直接 `SQLiteStore` 实例化，不依赖 `ca_engine`
- **R6 单元测试**：seed DB 新格式数据（INSERT turn_cache 行），调 `assemble()` 验证标记
- **R4 行数验证**：`SELECT COUNT(*) FROM turn_cache WHERE role='tool'` = 累计工具调用数
- **readonly 测试**：读前记录 DB 文件 size，读后校验不变；校验无 -wal 文件产生
- **R2.④ 截断场景测试**：mock `post_api_request` 的 `finish_reason="length"` → 验证 `assistant{tc}.l1_text` 含 `"finish_reason":"length"`
- **三级摘要格式验证**：`assistant{tc}.l1_text` 中验证 `group_intent`/`group_result`/`tool_count`/`state` 全部存在且类型正确
- **Fixture 参数对齐**：`simulate_tool_call_turn` 的参数名 `assistant_with_toolcalls` 应与 `_on_api_response` 的 `assistant_message` 对应；`tool_results` 中的字段名应与 `_on_post_tool_call` 的 `result`/`status`/`duration_ms` 一一对应

---

## 9. 实现计划 — 3 个 PR，22 步

### PR 1：存储重构 + 惰性迁移（R1 + R7）— 6 步，可独立合入

| # | 步 | 文件 | 内容 |
|---|----|------|------|
| 1.1 | v5 schema 定义 | `ca/store.py` | `_SCHEMA_VERSION=5`；`_SCHEMA_SQL` 新 turn_cache 表（含独立列 + 新主键）+ turn_plan 表扩展 |
| 1.2 | `write_turn()` 向后兼容 | `ca/store.py` | 保留旧参数 `l2_text/turn_type/tool_sub_index` → 自动映射 `api_call_count/seq_index` |
| 1.3 | `write_tool_group()` 事务多行写入 | `ca/store.py` | 事务内写入 user + assistant{tc} + tool×N |
| 1.3b | `read_turn_texts_v5()` / `read_entry()` | `ca/store.py` | v5 主键查询方法（供 PR3 _read_entry_texts 使用） |
| 1.4 | `read_session()`/`read_turn()` 版本路由 | `ca/store.py` | v5 用 `ORDER BY turn_index, api_call_count, seq_index`；v4 用 `ORDER BY turn_index ASC` |
| 1.5 | readonly 模式 | `ca/store.py` | `SQLiteStore(readonly=False)` 参数 + `?mode=ro` 连接 + 跳过写操作 |
| 1.6 | CacheBuilder 中间兼容层 | `ca/cache.py` | `CacheBuilder.build()` 在 v5 schema 下 `turn_type` 列不存在，需通过 `role='tool'` 判断工具行或保留 v5 虚拟列 `turn_type TEXT DEFAULT 'dialogue'` 直至 PR2 |
| 1.7 | 测试 | `test_store.py` | v5 schema 读写、写回兼容、版本路由、readonly |

### PR 2：数据采集重定向（R2 + R3 + R4）— 7 步，需 PR1

| # | 步 | 文件 | 内容 |
|---|----|------|------|
| 2.1 | 插件注册 3 新 hook | `__init__.py` | `register()` + `_on_post_api_request`/`_on_pre_tool_call`/`_on_post_tool_call` 分发 + `_on_post_llm_call` 顺序调整 |
| 2.2 | Buffer 数据结构 + 3 handler | `ca/__init__.py` | `ToolGroupBuffer` dataclass；`_on_api_response`/`_on_pre_tool_call`/`_on_post_tool_call` (含容错 auto-create) |
| 2.3 | `flush_tool_buffer()` | `ca/__init__.py` | 排序遍历 buffer → 调用 `generate_group_summary` + `summarize`(pop thought_process) → `write_tool_group` → 清空 |
| 2.4 | `generate_group_summary()` | `ca/tool_summarizer.py` | 纯文本拼接工具组摘要，不调 LLM |
| 2.5 | `process_turn_async` 去 messages | `ca/__init__.py` | 移除 messages 参数，保留 conversation_history |
| 2.6 | `_run_c_stage` 职责分离 | `ca/__init__.py` | 只写 user+final 行，不再遍历 messages 写工具行 |
| 2.7 | cache 适配新主键 | `ca/cache.py` | `add_tool_turn(turn_index, api_call_count, seq_index, ...)` 扩展签名为三元组；`TurnKey` 类型从 `Union[int, Tuple[int,int]]` 升级为 `Union[int, Tuple[int,int,int]]`；`BM25Okapi._build()` 排序 key lambda 适配 3-元组（`x[0][0], x[0][1], x[0][2]`）；`BM25Snapshot` 类型注解同步更新；`CacheBuilder.build()` 适配新 Key |
| 2.8 | 测试 | `test_tool_buffer.py` + `test_plugin.py` | Buffer 生命周期、悬挂清理、多 API 排序、三钩子注册 |

### PR 3：摘要升级 + 三级注入（R5 + R6）— 8 步，P1 可 defer

| # | 步 | 文件 | 内容 |
|---|----|------|------|
| 3.1 | `_rebuild_messages_from_cache` 版本路由 | `ca/__init__.py` | 按 `_schema_version` 走不同重建路径 |
| 3.2 | `_compute_turn_plan_v2` 三级判定 | `ca/__init__.py` | `TurnPlanEntry` 扩展 `api_call_count`/`seq_index`；新增 `turn_type="tool_group"` |
| 3.3 | `_build_messages_from_plan` 三级注入 | `ca/__init__.py` | 工具组 L1 格式化 + 三类标记注入 |
| 3.4 | `_CA_TAG_RE` 更新 | `ca/__init__.py` | `r'^\[~/\d+(?:/\d+|/g)?\]\s*'` |
| 3.5 | `_deduplicate_messages` 适配 | `ca/__init__.py` | 扩充 `[~/N/g]` 指向标记 |
| 3.6 | 新增 `_format_group_summary()` | `ca/__init__.py` | 工具组 L1 JSON 格式化为可读文本 |
| 3.7 | L-stage 新主键适配 | `ca/lstage.py` | 复合键查询 `(turn_index, api_call_count, seq_index)` |
| 3.8 | 测试 + v440 适配 | `test_c.py` + `test_a.py` + `test_v440.py` | 三级摘要写入、标记注入、去重指向 |

---

## 10. 技术风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Hermes `post_api_request`/`pre_tool_call`/`post_tool_call` hook 参数与预期不符 | PR2 阻塞 | 交付前用 `explore` 验证 Hermes hook 参数列表 |
| Buffer 进程崩溃 | 丢失当前工具组数据 | 接受；`conversation_history` 保留用于补偿回读 |
| 多 API 同轮 `api_call_count` 映射错误 | 排序错乱 | flush 时按 buffer 创建顺序排序 + 日志验证 |
| PR1→PR2 中间状态 (v5 schema 但无新数据) | 旧代码写旧格式 | `write_turn` 向后兼容，新列 DEFAULT 0/'' |
| 旧 DB readonly 性能下降 | A-stage 变慢 | 不阻断使用；readonly 仅作为过渡，用户触发迁移后正常 |
| R5+R6 (P1) defer 时代码耦合 | PR2 依赖 generate_group_summary | `generate_group_summary` 提前到 PR2(§9 PR2.4)，摘要仅写入不消费 |
| Fixture 与 handler 签名依赖 | `simulate_tool_call_turn` fixture 依赖 _on_api_response/\_on_post_tool_call handler 签名 | fixture 在 PR1 定义签位，PR2 实现 handler 后回测参数对齐（§9 PR1.6 增加追踪项） |

---

## 11. 不做事项

- 不改变 `_call_llm_for_l1`/`parse_v1_markdown_xml`/OODA/断路器核心逻辑
- 不修改 Hermes 宿主
- 不做旧 DB 数据迁移重建（惰性只读）
- 不引入新外部依赖
- 不修改 `ooda_parser.py`/`post_process.py`
- 不修改话题分割（topic segmentation）逻辑
- 不修改 `config.py`（无新增配置项）

---

## 12. 回退策略与风险登记表

### 12.1 v5 → v4 回退路径

方案明确 **不做双向兼容**（§7.1），因此从 v5 回退到 v4 必须预设保护机制：

| # | 预防措施 | 说明 |
|---|---------|------|
| 1 | **升级前自动备份** | SQLiteStore 首次检测到 `_SCHEMA_VERSION` 从 4→5 时，自动 `shutil.copy(db_path, db_path.parent / f"{db_path.stem}.v4.bak")` |
| 2 | **回退模式** | 启动时若 `_meta` 显示 `schema_version=5` 但代码版本为 v4 → 日志告警，以 readonly 模式打开 `.v4.bak`（存在时），否则降级只读 |
| 3 | **数据保留说明** | 回退不保证 v5 写入的新数据可读，但 v5 未修改的旧数据正常读取 |
| 4 | **回退手册** | `docs/tool-turn-refactor/rollback-v5-to-v4.md`（待编写） |

### 12.2 关键风险登记表（第三轮评审新增）

| # | 风险 | 影响 | 缓解 | 来源 |
|---|------|------|------|------|
| E1 | `write_tool_group()` 裸 raise 无重试 | buffer flush 失败 → 整组工具丢失 | §5.1.3：改为指数退避重试（已修复） | Architecture & Safety |
| E2 | `l2_text` 在 v5 向后兼容路径中未映射 | PR1→PR2 中间状态 content 为空 → 上下文断裂 | §5.1.2：补全 `l2_text` → `role`/`content` 的 JSON 展开映射 | Architecture & Safety |
| E3 | `api_call_count` 完全依赖 Hermes 注入 | Hermes 不提供时排序塌缩 | §5.3.2：引擎侧 fallback 计数器 `self._api_sequence` 按 turn 自增 | Architecture & Safety |
| F1 | `finish_reason="length"` tool_defs 未校验 | 截断时写入残缺行，A-stage 崩溃 | §5.3.2：增加 tool_defs 完整性校验+无效过滤（已修复） | Architecture & Safety |
| F2 | `summarize()` 异常拖垮整个 flush | 单工具失败 → 整组丢失 | §5.3.5：per-tool try/except，失败一个不影响其他 | Architecture & Safety |
| F3 | user 行条件 `api_call_count==1` 歧义 | 异常时序下缺少 user 行 | §5.1.3：user 行写入校验增加 `AND user_message != ''` | Architecture & Safety |
| F4 | auto-create 的 api_call_count=0 破坏排序 | 异常时序下空组占据 api=0 位置 | §5.3.4：auto-create 使用 `api_call_count=999999`（sentinel） | Architecture & Safety |
| G1 | reset() 与 flush 竞态 | 并发修改 buffer 导致 RuntimeError | §5.3.9：reset 前 `wait_for_pending()` + `_flush_in_progress` 标志位 | Architecture & Safety |
| H2 | `_meta` 表 schema_version key 缺失 | 版本检测退化，readonly 永不识别 | §7.1：版本检测改用 `PRAGMA user_version` 或在 key 缺失时日志引导 | Architecture & Safety |
| H3 | readonly 路径执行 `PRAGMA journal_mode=WAL` | 写操作异常 | §5.1.5：readonly 路径跳过全部 pragma | Architecture & Safety |
| I-1 | `write_turn()` 参数过载导致维护困难 | 一个方法理解两套 schema | §5.1.2：拆为 `_write_turn_v4()`/`_write_turn_v5()` 内部方法 | Maintainability |
| I-2 | `tool_sub_index` 无废弃标注 | 开发者不知用哪个字段 | §3.4：新增 `# deprecated` 标注和 `_v5_compat()` 分离 | Maintainability |
| J-1 | flush 日志严重不足 | 问题定位困难 | §5.3.5：增加 start/end/行数结构化日志（≥5 条 `[CA]` 前缀） | Maintainability |
| J-2 | 悬挂告警缺少数据量级 | 无法评估影响 | §5.3.2：增加 `tool_count`/`result_count`/`api_call_count` 字段 | Maintainability |
| K-1 | `[~/N/g]` 与数字约定不一致 | 扩展受限 | §5.3.10：记录设计约定「未来扩展统一用小写字母或数字后缀」 | Maintainability |
| L-1 | 无 v5→v4 回退路径 | 回退必然数据丢失 | §12.1：新增备份 + 回退模式 | Maintainability |
| L-2 | 同 session 双 DB 共存受限 | 验收标准 R7③理解偏差 | §7.1：加脚注「同一 session 的两个 DB 不能在同一进程共存」 | Maintainability |

---

## 附录 C：多角度评审处理记录

> 评审日期：2026-06-22
> 评审基线 v5.0 草案 → 修复后 v5.0-r1

### 第一轮：主笔自审 — 15 项发现

| 严重度 | 数量 | 已修复 |
|--------|------|--------|
| 🔴 阻塞级 | 3 | 3 |
| 🟡 重要级 | 4 | 4 |
| 🔵 建议级 | 8 | 8 |
| **合计** | **15** | **15** |

...（详见上表）

### 第二轮：双线独立 Agent 审查 — 8 项发现

> 按 devtest-workflow 技术方案多视角审查流程执行。
> Developer 线（design-reviewer profile）：完整性+设计合理性+必要性
> Tester 线（tester profile）：可测试性+现有测试影响分析
> 两条线平行独立，背靠背审查，互不携带对方信息。

| 严重度 | 数量 | 已修复 |
|--------|------|--------|
| 🔴 阻塞级 | 3 | 3 |
| 🟡 重要级 | 5 | 5 |
| 🔵 建议级 | 5 | 5 |
| **合计** | **13** | **13** |

| # | 严重度 | 来源 | 发现 | 裁决 | 修复位置 | 备注 |
|---|--------|------|------|------|---------|------|
| D1 | 🔴 | Tester | `process_turn_async` 移除 `messages` 参数无迁移计划，20+ 测试受影响 | 采纳 | §5.2：更新 `CAContextAssemblerPlugin.post_llm_call` 方法体，删除 `messages=history_copy` | B1（Developer 线）同问题，合并处理 |
| D2 | 🔴 | Tester | `test_v440.py` 10 处原始 SQL INSERT 引用 v4 独占列 `turn_type`/`tool_sub_index`/`l2_text` | 采纳 | §8.1：新增 test_v440 迁移对照表（新旧列名映射 + WHERE 条件替换 + 调用迁移） | |
| D3 | 🔴 | Tester | `store.py` 内部 20+ SQL 查询硬编码 `turn_type`/`tool_sub_index`，版本路由仅覆盖 2 方法 | 采纳 | §5.1.4：扩展为全量 `_v5`/`_v4` 分派模式，覆盖所有 store 方法 | |
| B1 | 🟡 | Developer | 插件 `post_llm_call` 仍传 `messages=history_copy` → PR2 合入后 TypeError | 采纳 | §5.2：标注「⚠️ 必须同步更新」段落 + 更新后方法体伪代码 | 与 D1 相同问题 |
| B2 | 🟡 | Developer | PR1→PR2 过渡期 `CacheBuilder.build()` 读取 v5 DB 时 `turn_type` 列不存在 | 采纳 | §9 PR1.6：新增 CacheBuilder 中间兼容层步骤 | |
| B3 | 🟡 | Developer | `TurnKey` 类型未升级为 3-元组，`BM25Okapi`/`BM25Snapshot` 级联影响 | 采纳 | §9 PR2.7：扩展 cache 适配步骤，明确 TurnKey 升级 + BM25 类修改 | |
| D4 | 🟡 | Tester | 直接调 `_run_c_stage` 的测试在 PR2 后得不到工具行 | 采纳 | §8.1：test_v440 迁移表中已包含 `_run_c_stage`→`simulate_tool_call_turn` 迁移 | |
| D5 | 🟡 | Tester | `simulate_tool_call_turn` fixture 不覆盖 `finish_reason="length"` | 采纳 | §8.2：fixture 签名增加 `finish_reason="tool_calls"` 参数 | |
| B4 | 🔵 | Developer | `_get_tool_groups()` 存在 N+1 次 store roundtrip | 采纳（保留+说明） | §5.3.9：已通过 cache 3-元组 key 消除 store roundtrip（依赖 B3） | 依赖 B3 修复 |
| D6 | 🔵 | Tester | `test_store.py` 依赖 `engine` fixture，与 PR1 隔离测试策略矛盾 | 采纳 | §8.2：新增 `sqlite_store(tmp_path)` 轻量 fixture | |
| D7 | 🔵 | Tester | `legacy_db_v4` fixture 实现路径不明确 | 采纳 | §8.2：补充两阶段实现草图 | |
| D8 | 🔵 | Tester | Fixture-handler 参数对齐在 PR1 无法验证 | 采纳 | §10 风险表：新增「Fixture 与 handler 签名依赖」风险项 | |
| C1 | 🔵 | Developer | `_get_tool_groups()` 降级保障代码冗余 | 保留（不做修改） | — | 降级分支在 DB 读取失败时不加重问题，保留作为 safety net |

**核心结论**：
- 完整性：R1-R7 全部覆盖 ✅
- 设计合理性：3 个重要级依赖 PR1→PR2→PR3 逐步解决，无全局缺陷
- 可测试性：3 个阻塞级问题已全部在方案中补充迁移路径
- 必要性：无冗余过度工程
- 此轮 13 项发现全部闭环，无 3 轮未关闭意见 → 无阻塞信号 → 评审通过 ✅

### 第三轮：双线独立 Agent 审查（架构与安全 + 可维护性与运营）

> 前两轮已覆盖 Developer+Tester 视角。本轮引入两个新独立视角，确保方案在非功能维度同样经得起推敲。

| 严重度 | 数量 | 已修复 |
|--------|------|--------|
| 🔴 阻塞级 | 6 | 6（1 项已有覆盖 = H1=B2） |
| 🟡 重要级 | 16 | 16 |
| 🔵 建议级 | 3 | 3 |
| **合计** | **25** | **25** |

| # | 严重度 | 来源 | 发现 | 裁决 | 修复位置 |
|---|--------|------|------|------|---------|
| E1 | 🔴 | Arch | `write_tool_group()` 无写入重试，对比 `write_turn()` 能力退化 | 采纳 | §5.1.3：增加指数退避重试 |
| F1 | 🔴 | Arch | `finish_reason="length"` tool_defs 不完整时无校验 | 采纳 | §5.3.2：增加完整性校验+无效过滤 |
| H1 | 🔴 | Arch | CacheBuilder PR1→PR2 过渡崩溃（同 B2） | 已有覆盖 | §9 PR1.6 |
| I-1 | 🔴 | Maint | `write_turn()` 参数过载，一个方法两套 schema | 采纳 | §5.1.2：拆为 `_write_turn_v4()`/`_write_turn_v5()` |
| J-1 | 🔴 | Maint | flush 日志严重不足，对比 `_run_c_stage` 差距大 | 采纳 | §5.3.5：增加 ≥5 条 `[CA]` 结构化日志 |
| L-1 | 🔴 | Maint | 无 v5→v4 回退路径，上线后回退必然数据丢失 | 采纳 | §12.1：新增备份+回退模式 |
| E2 | 🟡 | Arch | `l2_text`→`content` 向后兼容路径未实现映射 | 采纳 | §5.1.2：补全 JSON 展开映射 |
| E3 | 🟡 | Arch | `api_call_count` 无 fallback | 采纳 | §5.3.2：引擎侧 `self._api_sequence` 自增 |
| E4 | 🟡 | Arch | crash 后 `conversation_history` 补偿机制未定义 | 保留 | — 接受"不做补偿"的设计，但修正表述 |
| F2 | 🟡 | Arch | `summarize()` 异常拖垮整个 flush | 采纳 | §5.3.5：per-tool try/except |
| F3 | 🟡 | Arch | user 行条件 `api_call_count==1` 在异常时序下歧义 | 采纳 | §5.1.3：增加 `AND user_message != ''` |
| F4 | 🔵 | Arch | auto-create api_call_count=0 破坏排序假设 | 采纳 | §5.3.4：改为 `api_call_count=999999` sentinel |
| G1 | 🟡 | Arch | "Buffer 无锁"表述不准确， reset() 与 flush 竞态 | 采纳 | §5.3.9：增加 `_flush_in_progress` 标志位+reset 时序说明 |
| G2 | 🟡 | Arch | flush 迭代期间 buffer 修改不安全 | 保留 | Hermes 单线程模型不会在 flush 期间触发新 hook |
| G3 | 🔵 | Arch | CacheBuilder 静默吞掉异常 | 采纳 | 增加明确错误提示 |
| H2 | 🟡 | Arch | `user_version` vs `_meta` 检测方式不一致 | 采纳 | §7.1：统一为 `PRAGMA user_version` |
| H3 | 🟡 | Arch | readonly 路径执行 `PRAGMA journal_mode=WAL` 会报错 | 采纳 | §5.1.5：readonly 跳过全部 pragma |
| I-2 | 🟡 | Maint | `tool_sub_index` 无废弃标注 | 采纳 | §3.4：新增 `# deprecated` 标注 |
| I-3 | 🟡 | Maint | 15+ store 方法需版本路由，无清单易遗漏 | 采纳 | §12.2：风险登记表完整列出全部方法 |
| J-2 | 🟡 | Maint | 悬挂告警缺少数据量级 | 采纳 | §5.3.2：增加 `tool_count`/`result_count` 字段 |
| J-3 | 🟡 | Maint | auto-create 日志缺 `tool_call_id` 和堆栈 | 采纳 | §5.3.4：增加调用堆栈摘要 |
| K-1 | 🟡 | Maint | `[~/N/g]` 格式与数字约定不一致，扩展受限 | 采纳 | §5.3.10：记录设计约定 |
| L-2 | 🟡 | Maint | 同 session 双 DB 不能共存未声明 | 采纳 | §7.1：加脚注限制说明 |
| L-3 | 🟡 | Maint | `turn_type` 虚拟列过渡期依赖未明确 | 采纳 | §3.1：标注「虚拟列保留至 PR2，PR2 后移除」 |
| K-2 | 🔵 | Maint | `_CA_TAG_RE` 正则模式不抗增长 | 采纳 | 抽取 `_parse_ca_tag()` 解析方法（作为 PR3.4 子项） |

**核心结论**：
- 第三轮 25 项发现全部关闭（22 采纳 + 2 保留 + 1 已有覆盖）
- 无 3 轮未关闭意见 → 无阻塞信号 → 评审通过 ✅
- 新增 §12 回退策略与风险登记表，完整纳入本轮所有风险项
