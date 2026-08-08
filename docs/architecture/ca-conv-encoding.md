# CA 会话编码表 — 技术方案

## 1. 动机

原始 `conversation_history` 没有 `_turn_index` / `seq_index` 编码，`_build_aligned_outcomes` 无法将 Hermes 消息映射到 CA 的 turn_cache / turn_plan / tool_plan 条目。tool 行全部被 absorb。

## 2. 编码方案

统一两层编号：**(对话轮, 行序)**。

行序在当前轮内从 0 递增。行序=0 是用户提问的 L2 原文（始终保持，不做编码映射）。行序≥1 的行各对应 memory table 一条记录。

```
对话轮 1:
  conv[0]: user              ← 行序=0，不编码
  conv[1]: asst(tool_call)   ← 行序=1，映射到 DB
  conv[2]: tool              ← 行序=2，映射到 DB
  conv[3]: asst(文字回复)     ← 行序=3，映射到 DB（含 L1 摘要）

对话轮 2:
  conv[4]: user              ← 行序=0，不编码
  conv[5]: asst(tool_call)   ← 行序=1
  ...
```

## 3. 编码表结构

写入 `working_copy.db` 的 `conv_encoding` 表：

```sql
CREATE TABLE IF NOT EXISTS conv_encoding (
    session_id  TEXT NOT NULL,
    conv_idx    INTEGER NOT NULL,   -- conversation_history 中的绝对位置
    turn_index  INTEGER NOT NULL,   -- CA 对话轮编号
    seq_index   INTEGER NOT NULL,   -- DB 中的 seq_index（0=对话行，>0=工具行的 tool_sub_index）
    role        TEXT NOT NULL,       -- user / assistant / tool
    PRIMARY KEY (session_id, conv_idx)
);
```

**入口：** 每轮对话只写入行序≥1 的行（行序=0 的用户提问原文不记）。

**seq_index 的含义：**
- 0 = 该行是对话消息（user/asst），没有工具子编号
- >0 = 该行是工具消息，seq_index = tool_sub_index，对应 tool_plan 中的 (turn, sub_index)

**示例数据：**

```
session_id                     | conv_idx | turn_index | seq_index | role
20260613_224455_68adb8         | 1        | 1          | 0         | assistant
20260613_224455_68adb8         | 2        | 1          | 0         | tool
20260613_224455_68adb8         | 3        | 1          | 1         | tool
20260613_224455_68adb8         | 4        | 1          | 0         | assistant
20260613_224455_68adb8         | 6        | 2          | 0         | tool
20260613_224455_68adb8         | 7        | 2          | 0         | assistant
```

## 4. 编码写入时机（C-stage）

位置：`_run_c_stage()` → `write_turn()` / `persist_plan` 之后，backfill 触发之前

```python
# 本轮的 conversation_history 快照（从 Hermes pre_llm_call 传来）
for conv_idx, msg in enumerate(conversation_history):
    if _is_user_message(conv_idx):
        continue  # 行序=0 不编码
    turn = self._resolve_turn(conv_idx)
    seq  = msg.get("_seq_index", 0)
    role = msg.get("role", "")
    conn.execute("""
        INSERT OR REPLACE INTO conv_encoding
        (session_id, conv_idx, turn_index, seq_index, role)
        VALUES (?, ?, ?, ?, ?)
    """, (self._session_id, conv_idx, turn, seq, role))
```

**`_resolve_turn` 方法：** 按对话轮计数。遇到 user 角色就递增 turn_index，否则沿用上一轮的 turn_index。

## 5. 编码读取 + 组装（A-stage）

### 5.1 加载

```python
def _load_conv_encoding(self, session_id: str) -> Dict[int, EncodingRow]:
    """加载当前会话的编码表。返回 conv_idx → EncodingRow。"""
    rows = conn.execute("""
        SELECT conv_idx, turn_index, seq_index, role
        FROM conv_encoding WHERE session_id=? ORDER BY conv_idx
    """, (session_id,)).fetchall()
    return {r[0]: EncodingRow(r[1], r[2], r[3]) for r in rows}
```

### 5.2 逐行映射到 _build_aligned_outcomes

```python
# 替换原有 msg.get("_turn_index") fallback
_enc = encoding_map.get(_conv_idx)
if _enc:
    _turn = _enc.turn_index
    seq   = _enc.seq_index
    role  = _enc.role
else:
    # 行序=0 或新轮消息无编码 → fallback 到原有顺序计数
    _turn = None
```

## 6. _build_aligned_outcomes 中的使用

### 对话行（role=user 或 assistant，seq_index=0）

```python
if role == "user":
    outcomes.append(None)          # 始终保持原文

elif role == "assistant" and has_tool_calls:
    # 查 turn_plan，找该 tool_group 的 L1
    _key = (_turn, "tool")
    _group_entry = _plan_by_turn.get(_key)
    text = _group_entry.l1 if _group_entry else None
    outcomes.append(text)

elif role == "assistant" and not has_tool_calls:
    # 注入整轮对话 L1 摘要
    text = _format_l1_for_display(l1_texts.get(_turn, ""))
    outcomes.append(text or l0_texts.get(_turn, ""))
```

### 工具行（role=tool，seq_index>0）

```python
elif role == "tool":
    _t_entry = _tool_by_key.get((_turn, seq_index))
    if _t_entry:
        text = tool_l1_texts.get((_turn, seq_index), "")
        outcomes.append(text or tool_l0_texts.get((_turn, seq_index), ""))
    else:
        outcomes.append("")  # absorb
```

## 7. 改写（_mutation_mode）按编码回写

遍历 `conversation_history` 时，行序=0 直接跳过（原文保持），其余行从 outcomes 队列弹出对应 outcome 写入 `msg["content"]`。

```python
for conv_idx, msg in enumerate(conversation_history):
    if _is_user_message(conv_idx):
        continue  # 行序=0 原文保持
    outcome = outcomes.pop(0)
    if outcome is not None:
        msg["content"] = outcome
    elif msg.get("role") == "tool":
        msg["content"] = " "  # absorb
```

## 8. 时序

```
Turn N C-stage:
  write_turn(user, L2原文)              → DB row (turn=N, seq=0, role=user)
  write_turn(asst, tool_calls, L1)      → DB row (turn=N, seq=0, role=dialogue)
  write_turn(tool, tool_L1)             → DB row (turn=N, seq=0/1..., role=tool)
  store encoding:
    conv_idx[1] → (turn=N, seq=0)
    conv_idx[2] → (turn=N, seq=0)
    conv_idx[3] → (turn=N, seq=1)
    conv_idx[4] → (turn=N, seq=0)
    ...

Turn N+1 A-stage:
  _load_conv_encoding()                 → 拿到 turn N 之前的完整编码
  _build_aligned_outcomes():
    对 turn N+1 新消息：fallback 计数
    对 turn N 及以前的消息：编码表直接映射
```

## 9. 向后兼容

- `_load_conv_encoding` 返回空表（无历史编码或新会话）→ `_build_aligned_outcomes` 走原 fallback
- 不影响任何现有单元测试
- 行序=0 的用户行行为不变（原文保持）

## 10. 改动清单

### 新增

| 文件 | 改动 |
|------|------|
| `ca/__init__.py` | `EncodingRow` dataclass |
| `ca/__init__.py` | `_load_conv_encoding()` DB 读取 |
| `ca/__init__.py` | `_persist_conv_encoding()` C-stage 写入 |

### 修改

| 文件 | 位置 | 改动 |
|------|------|------|
| `ca/__init__.py` | `_run_c_stage()` finally 末尾 | 加 `_persist_conv_encoding()` |
| `ca/__init__.py` | `_compute_assemble_plan()` 开头 | 加 `_load_conv_encoding()` |
| `ca/__init__.py` | `_build_aligned_outcomes()` | 编码表替代 `msg.get("_turn_index")` fallback |
| `ca/__init__.py` | `_mutation_mode()` | 行序=0 跳过，其余按编码弹出 outcome |
