# Debug 报告：Phase 1 turn 编号 off-by-one

## 发现时间

2026-06-17，A-stage 增量缓存实装过程中的边界场景测试。

## 发现过程

测试 Fct pending 场景时，设 DB turn 13 的 user 行 fct=NULL，期望增量路径 Step 4 检测到 pending 并标记 `_A_cache_is_stale=True`。实际检测未触发。

逐一排查后定位到：全量路径和增量路径的 turn 编号都偏移了 1。

## 根因

**`_simple_mutation_mode_v5` Phase 1 中 `current_turn=0` 起始，遇第 1 个 user→+1→1。但 DB turn 是 0-indexed**。

```
_on_pre_llm_call_v5 写 turn 号:
  turn = len([m for m in conversation_history if m.get("role") == "user"])
  第 1 个 user → conv_hist 含 0 个 user → turn=0
  第 2 个 user → conv_hist 含 1 个 user → turn=1
  ... 第 N 个 user → turn=N-1

_simple_mutation_mode_v5 Phase 1:
  current_turn = 0
  第 1 个 user → current_turn=1 ← 比 DB 大 1
  第 2 个 user → current_turn=2 ← 比 DB 大 1
  ... 第 N 个 user → current_turn=N ← 比 DB 大 1
```

Phase 2: `get_turn_ca_rows(store, sid, N)` → 查 DB turn N。但实际数据在 DB turn N-1。

## 影响范围

| 影响 | 分析 |
|---|---|
| **第 1 个 user 的 thought/tool 行** | 永远无法被 Fct 替换。Phase 2 查 DB turn 1 但实际数据在 turn 0。 |
| **第 2+ 个 user** | 查 DB turn N，但实际数据在 N-1。全量路径在这套偏移体系内"自洽"——所有 turn 都偏移 1，结果上每轮都查到错位的 turn 数据。 |
| **实际表现** | 第 1 轮对话通常无 tool_calls → 无 thought/tool 可替换 → 不显式出错。如果有 tool call 也不会触发错误（跳过替换，保留 Elm）。后续轮次虽然提取了错位的 turn 数据，但由于 tool calling 结构相似，内容影响小。 |
| **增量缓存** | 引入了这个 bug。Fct pending 检测看不到正确的 turn，全量路径的 turn 编号与增量路径的 cache_turns 继承编号系统一致。 |

总结：这是一个**"自洽但不正确"的 off-by-one**。老代码在 turn 编号上偏移 1，但因为全量路径在自己偏移系统内"符合预期"，且第 1 轮对话通常无 tool call，所以从未被发现。

## 修复

**全量路径 Phase 1** (`_simple_mutation_mode_v5`): `current_turn = -1`（非 0）

```python
# 改前
current_turn = 0
# 第 1 个 user → current_turn=1 → 查询 DB turn 1 → 错

# 改后
current_turn = -1
# 第 1 个 user → current_turn=0 → 查询 DB turn 0 → 正确
```

**增量路径 Step 3**: 与全量路径对齐，起始设为 `_A_cache_turns - 1`

```python
# 改前
current_turn = self._A_cache_turns

# 改后
current_turn = self._A_cache_turns - 1
```

## 关联变更

单元测试 `test_astage.py` 使用 `write_turn_v5(ca_engine.store, "test", 2, ...)` 按旧的 1-indexed turn 号写入。以下测试的 turn 参数同步修正：

| 测试 | 改前 | 改后 |
|---|---|---|
| `test_replaces_tool_lines_with_fct` | turn=2 (×2) | turn=1 |
| `test_tail_boundary_preserved` | turn=2, turn=4 | turn=1, turn=3 |

## 教训

- 测试驱动开发应包含"故意制造异常数据"的场景（如设 fct=NULL 确认 pending 检测）
- Hermes 的 turn 编号来源（`_on_pre_llm_call_v5` 的 `len()`) 与 Phase 1 的 user 计数法并不天然对齐
- 做增量/缓存优化时不能假设老代码的所有数值都是正确的参考系
