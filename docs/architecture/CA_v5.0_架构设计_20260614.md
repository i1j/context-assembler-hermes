# CA v5.0 — 架构设计

> **重要提示**：本文档为 v5.0 初始架构设计（2026-06-14）。自实装以来，架构已演进，新增以下子系统未在本文档反映。阅读时请对照以下变更对照表，以实际代码为最终权威。

## 实装后变更对照

| 变更 | 说明 | 代码位置 | 新增文档 |
|------|------|---------|---------|
| **topic-aware 三级替换** | A-stage 不再统一替换。`TopicGradeManager` 按话题定级(L2/L1/L0)，grade 驱动 thought/tool 行的替换策略（L2=保留Elm，L1=全文Fct，L0=150ch截断） | `topic_manager.py` (495行), `__init__.py:392-432` | AGENTS.md §话题分割与等级管理 |
| **增量缓存** | `_A_stable_cache` / `_A_cache_turns` / `_A_cache_is_stale` 保障话题未切换时仅处理 delta 轮 | `__init__.py:223-225,465-618` | `docs/design/A-stage-增量缓存方案设计.md` |
| **bg_review 同步写 Fct** | 后台轮在 E-stage 直接同步写入 Fct/Hdl 列（代码生成摘要，不经过 F-stage daemon） | `__init__.py:741-757` | AGENTS.md §_on_pre_llm_call_v5 |
| **角色队列匹配** | 取代旧逐行 seq 对齐，thought→ca_thoughts 队列、tool→ca_tools 队列，一一对应 | `__init__.py:377-432` | ca-development/references 角色队列匹配 |
| **changes 列表格式** | Fct 输出从单对 `<stage_tag>/<core_change>` 改为多对单状态标签，`PAIR_PATTERN` 解析 | `ca/post_process.py:97-99` | ca-development SKILL.md §Fct JSON格式 |
| **`reasoning_content` 清理** | A-stage 替换时 pop `reasoning_content`/`tool_calls` 字段，减少保护区外 token | `__init__.py:401-412,420-429` | — |
| **命名统一** | L2/L1/L0 → Elm/Fct/Hdl，C-stage → F-stage | 全仓库 | AGENTS.md §术语 |

## 1. 动机

v4/v5 hybrid schema（turn_type / tool_sub_index / Elm / api_call_count 等混合主键）、内存 buffer `_tool_buffer`、C-stage PK 碰撞 L2 覆盖、conv_encoding 分离表等历史债积累过多。全线重写，不向后兼容。

**核心原则：**

- 每条原始数据到达时立即落盘，不经过内存 buffer
- 坐标 `(turn, seq)` 为唯一身份，不引入辅助键
- 存储层独立，写入端（E-stage）和消费端（C-stage / A-stage）松耦合
- 原始信息应存尽存，零开销

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│ E-stage  (写入)                                         │
│   pre_llm_call → write seq 0 (user)                     │
│   post_api_request → write seq+1 (thought)              │
│   post_tool_call → write seq+1 (tool result)            │
│   post_llm_call → write seq+1 (assistant_fin)          │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│ turn_stream (SQLite, WAL)                                │
│   PRIMARY KEY (session_id, turn, seq)                    │
└──────────────┬──────────────────────────────────────────┘
        ┌──────┴──────┐
        ▼              ▼
┌──────────────┐ ┌──────────────┐
│ C-stage       │ │ A-stage       │
│ (daemon 线程) │ │ (主线程)       │
│ 读 ↑ content │ │ 扫 conversation│
│ → LLM L1     │ │ _history       │
│ → UPDATE     │ │ 读 ↑ l1_text   │
│   l1_text    │ │ → 替换 content  │
│   l0_text    │ │                │
└──────────────┘ └──────────────┘
```

## 3. 数据结构

```sql
CREATE TABLE turn_stream (
  -- 坐标（唯一身份）
  session_id         TEXT    NOT NULL,
  turn               INTEGER NOT NULL,       -- 对话轮次，1-based
  seq                INTEGER NOT NULL,       -- 轮内序号，从 0 开始单调递增

  -- 原始数据核
  role               TEXT    NOT NULL,        -- 'user' | 'assistant' | 'tool'
  content            TEXT    NOT NULL,        -- L2 原文

  -- tool 行专用
  tool_name          TEXT,                    -- 工具名称
  tool_call_id       TEXT,                    -- 工具调用 ID（LLM 分配）
  args_json          TEXT,                    -- 工具参数 JSON
  status             TEXT,                    -- ok / error / blocked / cancelled / pending
  duration_ms        INTEGER,                -- 工具执行耗时

  -- thought / assistant 行专用
  tool_calls_json    TEXT,                    -- 完整 tool_defs 数组 JSON
  finish_reason      TEXT,                    -- stop / tool_calls / length
  usage_prompt_tokens     INTEGER,            -- LLM prompt token 实耗
  usage_completion_tokens INTEGER,            -- LLM completion token 实耗

  -- 标记
  biz_category       TEXT,                    -- null（普通）| 'bg_review'
  written_at         REAL,                    -- time.time() 写入时间戳

  -- C-stage 摘要（框架预留，当前固定用 l1）
  l1_text            TEXT,                    -- thought/tool 行：轻量摘要（generate_group_summary / tool_summarizer）；dialogue 行：C-stage LLM 生成的结构化摘要（5 字段 JSON）
  l0_text            TEXT,                    -- L0 一句话摘要（框架预留）

  PRIMARY KEY (session_id, turn, seq)
)
```

### turn、seq 编号规则

**turn**：conversation_history 中 user 消息的个数。pre_llm_call 进入时 conversation_history 已包含当前轮用户消息。

```python
turn = len([m for m in conversation_history if m.get("role") == "user"])
```

**seq**：同一 turn 内 E-stage 写每条新行时递增。纯对话轮：

```
seq 0: user
seq 1: assistant_fin
```

工具调用轮：

```
seq 0: user
seq 1: thought（LLM 的 reasoning / tool_calls 选择文本）
seq 2: tool A 返回结果
seq 3: tool B 返回结果
seq 4: thought2（二次 LLM 调用，如有）
seq 5: tool C 返回结果
seq 6: assistant_fin（最终回复）
```

role 字段用作核对：A-stage 扫 conversation_history 时，应按 role 与 turn_stream 对齐。不一致被视为监控告警信号。

### 各写入点使用的字段填表

| 写入点 | role | content | tool_name | tool_call_id | tool_calls_json | finish_reason | status | duration_ms | args_json | usage* | biz_category |
|--------|------|---------|-----------|-------------|----------------|--------------|--------|-------------|----------|--------|-------------|
| pre_llm_call | user | user_message | - | - | - | - | - | - | - | - | bg_review? |
| post_api_request | assistant | thought | - | - | tool_calls JSON | finish_reason | - | - | - | usage | 同当前轮 |
| post_tool_call | tool | result | tool_name | tool_call_id | - | - | status | duration_ms | args | - | 同当前轮 |
| post_llm_call | assistant | assistant_response | - | - | - | - | - | - | - | - | 同当前轮 |

## 4. E-stage：写入

### 4.1 _on_pre_llm_call — 写入 user 行

在调用 `_simple_mutation_mode`（A-stage）之前，先写入当前轮用户消息。

```python
def _on_pre_llm_call(**kwargs):
    session_id = kwargs["session_id"]
    plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return None

    conversation_history = kwargs.get("conversation_history", [])
    user_message = kwargs.get("user_message", "")
    engine = plugin._engine

    # ── E-stage：写 seq 0 ──
    turn = len([m for m in conversation_history if m.get("role") == "user"])
    engine._current_turn = turn
    engine._seq_counter[turn] = 0

    bg = (get_current_write_origin() == "background_review")
    engine.store.write_turn(
        session_id, turn, seq=0,
        role='user', content=user_message,
        biz_category='bg_review' if bg else None,
        written_at=time.time(),
    )

    if bg:
        return None  # bg_review 跳过 A-stage

    # ── A-stage 替换 ──
    return plugin.pre_llm_call(**kwargs)
```

### 4.2 _on_post_api_request — 写入 thought 行

LLM 返回 thought + tool_calls 时，写一条 assistant 行。同时写入工具占位行（可选——如不写占位行，工具行完全由 post_tool_call 创建）。

```python
def _on_post_api_request(**kwargs):
    ...
    turn = engine._current_turn
    seq = engine._seq_counter[turn] + 1
    engine._seq_counter[turn] = seq

    thought = getattr(assistant_message, "content", "") or ""
    tool_calls = getattr(assistant_message, "tool_calls", None)

    # 无工具且非 tool_calls finish → 纯对话，不写 thought 行（assistant_fin 在 post_llm_call 写）
    if not tool_calls and finish_reason != "tool_calls":
        return

    # 写 thought 行（含 thought L1 摘要）
    tool_defs = [...]  # 提取 tool_defs 数组
    thought_l1 = ToolSummarizer.generate_group_summary(thought)  # ≤100 字轻量摘要
    engine.store.write_turn(
        session_id, turn, seq=seq,
        role='assistant', content=thought,
        tool_calls_json=json.dumps(tool_defs),
        l1_text=thought_l1,  # thought L1（A-stage 替换用）
        finish_reason=finish_reason,
        usage_prompt_tokens=...,
        usage_completion_tokens=...,
        written_at=time.time(),
    )

    # 写 tool 占位行（预占 seq，供 post_tool_call 回填）
    for i, tc_def in enumerate(tool_defs, start=1):
        tool_seq = engine._seq_counter[turn] + i
        tc_id = tc_def["id"]
        engine.store.write_turn(
            session_id, turn, seq=tool_seq,
            role='tool', content='',
            tool_name=tc_def.get("function", {}).get("name", ""),
            tool_call_id=tc_id,
            status='pending',
            written_at=time.time(),
        )
        # tool_seq_map: tool_call_id → (turn, seq)，供 post_tool_call 回填
        engine._tool_seq_map[tc_id] = (turn, tool_seq)

    engine._seq_counter[turn] += len(tool_defs)
```

### 4.3 _on_post_tool_call — 回填 tool 行 + 生成 per-tool L1

```python
def _on_post_tool_call(**kwargs):
    ...
    turn, seq = engine._tool_seq_map.get(tool_call_id)
    if turn is None:
        # 异常时序：无预占行，现场插
        seq = engine._seq_counter.get(engine._current_turn, 0) + 1
        turn = engine._current_turn
        engine._seq_counter[turn] = seq

    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False) if result else ""

    # 生成 per-tool L1（同步，不经过 buffer）
    tc_def = {"id": tool_call_id, "type": "function",
              "function": {"name": tool_name, "arguments": args or {}}}
    result_entry = [{"content": content, "status": status}]
    try:
        l1_dict, l0_text = engine.tool_summarizer.summarize(tc_def, result_entry)
        l1_str = json.dumps(l1_dict, ensure_ascii=False) if l1_dict else ""
    except Exception as e:
        logger.warning("[CA] tool sumarize failed: %s", e)
        l1_str = ""
        l0_text = ""

    engine.store.write_turn(
        session_id, turn, seq=seq,
        role='tool', content=content,
        tool_name=tool_name, tool_call_id=tool_call_id,
        args_json=json.dumps(args) if args else None,
        status=status, duration_ms=duration_ms,
        l1_text=l1_str, l0_text=l0_text,
        written_at=time.time(),
    )
```

### 4.4 _on_post_llm_call — 写入 final assistant 行

```python
def _on_post_llm_call(**kwargs):
    ...
    if not user_message and not assistant_response:
        return

    turn = engine._current_turn
    seq = engine._seq_counter.get(turn, 0) + 1
    engine._seq_counter[turn] = seq

    # ── E-stage：写 final assistant ──
    engine.store.write_turn(
        session_id, turn, seq=seq,
        role='assistant', content=assistant_response,
        finish_reason='stop',
        written_at=time.time(),
    )

    # ── 快照恢复（同当前） ──
    ...

    # ── 启动 C-stage ──
    engine.process_turn_async(user_message, assistant_response, conversation_history)
```

### 4.5 会话生命周期

| 事件 | 行为 |
|------|------|
| on_session_start | engine 初始化（含 _current_turn=0, _seq_counter={}, _tool_seq_map={}） |
| on_session_end | 无操作（post_llm_call 已写完所有数据） |
| on_session_reset | engine.destroy() → 清理 turn_stream 对应 session_id 全部数据 |

## 5. C-stage：摘要生成

C-stage 在 daemon 线程异步执行，只写 Fct/Hdl，不碰 content。

### 5.1 写入路径

```python
def _run_c_stage(self, session_id, turn_index, prev_l1, *,
                 user_message, assistant_response, bg_review, ...):
    dialogue_ok = False
    try:
        if bg_review:
            # bg_review 路径：从 user_message 提取摘要，不调 LLM
            cleaned = {"core_change": user_message[:80], "_assemble_status": 0}
            dialogue_ok = True
        else:
            # 正常路径：调 LLM
            response_text, finish_reason = self._call_llm_for_l1(prev_l1, l2_prompt)
            cleaned = parse_and_clean(response_text)
            dialogue_ok = True

        l1_str = json.dumps(cleaned, ensure_ascii=False)
        l0_text = self._extract_l0(cleaned)

        # UPDATE turn_stream 的 l1/l0 列，不碰 content
        self._update_l1(session_id, turn_index, l1_str, l0_text)

    except Exception as e:
        # fallback
        ...

    finally:
        self._pending_tasks.pop(turn_index, None)
```

### 5.2 _update_l1 方法

```python
def _update_l1(self, session_id: str, turn_index: int,
               l1_text: str, l0_text: str) -> bool:
    """将 L1/L0 摘要回写到 turn_stream 的 seq=0 行，不覆盖 content。"""
    self.store.conn.execute(
        """UPDATE turn_stream
           SET l1_text=?, l0_text=?
           WHERE session_id=? AND turn=? AND seq=0""",
        (l1_text, l0_text, session_id, turn_index),
    )
    self.store.conn.commit()
```

sequel note: 为防止 daemon 线程写入时 main 线程已开启新 session 导致 db 关闭，_update_l1 配合 engine 引用锁。

## 6. A-stage：上下文替换

### 6.1 _simple_mutation_mode 游标替换

```python
def _simple_mutation_mode(self, result, conversation_history):
    self._saved_history_snapshot = [{**m} for m in conversation_history]

    # 尾部保护区：倒数第 3 个 user 消息之后
    tail_boundary = 0
    _user_count = 0
    for i in range(len(conversation_history) - 1, -1, -1):
        if conversation_history[i].get("role") == "user":
            _user_count += 1
            if _user_count >= 3:
                tail_boundary = i
                break

    # 游标：跟 E-stage 写 turn_stream 的 (turn, seq) 坐标同步
    turn = 0
    seq = 0
    replaced = 0
    skipped = 0

    for i, msg in enumerate(conversation_history):
        role = msg.get("role", "")

        # ── 游标推进 ──
        if role == "user":
            turn += 1
            seq = 0
            continue  # user 不替换

        seq += 1  # assistant / tool 行各占一个 seq

        # ── 尾部保护区：保留原文 ──
        if i >= tail_boundary:
            continue

        # ── 角色核对（仅调试/监控） ──
        ts_role = self._verify_role(turn, seq)
        if ts_role and ts_role != role:
            logger.warning(...)

        # ── 查这一行的 L1 ──
        l1 = self.store.read_l1(self._session_id, turn, seq)
        if l1:
            msg["content"] = l1
            replaced += 1
        else:
            skipped += 1

    logger.info("[CA] simple_mutation: replaced %d + skipped %d", replaced, skipped)
    return None
```

### 6.2 store.read_l1 方法

```sql
SELECT l1_text FROM turn_stream WHERE session_id=? AND turn=? AND seq=?
```

返回非空 Fct。C-stage 尚未完成的行 Fct 为 null → skipped。

## 7. 删除清单

| 删除目标 | 所在文件 | 原因 |
|---------|---------|------|
| `_tool_buffer` + `ToolGroupBuffer` | ca/__init__.py | E-stage 写即落盘替代 |
| `flush_tool_buffer()` | ca/__init__.py | 同上 |
| `_build_aligned_outcomes()` | ca/__init__.py | 旧 mutation，_simple_mutation 替代 |
| `_compute_tool_plan_v2()` | ca/__init__.py | 旧 tool_plan，不再需要 |
| `_on_api_response` buffer 写入逻辑 | ca/__init__.py | 改为直接 write_turn |
| `_on_pre_tool_call` buffer 写入逻辑 | ca/__init__.py | 同上 |
| `_on_post_tool_call` buffer 写入逻辑 | ca/__init__.py | 同上 |
| `cache.add_tool_group()` 的调用 | ca/__init__.py | 死数据链调用（函数体保留） |
| conv_encoding 表 + 全套读写 | ca/__init__.py | 坐标 (turn,seq) 已在主键 |
| `_persist_conv_encoding()` | ca/__init__.py | 同上 |
| `_load_conv_encoding()` | ca/__init__.py | 同上 |
| `EncodingRow` | ca/__init__.py | 同上 |
| `_phase_dialogue_turn_l1()` | ca/__init__.py | 旧路径 |
| C-stage 中写 Elm / turn_type / tool_sub_index | ca/__init__.py | 改为只 UPDATE l1/l0 |
| `write_turn()` 的 v4 兼容参数映射 | ca/store.py | 全用新参数 |
| `turn_type` 列 | store | 不再需要 |
| `Elm` 列 | store | content 就是 L2 |
| `tool_sub_index` 列 | store | seq 统一替代 |
| `api_call_count` 作为 PK 列 | store | 不在 PK 中（保留为普通列的可选项已放弃） |
| `test_tool_buffer.py` | tests/ | 整套 buffer 测试 |
| `test_v440.py` | tests/ | v4 兼容测试 |
| `test_pr3_injection.py` | tests/ | 旧 mutation 测试 |

## 8. 框架预留

当前 `Fct` / `Hdl` 列写入 C-stage 摘要，A-stage 固定取 `Fct`。后续可扩展：

- **层级选择**：A-stage 按 turn 属性（biz_category / token_budget / role）决定取 l1 还是 l0
- **多分支规则**：添加规则表，按 (turn, role, biz_category) 路由到不同摘要类型
- **增量摘要**：C-stage 不仅写当前轮，还可写跨轮聚合摘要（预留 Fct 字段为 TEXT 类型无需改 schema）

不影响 E-stage 的写入路径和数据结构。

## 9. 测试策略

| 层级 | 范围 | 方式 |
|------|------|------|
| E-stage | 各钩子独立写 turn_stream | 单 hook 调用 + 查 DB 验证 (turn,seq) 坐标正确 |
| C-stage | UPDATE l1/l0 不覆盖 content | mock LLM + 查 DB 确认 content 未被改 |
| A-stage | 游标计数 + 替换逻辑 | mock store.read_l1 + 验证 msg.content 变化 |
| 集成 | 完整对话轮 | 模拟 Hermes 钩子序列（pre→post_api→tool→post_llm）→ 查 DB  |
