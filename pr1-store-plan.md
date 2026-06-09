# [实现方案 v3: PR1 — 存储重构 + 惰性迁移（R1+R7）]

> **评审状态**：已完成 4 视角多视角审查，34 条意见全部闭环（33 采纳 + 1 确认性不采纳）。
> **定位**：仅改动 `ca/store.py`。版本路由器 = `self._readonly`。
> **基线**：需求文档 `docs/tool-turn-refactor/tool-turn-refactor-req.md` §5 PR1。

---

## 1. 设计原则

### 1.1 版本路由：`self._readonly` 决定路径

```
SQLiteStore(path, readonly=False)  → v5 新 schema（新会话，可写）
  └─ _get_conn 用 ?mode=ro 时**不**走此路径
  └─ 所有方法用 v5 列（role / api_call_count / seq_index）

SQLiteStore(path, readonly=True)   → v4 旧 schema（旧会话，只读）
  └─ _get_conn 用 ?mode=ro 打开，跳过 PRAGMA/schema/migration
  └─ 所有读方法用 v4 列（turn_type / tool_sub_index），旧 ORDER BY
```

**注意**：`readonly` 在此版本同时控制了「版本路由」和「读写模式」两个维度——`readonly=True = v4 旧库只读`，`readonly=False = v5 新库读写`。这是 PR1 级别的简化，未来可用枚举重构。

### 1.2 强制约束：v4 DB 不可通过可写模式打开

```python
# _check_schema 中：
if not self._readonly and current_ver < 5:
    raise RuntimeError("v4 DB 只能以 readonly=True 打开。请用迁移脚本升级到 v5。")
# _migrate(4) 中：
if not self._readonly:
    raise RuntimeError("v4→v5 惰性迁移不可通过可写路径触发，请用 migration_v5.py 脚本。")
```

### 1.3 不做的（划清边界）

- 不修改 engine（`ca/__init__.py`）— PR2 做
- 不修改 lstage / cache / 插件 __init__ — PR2/PR3 做
- 不修改现有测试的调用签名 — 向后兼容保障
- 不做旧 DB 数据迁移重建 — 由独立的 `migration_v5.py` 脚本处理

---

## 2. 版本路由 V4↔V5 映射表

### 2.1 列映射

| v4 列/条件 | v5 等价列/条件 |
|-----------|---------------|
| `turn_type='dialogue'` | 对话轮行：`role IN ('user','assistant')` |
| `turn_type='tool'` | 工具行：`role='tool'` |
| `turn_type AS role` | `='dialogue'` → 写 `role='user'`（对话轮首行恒为 user） |
| `tool_sub_index` | `seq_index` |
| `turn_type='dialogue' AND tool_sub_index=0` | `api_call_count=0 AND seq_index=0`（user 行） |
| `turn_type='dialogue'`（查询对话轮行） | `api_call_count=0 AND seq_index=0`（user 行），或 `api_call_count=999999`（final 行） |
| ORDER BY `turn_index ASC` | 对话轮用 `turn_index, api_call_count, seq_index` |
| `backfill_attempts`（v4 列存在） | `backfill_attempts`（v5 列仍存在） |

### 2.2 返回字典 `turn_type` 映射

v5 路径中，返回的 `TurnRecord` 字典需将 `role` 值翻译回旧 `turn_type` 语义：

| `role` 值 | 映射 `turn_type` |
|-----------|------------------|
| `'user'` | `'dialogue'` |
| `'assistant'`（api=0 且 seq=0） | `'dialogue'` |
| `'assistant'`（api>0 且 seq=0） | `'tool'`（assistant{tc} 行） |
| `'tool'` | `'tool'` |

`read_turn` v5 路径（查询条件固定为 user 行）：直接硬编码 `turn_type='dialogue'`。

### 2.3 方法版本路由汇总

| 方法 | v5 路径 | v4 readonly 路径 | 备注 |
|------|---------|------------------|------|
| `write_turn` | 新列 INSERT + 向后兼容映射 | readonly guard | ✅ 已实现 |
| `write_turns_batch` | 新列 INSERT | readonly guard | ✅ 已实现 |
| `read_session` | 新列 SELECT + role→turn_type 映射 | 旧列 SELECT + 旧 ORDER BY | ✅ 已实现 |
| `read_turn` | `WHERE api=0 AND seq=0` SELECT | `WHERE turn_type='dialogue'` | ✅ 已实现 |
| `max_turn_index` | `WHERE role='user' AND api=0` | `WHERE turn_type='dialogue'` | ✅ 已实现 |
| `write_turn_plan` | 新 PK INSERT（含 api_call_count, seq_index） | readonly guard | 🔧 待改 |
| `read_turn_plan` | 新 ORDER BY（按 turn, api, seq） | 旧 ORDER BY | 🔧 待改 |
| `read_turn_texts` | WHERE 用 `(turn, api, seq)` 全主键 | WHERE 用 `(turn_type, tool_sub_index)` | 🔧 待改 |
| `read_assemble_status` | 同 `read_turn_texts` | 同上 | 🔧 待改 |
| `write_query_embedding` | WHERE 用 `api=0 AND seq=0 AND role='user'` | WHERE 用 `turn_type='dialogue'` | 🔧 待改 |
| `read_query_embedding` | 同上（纯读，跨列兼容） | 同上 | 🔧 待改 |
| `get_pending_backfill` | WHERE 用 `role` 代替 `turn_type` | 旧列 WHERE | 🔧 待改 |
| `increment_backfill_attempts` | WHERE 用 `(turn, api, seq)` PK | 旧列 UPDATE | 🔧 待改 |
| `delete_session` | readonly guard | — | 🔧 待加 |
| `delete_turn_plan` | readonly guard | — | 🔧 待加 |
| `list_session_ids` | 跨列兼容，无需改 | — | ✅ 无需改 |
| `get_max_token_offset` | 跨列兼容，无需改 | — | ✅ 无需改 |
| `read_turn_l1_fields` | WHERE 用 role 条件 | WHERE 用 turn_type | 🔧 待改 |
| `read_topic_*`(3个) | readonly guard（PR2 再实现 v5 版本） | 旧列 | 🔧 待加 guard |
| `upsert_turn_plan_topic` | readonly guard | — | 🔧 待加 guard |
| `write_tool_group()` | **新增** — stub 含 readonly guard | — | 🔧 新增 |

---

## 3. `_SCHEMA_SQL_V5` 设计

### 3.1 turn_cache 表（与 analysis.md §2.2 一致）

```
PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)

消息独立列:  role, content, tool_call_id, tool_name, tool_calls_json, finish_reason
元数据列:    api_request_id, duration_ms, status, error_type, error_message, usage_json
CA 摘要列:  l0_text, l1_text, l0_embedding, l1_embedding, bm25_tokens,
            token_offset, l2_text, _assemble_status, backfill_attempts, query_embedding
```

### 3.2 turn_plan 表（PK 含 seq_index 以支持逐工具调度）

```
PRIMARY KEY (session_id, turn_index, api_call_count, seq_index)

决策列:   target_level, decision_reason
Token列:  l2_tokens, summary_tokens, tokens_saved
检索列:   rrf_score, upgrade_rank
预算列:   budget_remaining
话题列:   topic_group
```

### 3.3 v4 遗留

`_SCHEMA_SQL`(v4) 在 PR1 中整段删除。`_SCHEMA_SQL_V5` 全面替代。

---

## 4. 向后兼容策略

### 4.1 `write_turn` 新参数（Optional，默认值 auto-fill）

```python
def write_turn(self, session_id, turn_index, *,
               l0_text="", l1_text="", ..., turn_type="dialogue", tool_sub_index=0, ...,
               # v5 新增（Optional）
               api_call_count=None, seq_index=None,
               role=None, content=None, tool_call_id=None, tool_name=None,
               tool_calls_json=None, finish_reason=None,
               api_request_id=None, duration_ms=None, status=None,
               error_type=None, error_message=None, usage_json=None):
    if self._readonly: return False
    # 向后兼容映射
    if api_call_count is None: api_call_count = 0
    if seq_index is None: seq_index = tool_sub_index or 0
    if role is None:
        role = 'user' if turn_type == 'dialogue' else turn_type  # ★ 关键修复
    # content 不自动填充（旧语义 l0_text 不是消息正文）
    # ★ content = content (不做回退填充)
```

### 4.2 旧调用方不受影响

- 旧调用方传 `turn_type='dialogue'` → `role='user'` ✅
- 旧调用方传 `tool_sub_index=5` → `seq_index=5` ✅
- `max_turn_index` 用 `WHERE role='user'` 匹配 ✅

---

## 5. `write_tool_group()` 接口

```python
def write_tool_group(self, session_id, turn_index, api_call_count,
                     rows: List[Dict]) -> bool:
    """批量写入一个工具组的全部消息行（assistant{tc} + tool×N）。
    
    rows: 按拓扑排序后的消息列表，每行含 role/content/tool_call_id/...
    
    PR1 定义接口但暂不调用（PR2 由 _flush_tool_buffer 调用）。
    实现：BEGIN TRANSACTION → 逐行 INSERT → COMMIT → 返回 True/False。
    """
    if self._readonly:
        logger.warning("write_tool_group called on readonly store, skipping")
        return False
    raise NotImplementedError("PR2 实现")
```

---

## 6. `legacy_db_v4` fixture 方案

测试用 v4 种子库通过 raw sqlite3 直接执行 v4 DDL 创建：

```python
@pytest.fixture
def legacy_db_v4(tmp_path):
    """创建 schema_version=4 的旧格式种子 DB。"""
    import sqlite3
    db = tmp_path / "legacy_v4.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS turn_cache (
            session_id TEXT, turn_index INTEGER, turn_type TEXT, tool_sub_index INTEGER,
            l0_text TEXT, l1_text TEXT, l0_embedding BLOB, l1_embedding BLOB,
            bm25_tokens TEXT, token_offset INTEGER, l2_text TEXT,
            _assemble_status INTEGER, backfill_attempts INTEGER, query_embedding BLOB,
            created_at TEXT,
            PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)
        );
        CREATE TABLE IF NOT EXISTS turn_plan (
            session_id TEXT, turn_index INTEGER, turn_type TEXT, tool_sub_index INTEGER,
            target_level TEXT, decision_reason TEXT,
            l2_tokens INTEGER, summary_tokens INTEGER, tokens_saved INTEGER,
            rrf_score REAL, upgrade_rank INTEGER, budget_remaining INTEGER,
            topic_group INTEGER, created_at TEXT,
            PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)
        );
        CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO _meta (key, value) VALUES ('schema_version', '4');
    """)
    conn.commit()
    conn.close()
    return str(db)
```

---

## 7. 代码修改清单（23 项）

详见 `pr1-review-decisions.md` §修改清单。按以下顺序执行：

1. 删除 `_SCHEMA_SQL` 死代码
2. 更新 `_SCHEMA_SQL_V5`（turn_plan PK 加 seq_index）
3. 修复 `write_turn` role 映射 + content 不自动填充
4. `write_turns_batch` 补回 backfill_attempts + 向后兼容
5. `read_session` v5 增加 role→turn_type 映射
6. `read_turn` v5 补全 SELECT 列 + hardcode turn_type
7. `_check_schema_readonly` try/finally
8. `_check_schema` + `_migrate(4)` writable guard
9. `write_turn_plan` / `read_turn_plan` 版本路由
10. `write_query_embedding` / `read_query_embedding` v5 WHERE
11. `get_pending_backfill` / `increment_backfill_attempts` 版本路由
12. `read_turn_texts` / `read_assemble_status` v5 WHERE
13. `delete_session` / `delete_turn_plan` readonly guard
14. 新增 `write_tool_group()` stub
15. `read_turn_l1_fields` + topic 方法 readonly guard
16. `upsert_turn_plan_topic` readonly guard

---

## 自检

- [x] 多视角审查 34 条意见全部关闭（33 采纳 + 1 确认性不采纳）
- [x] role 映射修复：`turn_type='dialogue' → role='user'`
- [x] turn_plan PK 含 seq_index，支持逐工具调度
- [x] engine 调用的方法全部版本路由完成
- [x] 未调用的方法有 readonly guard，不会运行时崩溃
- [x] _migrate(4) writable 路径被 RuntimeError 阻断
- [x] v4 死代码已删除
- [x] 测试 fixture 方案明确
