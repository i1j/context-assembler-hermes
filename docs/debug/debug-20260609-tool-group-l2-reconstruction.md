# 工具组 L2 全量重建 + flush_tool_buffer 摘要顺序修复

## 时间：2026-06-09

## 背景

CA 插件工具组的上下文结构存在两个问题：

1. **L2 全量数据不完整** — `read_turn_texts(turn_type='tool_group')` 只返回 assistant 行的 thought 文本作为 l2_text，缺失 tool_calls 定义和工具返回结果。`_extend_with_l2` 接到纯文本后 `json.loads` 失败，落到 `role='user'` 兜底。

2. **组摘要用 raw content 而非结构化 L1** — `flush_tool_buffer()` 中 `generate_group_summary()` 在 `tool_summarizer.summarize()` 之前调用，使用 raw `content[:80]` 作为工具结果摘要，而非工具轮结构化 L1 的 `result_summary`。

## 修改 1：`ca/store.py` — `read_turn_texts()` 工具组 L2 重建

**文件**：`ca/store.py:764-826`

**改前**：`turn_type='tool_group'` 分支只查 `role='assistant' AND seq_index=0`，返回 `content`（thought 纯文本）作为 l2_text。

**改后**：查该 turn 下所有 `role IN ('assistant', 'tool')` 行，按 `(api_call_count, seq_index)` 排序，组装完整消息数组：

```python
l2_text = [
  {"role": "assistant", "content": thought, "tool_calls": [{id, function}, ...]},
  {"role": "tool",      "content": result1, "tool_call_id": "call_xxx"},
  ...
]
```

- `tool_calls` 从 assistant 行的 `tool_calls_json` 解析
- tool 行按 `seq_index` 顺序排列
- l1/l0 仍从 assistant 行读取，不受影响

## 修改 2：`ca/__init__.py` — `flush_tool_buffer()` 摘要顺序

**文件**：`ca/__init__.py:649-719`

**改前**：
```
① raw content[:80] → tool_results_for_summary
② generate_group_summary() → group_summary（用的 raw content）
③ tool_summarizer.summarize() → 工具轮 L1/L0
④ 写 assistant{tc} 行 + tool 行
```

**改后**：
```
① tool_summarizer.summarize() → per_tool_summaries[]（工具轮 L1/L0）
② 写 tool 行（用 per_tool L1/L0）
③ 用 per_tool L1 result_summary 拼 group_summary
④ 用 per_tool L0 拼 group_l0（`|` 拼接，限前 5 个）
⑤ 写 assistant{tc} 行（l1=group_summary, l0=group_l0）
```

**效果**：
- 工具组 L0 从 `{"success":false}；{"total_count":20}` → `skill_view: File not found | search: *.py=20`
- 工具组 L1 的 `result_summary` 从 raw content 截断 → ToolSummarizer 结构化文本

## 测试

- test_tool_buffer.py: 全部通过
- test_pr3_injection.py: 13/13 通过
- test_v440.py: 全部通过
- test_store.py: 25/25 通过
- test_v460.py: 53/53 通过
- test_plugin.py: 预存 1 个失败（TestBreakerStateCleanup，与修改无关）
- 总计：105 passed, 0 regression

## 待完成

- 重启 Hermes tester profile 进程后修改才生效
- 工具组层级约束（父对话轮 target_level 检查）尚未实现
- thought 注入保留完整内容未实现
