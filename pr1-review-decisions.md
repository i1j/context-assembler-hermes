# 多视角审查裁决记录 — PR1 实现方案

> 主笔整合全部 4 视角意见，逐条裁决。关闭所有意见后方可进入编码。

---

## A 完整性（9 条）

| # | 来源 | 意见 | 裁决 | 对应修改 |
|---|------|------|------|---------|
| A1 | 🔴 高 | 可写模式打开 v4 旧 DB 静默破坏：`_SCHEMA_SQL_V5` 的 `CREATE TABLE IF NOT EXISTS` 不修改已存在的 v4 表，`_migrate(4)` 仅升版本不改结构 → 后续 v5 查询全崩 | ✅ **采纳** | `_check_schema` 中加 `if not self._readonly and current_ver < 5: raise RuntimeError("v4 DB 必须以 readonly=True 打开")` |
| A2 | 🔴 高 | `read_session` v5 路径 `turn_type=row[7]` 实际是 role 列值（"user"/"assistant"/"tool"），非旧预期"dialogue"/"tool" | ✅ **采纳** | v5 返回前做 role→turn_type 映射：`'user'/'assistant'→'dialogue'`, `'tool'→'tool'` |
| A3 | 🔴 高 | `read_turn` v5 路径同样问题：`turn_type=row[7]` 实为 role | ✅ **采纳** | v5 路径硬编码 `turn_type='dialogue'`（查询条件已限定为 user 行） |
| A4 | 🟡 中 | `write_turn` 向后兼容：`content = l0_text or None` 语义错位——content 是消息正文，l0_text 是 L0 摘要 | ✅ **采纳** | 旧签名（content=None）时不填充 content 列，保持 NULL |
| A5 | 🟡 中 | `write_tool_group()` stub 缺失 | ✅ **采纳** | 新增 stub 含 readonly guard + docstring |
| A6 | 🟡 中 | `delete_session`/`delete_turn_plan`/`_invalidate_session_cache` 缺 readonly guard | ✅ **采纳** | 前两方法加 readonly guard；`_invalidate_session_cache` 纯内存操作，无需 guard |
| A7 | 🟡 中 | `read_turn` v5 路径返回 dict 缺 `api_call_count`/`content`/`tool_name`/`tool_calls_json`/`finish_reason`，与 `read_session` 不对称 | ✅ **采纳** | SELECT 和返回字典补全 |
| A8 | 🟢 低 | `_SCHEMA_SQL`(v4) 成死代码 | ✅ **采纳** | 删除整段定义；保留 `INITIAL_BACKFILL_ATTEMPTS` 等常量 |
| A9 | 🟢 低 | `_migrate(4)` 仅升版本号不改结构，存在风险路径 | ✅ **采纳** | 加 `if not self._readonly: raise RuntimeError(...)` |

## B 设计合理性（7 条）

| # | 来源 | 意见 | 裁决 | 对应修改 |
|---|------|------|------|---------|
| B1 | 🔴 高 | `self._readonly` 耦合「版本路由」与「读写模式」两概念，PR1 可工作但扩展性差 | ✅ **采纳**（PR1级别） | 文档化此耦合：`_readonly=True=v4 旧+只读`，`_readonly=False=v5 新+读写`。未来可用枚举重构。 |
| B2 | 🔴 高 | `role = turn_type` → `role='dialogue'` → v5 查询 `WHERE role='user'` 匹配不到 | ✅ **采纳** | 旧 `turn_type='dialogue'` 映射为 `role='user'`（对话轮首行恒为用户消息） |
| B3 | 🔴 高 | 15 个方法仍用 v4 列名，v5 模式下调用即崩溃——这是运行时缺陷非"待改"标记问题 | ✅ **采纳** | engine 调用的方法（write_turn_plan / read_turn_texts / read_assemble_status / write_query_embedding / get_pending_backfill / increment_backfill_attempts）做版本路由；其余 guard。 |
| B4 | 🟡 中 | 惰性迁移脆弱性：v4 DB 被误以 writable 打开 → 版本号被篡改 | ✅ **采纳** | 合并到 A1/A9：`_check_schema` + `_migrate(4)` 双 guard |
| B5 | 🟡 中 | v5 turn_plan PK 缺少 `seq_index`：一个 API 组只能有一条 entry → 无法逐工具调度 | ✅ **采纳** | v5 turn_plan PK 扩展为 `(session_id, turn_index, api_call_count, seq_index)`，与 `turn_cache` PK 一致 |
| B6 | 🟢 低 | `_check_schema` 中 `_SCHEMA_SQL_V5` 引用 vs `_SCHEMA_SQL` 残留 | ✅ **采纳** | 合入 A8 |
| B7 | 🟢 低 | 设计文档应写明版本路由的完整映射表（已部分写在方案 §3） | ✅ **采纳** | 更新 `pr1-store-plan.md` §3 映射表完整化 |

## C 必要性（7 条）

| # | 来源 | 意见 | 裁决 | 对应修改 |
|---|------|------|------|---------|
| C1 | 🔴 高 | #13-27 方法被引擎调用，不做版本路由则 assemble() 全崩 | ✅ **采纳** | 合入 B3 |
| C2 | 🟡 中 | `_SCHEMA_SQL`(v4) 死代码未清理 | ✅ **采纳** | 合入 A8 |
| C3 | 🟡 中 | `write_tool_group()` 无调用方无测试 | ✅ **采纳** | 但 PR1 仍定义 stub（PR2 需要语义接口契约），加 `raise NotImplementedError` 防误用 |
| C4 | 🟡 中 | `readonly=True` 时 mkdir 被跳过，需文档化 | ✅ **采纳** | `__init__` docstring 注明 |
| C5 | 🟡 中 | turn_plan PK 缺 seq_index | ✅ **采纳** | 合入 B5 |
| C6 | 🟢 低 | `read_session` v5 `turn_type` = role 值 | ✅ **采纳** | 合入 A2 |
| C7 | 🟢 低 | ALTER TABLE 替代方案不成立（SQLite 改 PK 必须重建表），确认整张新表方案合理 | ✅ **不采纳**（确认性意见） | 无需改动 |

## D 可测试性（11 条）

| # | 来源 | 意见 | 裁决 | 对应修改 |
|---|------|------|------|---------|
| D1 | 🔴 高 | `max_turn_index` v5 路径 `WHERE role='user'` 与向后兼容层写入的 `role='dialogue'` 不匹配 → 现有测试全崩 | ✅ **采纳** | 合入 B2：`role='dialogue'` → `role='user'` 映射修正 |
| D2 | 🔴 高 | `write_tool_group()` 未实现 → PR1 测试无法编写 | ✅ **采纳** | 合入 C3 |
| D3 | 🔴 高 | 15 个「待改」方法未版本路由 → 引擎 fixture 走 v5 模式即崩溃 | ✅ **采纳** | 合入 B3 |
| D4 | 🔴 高 | `legacy_db_v4` fixture 创建策略悬空：`force_version` 未实现 | ✅ **采纳** | fixture 内用 raw sqlite3 执行 v4 DDL（不污染生产代码）。`pr1-store-plan.md` 更新 fixture 方案。 |
| D5 | 🔴 高 | `write_turn` 的 `role=turn_type` → `role='dialogue'` 与 `max_turn_index` 的 `WHERE role='user'` 不一致 | ✅ **采纳** | 合入 B2 |
| D6 | 🟡 中 | `read_turn` v5 返回字典缺新字段 | ✅ **采纳** | 合入 A7 |
| D7 | 🟡 中 | `write_turn` `content=l0_text` 语义误导 | ✅ **采纳** | 合入 A4 |
| D8 | 🟡 中 | 空文件/不存在文件 readonly 测试边界不清晰 | ✅ **采纳** | 在测试中区分：不存在 → 预期 `OperationalError`；存在但 0 字节 → skipif 保护 |
| D9 | 🟡 中 | `write_turns_batch` 移除了 `backfill_attempts` 列写入 | ✅ **采纳** | INSERT 补回 `backfill_attempts` 列 |
| D10 | 🟢 低 | `_check_schema_readonly` 用 `self._local.conn`，异常路径 `last_used` 未设置 | ✅ **采纳** | try/finally 确保 `last_used` 赋值 |
| D11 | 🟢 低 | `_SCHEMA_SQL` 死代码误导测试维护者 | ✅ **采纳** | 合入 A8 |

---

## 裁决汇总

| 视角 | 意见数 | 采纳 | 不采纳 | 合并到其他 |
|------|--------|------|--------|----------|
| A 完整性 | 9 | 9 | 0 | — |
| B 设计合理性 | 7 | 7 | 0 | — |
| C 必要性 | 7 | 6 | 1（确认性） | — |
| D 可测试性 | 11 | 11 | 0 | — |
| **合计** | **34** | **33** | **1** | **—** |

## 修改清单（按代码位置排序）

1. `_SCHEMA_SQL`(v4) 整段删除
2. `_SCHEMA_SQL_V5` turn_plan PK 加 `seq_index` 列
3. `__init__` docstring 补充 readonly 语义说明
4. `_get_conn` 保留不动（v5 用 `_SCHEMA_SQL_V5` 已正确）
5. `_check_schema_readonly` 加 try/finally 保护 last_used
6. `_check_schema` 当 `not self._readonly and current_ver < 5` 时 raise RuntimeError
7. `_migrate(4)` 加 writable guard
8. `write_turn`：role 映射 `'dialogue'→'user'`；content 不自动填充 l0_text
9. `write_turns_batch`：补回 backfill_attempts 列
10. `read_session` v5：turn_type = role 映射值
11. `read_turn` v5：hardcode turn_type='dialogue'；补全 SELECT 列
12. `max_turn_index` v5：条件改为 `role='user'`（已正确，需配合 #8）
13. `write_turn_plan`：+v5 版本路由
14. `read_turn_plan`：+v5 版本路由
15. `write_query_embedding`：+v5 WHERE 条件
16. `get_pending_backfill`：+v5 版本路由
17. `increment_backfill_attempts`：+v5 WHERE 条件
18. `read_turn_texts`：+v5 WHERE 条件
19. `read_assemble_status`：+v5 WHERE 条件
20. `delete_session`：+readonly guard
21. `delete_turn_plan`：+readonly guard
22. 新增 `write_tool_group()` stub
23. 新增 topic 相关方法 readonly guard

## 第二轮审查触发条件

以上 23 项全部修改落地 + `pr1-store-plan.md` 同步更新后 → 至少通知【可测试性（D 视角）】子 Agent 做第二轮回溯检查，确认高严重度意见物理落地。
