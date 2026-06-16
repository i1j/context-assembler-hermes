# 调试记录 2026-06-15 — _simple_mutation_mode 简化注入

## 背景

旧的 `_mutation_mode` 依赖 `_build_aligned_outcomes` → `(turn_plan, tool_plan, cache, 编码表)` 四层链路。其中 cache 的 tool key `(turn, seq)` 存在 per-api-group vs per-turn 的语义碰撞（`flush_tool_buffer` 写 seq=1,2,1,2，CacheBuilder 按 `(turn, seq)` 读 → 后一组覆盖前一组），编码表因线程安全问题从未生效。

简化方案绕过全部四层，直接从 DB 查 L1。

## 改动

**文件**: `plugins/ca_assembler/__init__.py`

### 新增 `_simple_mutation_mode`

```python
def _simple_mutation_mode(self, result, conversation_history: list) -> Optional[str]:
```

规则：

| 条件 | 行为 |
|------|------|
| 尾部保护区（倒数第 3 个 user 之后） | 全部原文保留 |
| `user`（全文） | 原文保留 |
| `assistant_fin`（全文） | 原文保留 |
| `assistant{tc}`（保护区外） | `store.read_turn_texts(…, 'tool_group', api_count)` → `_format_tool_group_assembly(l1)` |
| `tool`（保护区外） | `store.read_tool_rows_for_group(…, api_count)` → 按 `seq_index` 取 `Fct` |

### 不依赖

- `turn_plan` / `tool_plan` / `bypass_turns`（来自 `_AssemblePlanResult`，仅用 `result` 的 degraded 判断）
- `AssemblyCache`（`tool_Fcts` 的 `(turn, seq)` 碰撞 key）
- `_conv_encoding` / `conv_encoding` DB 表
- `_build_aligned_outcomes()` 方法
- `_compute_tool_plan_v2()` 方法
- bg_review 特殊处理（不再区分 bg/non-bg，统一规则）
- `_turn_index` / `_seq_index` / `_api_call_count` 元数据（已丢失）

### 依赖

- `store.read_turn_texts(sid, turn, 'tool_group', tool_group_api_count=group)` — DB 原生 `(turn, api, seq)` 精确过滤
- `store.read_tool_rows_for_group(sid, turn, group)` — 同上
- `_format_tool_group_assembly(l1)` — L1 纯文本 thought ≤100 字

### 调用入口变更

```
pre_llm_call → self._simple_mutation_mode(result, conversation_history)
```


### 旧代码保留

`_mutation_mode` 方法保留（未被删除），但不再被入口调用。

## 尾部保护区算法

```
从 conversation_history 末尾往前扫
  role=user → _user_count++
  _user_count >= 3 → tail_boundary = i（该索引及之后保留）
```

## 顺序计数

从头向前扫，不依赖消息元数据：

```
role=user           → turn++, group=0, tool_seq=0
role=assistant{tc}  → group++, tool_seq=0
role=tool           → tool_seq++
```

## DB 查询定位

同 turn 多 API group 场景举例：

```
conversation_history: [user, asst{tc}g1, tool, tool, asst{tc}g2, tool, tool, asst_fin]
顺序计数:              turn1   g1           g1,1  g1,2  g2           g2,1  g2,2   -

DB 查询:
  read_turn_texts(sid, turn=1, 'tool_group', api=1) → g1 的 L1
  read_tool_rows_for_group(sid, turn=1, api=1)     → g1 的 2 个工具行 L1 (seq=1,2)
  read_turn_texts(sid, turn=1, 'tool_group', api=2) → g2 的 L1
  read_tool_rows_for_group(sid, turn=1, api=2)     → g2 的 2 个工具行 L1 (seq=1,2)

无碰撞，因为 read_tool_rows_for_group 原生按 (turn, api) 过滤，
不经过 cache 的 (turn, seq) 平铺 key。
```

## 测试

```
397 passed, 20 skipped, 0 failed
```

变更的测试：`test_plugin.py::test_extracts_ca_markers_from_assemble` — 移除对 `_build_aligned_outcomes` 的 mock 断言，改为 mock store 查询。
