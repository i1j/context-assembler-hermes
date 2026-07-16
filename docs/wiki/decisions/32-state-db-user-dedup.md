---
source_files: ["__init__.py"]
---

# CE-005: Gateway 缓存路径 state.db user 双写清理

| 字段 | 值 |
|------|-----|
| 决策 ID | CE-005 |
| 版本 | v6.1 (2026-06-28) |
| 日期 | 2026-06-28 |
| 父节点 | P-001 (Plugin 层职责分离), CE-004 |

## 触发条件

CA 合规审计（2026-06-28）中发现：即使 CE 管线已停用（`should_compress() → False`），
**某些 Web UI 会话的 state.db 中仍存在 user 消息重复写入**。

### 现象

```
Session mqxysnl3qinm2k (src=cli) 的 state.db messages 表：

id=55127  user  "当前会话的state.db中的user有重复写入吗？"
id=55128  user  "当前会话的state.db中的user有重复写入吗？"  ← 精确重复，gap=1
```

| 检查项 | 结果 |
|--------|------|
| 受影响 session 数 | **67**（Web UI 会话的 100%） |
| CA cache 相关 | **100%** — 所有双写 session 都有 CA cache |
| 无双写 session | 352 个 CA session 无此问题（CLI 路径） |
| assistant/tool | 无双写 |

## 根因分析

### 排除项

| 路径 | 结论 |
|------|------|
| CA 写 state.db | CA 只写 `ca_cache/` 的 SQLite，不碰 `state.db`。排除 |
| CA hook 返回字符串导致双重处理 | `_on_pre_llm_call_v5` 始终返回 `None`。排除 |
| `_persist_session` 时 messages 已有重复 | DUP_TRACE 日志确认入口处 messages 中的 user dict 数量正确、id 唯一。排除 |
| CE 管线（旧 should_compress → abort 路径） | 已在 v6.0 停用。排除 |

### 真正根因（Hermes Gateway 三层叠加）

**第 1 层** — `_flush_messages_to_session_db` 的身份追踪保护（`run_agent.py:1662`）：

```python
if flushed_session_id != current_session_id or self._last_flushed_db_idx == 0:
    self._flushed_db_message_ids = set()  # 清空所有已 flush 的 id 记录
```

Python `id()` 追踪要求同一 dict 对象才能匹配。不同 turn 创建的 user dict 即使内容相同，
也有不同的 `id()`，无法通过此保护识别。

**第 2 层** — Gateway 每个新 turn 重置 `_last_flushed_db_idx = 0`（`run.py:14575-14579`）：

```python
if interrupt_depth == 0:
    if hasattr(agent, "_last_flushed_db_idx"):
        agent._last_flushed_db_idx = 0
```

Web UI 每次用户发消息都走 Gateway 缓存 agent 路径，每个新 turn 都设 `_last_flushed_db_idx = 0`
→ 下次 `_flush_messages_to_session_db` 时 `_flushed_db_message_ids` 被清空。

CLI 不使用 agent 缓存，`_last_flushed_db_idx` 持续增长，从不归零。

**第 3 层** — Gateway shutdown flush 无 `conversation_history`（`run.py:5128-5143`）：

```python
_session_messages = getattr(agent, "_session_messages", None)
_flush(_session_messages)  # 不传 conversation_history！
```

`_finalize_shutdown_agents` 在 agent 缓存清理时执行此 flush。由于不传 `conversation_history`，
`history_ids` 为空集 → 所有 dict 都被视为新消息 → 再次写入 state.db。

### 第 1 层 + 第 2 层 + 第 3 层 同时满足时的效果

1. turn N 结束 → `_session_messages` 持有完整消息列表
2. turn N+1 开始 → `_last_flushed_db_idx = 0` → `_flushed_db_message_ids = set()`
3. Gateway 清理缓存 → `_flush(_session_messages)` 无 `conversation_history`
4. `history_ids = {}`，`_flushed_db_message_ids = {}` → 所有 user dict 被重新写入

## 决策

### 备选方案

| 方案 | 描述 | 可行性 |
|------|------|--------|
| A | 修改 Hermes `_flush_messages_to_session_db` 不因 `== 0` 清空 identity 集 | ❌ 不动 Hermes 代码 |
| B | 修改 Gateway `_finalize_shutdown_agents` 传入 `conversation_history` | ❌ 不动 Hermes 代码 |
| C | CA 注册 `on_session_finalize` hook，事后清理重复 user 行 | ✅ CA 端唯一方案 |

### 选定方案 C：`on_session_finalize` 清理

CA 插件注册 `on_session_finalize` hook，在该 hook 中打开 state.db，
使用精确 SQL 删除相邻（gap=1）且内容相同的重复 user 行，保留第一次写入。

```python
ctx.register_hook("on_session_finalize", _on_session_finalize)
```

#### 清理 SQL

```sql
DELETE FROM messages WHERE id IN (
    SELECT b.id FROM messages a
    JOIN messages b ON b.id = a.id + 1
    WHERE a.session_id=? AND b.session_id=?
    AND a.role='user' AND b.role='user'
    AND (a.content = b.content OR (a.content IS NULL AND b.content IS NULL))
)
```

语义：仅删除 **第二条**（`b.id`）相邻且内容完全一致的 user 行。不会误删非相邻行或不同内容。

#### 安全措施

- 3 秒 `busy_timeout` 防 WAL 竞争
- 异常时 `logger.debug` 静默跳过，不崩溃
- 仅删 gap=1 + same-content 的 user 行，不碰其他角色和消息
- 使用 `get_hermes_home()` 获取 state.db 路径，profile 感知

#### 局限

- 不防止双写发生（CA 无法接触 agent 内部状态），仅事后清理
- 仅 end-of-session 时触发（`on_session_finalize` 在被缓存 agent 清理时被调用）
- 运行中的查询可能短暂看到双写

### 为何不做预防

CA 插件通过 `PluginContext` 仅能注册 hook 和工具，
从 hook kwargs 中仅能获取 `session_id`、`conversation_history` 等，**无法访问 agent 的
`_flushed_db_message_ids` / `_last_flushed_db_idx`**。预防式修复必须在 Hermes 侧。

## 影响

| 维度 | 影响 |
|------|------|
| 存储 | state.db 中重复 user 行在 session 结束时被清理 |
| 兼容性 | 向下兼容。清理 SQL 对无双写 session 无影响（0 rows affected） |
| 日志 | 清理到重复时写 `[CA] on_session_finalize: cleaned N dup user rows` |
| 运维 | 可通过 agent.log 的 `[CA] on_session_finalize` 行追踪清理情况 |

## 验证

```sql
-- 清理前（双写 session）
SELECT a.id, b.id FROM messages a JOIN messages b ON b.id = a.id + 1
WHERE a.session_id='mqxysnl3qinm2k' AND a.role='user' AND b.role='user'
AND a.content = b.content;

-- → 返回 1 行（a.id=55127, b.id=55128）

-- 清理后：应为 0 行
```
