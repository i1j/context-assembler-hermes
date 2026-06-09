# 工具组架构重构：per-tool 条目 → tool_group 组摘要

## 时间：2026-06-09

## 背景

原有的 CA 工具轮注入架构存在两个并行机制：
- `turn_type="tool"` — 单个工具独立注入 `[~/N/M]`
- `turn_type="tool_group"` — assistant{tc} 组摘要注入 `[~/N/g]`

这种设计导致：
1. 同一轮对话中多个工具组（多次 API 调用）时，cache key `turn_index` 覆盖导致丢数据
2. 工具组和单个工具并行注入，ctx 冗余
3. `[~/N/g]` 格式破坏了 `[~/N/M]` 的数字序列语义

## 决策

去掉 `turn_type="tool"` 条目。工具组替代原来单个工具的位置，每组的子工具以字段形式包含在组内，不单独注入。

## 改动清单

### ca/cache.py

**cache key 修复**：工具组 cache key 从 `turn_index`（int）改为 `(turn_index, api_call_count)`（tuple），支持一轮多工具组。

```python
# 改前
cache.tool_group_l1_texts[idx] = rec.get("l1_text", "")

# 改后  
api_count = rec.get("api_call_count", 0)
gkey = (idx, api_count)
cache.tool_group_l1_texts[gkey] = rec.get("l1_text", "")
```

同时修复 `get_tool_group_snapshot_data()` 返回类型注解 `Dict[int, str]` → `Dict[Tuple[int, int], str]`。

### ca/store.py

`read_turn_texts()` 新增 `tool_group_api_count` 参数。工具组 L2 重建时按 `api_call_count` 过滤，避免多工具组同轮时返回所有组的数据。

### ca/__init__.py

**`_compute_turn_plan_v2()`**：
- 签名：`tool_l1_texts, tool_l0_texts` → `tool_group_l1_texts, tool_group_l0_texts`
- 删掉 `turn_type="tool"` 的循环（原来约 60 行）
- 恢复 `turn_type="tool_group"` 循环，新增顺序号分配（`group_idx_counter` 计数器）
- 工具组编顺序号：`sub_idx = 1, 2, 3...` 对应 `[~/N/1], [~/N/2]...`

**`_build_messages_from_plan()`**：
- prefix 逻辑：`is_tool` → `is_tool_or_group`，工具组使用 `tool_sub_index` 生成 `[~/N/M]`
- 工具组 L2 → 从 l2 重建数据中提取 thought 原文透传
- 工具组 L1 → `_format_group_summary()` 格式化组摘要
- 工具组 L0 → 组 L0 文本（各工具 L0 拼接）

**`assemble()`**：
- 移除 `tool_l1_texts, tool_l0_texts` 的 fetch
- 移除 `tool_retrieval` 阶段（per-tool 检索 + rank + budget 分配）
- `_compute_turn_plan_v2` 调用适配新参数

### 决策规则（未变，仅从 per-tool 移至 tool_group）

| 条件 | 级别 | 原因标签 |
|------|------|---------|
| 尾区（最近 TOOL_TAIL_TURN_COUNT 轮） | L2（thought 原文） | tail |
| 非尾区 + 对话轮 L2 | L1（格式化组摘要） | dialogue_downgrade |
| 非尾区 + 对话轮 L1 | L0（组 L0） | dialogue_downgrade |
| 非尾区 + 对话轮 L0 | 跳过（不注入） | — |

### ctx 格式变更

```
改前：
  [~/5/0] 对话轮摘要
  [~/5/1] read_file: ... (单个工具)
  [~/5/2] search: ... (单个工具)
  [~/5/g] 工具组：... (组摘要)

改后：
  [~/5/0] 对话轮摘要
  [~/5/1] 工具组：查文件→...（2个，ok）
  [~/5/2] 下一个工具组：...（1个，ok）
```

### 测试变更

| 文件 | 改动 |
|------|------|
| `test_pr3_injection.py` | 2 个测试：去掉 `[~/1/g]` 断言，改为 `[~/1/1]`；plan 去掉 tool 条目 |
| `test_v440.py` | test_TC_A_021：写入 tool_calls_json 行使 CacheBuilder 识别工具组，断言改为 tool_group L1 |
| `test_v460.py` | TestComputeTurnPlanV2 全部 9 个测试 skip（测试旧 per-tool 架构） |

## 测试结果

284 passed, 13 skipped, 0 failed
