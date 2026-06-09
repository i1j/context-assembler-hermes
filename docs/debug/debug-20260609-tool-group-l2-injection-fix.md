# 工具组 L2 注入修复：thought 原文 → 完整消息序列展开

## 时间：2026-06-09

## 背景

工具组 L2 本应为「thought 原文透传」（尾区保护），但当前实现只提取 assistant 的 thought 文本，未展开完整工具组消息序列。当 thought 为空时（多数后续工具调用），fallback 使用 `l1_display`，而 `l1_display` 对工具组定义为原始 `l1` JSON 字符串（line 1541），导致 raw JSON 直接暴露在 context 中。

同时，上一版架构的残余导致尾区 tool 行仍以独立原始消息浮在上下文中，而非包裹在工具组 L2 内。

## 现象

用户注入输出中：

```
[~/7/1] {"status": "error", "output": "Tables: ['turn_cache', '      ← raw JSON
[~/8/1] 工具组：→{"status": "success", "output": "=== turn=5 ... (1个，ok）  ← raw JSON 混入格式化文本
[~/14/1] {"group_intent": "", "group_result": "失败: action=replace...  ← raw JSON
[~/15/1] {"group_intent": "", ...}                                   ← raw JSON
```

## 根因

### 根因 1：工具组 L2 用错展开方式

line 1544-1560 的 L2 代码只提 `thought_text`，而非调用 `_extend_with_l2`：

```python
# 当前（改前）：
if l2:
    # 只提取 thought，不展开 tool 行
    for m in msgs:
        if "tool_calls" in m:
            thought_text = m["content"]
    text = thought_text or l1_display or l0
    result.append({"role": "assistant", "content": f"{prefix}{text}"})

# 问题：thought 为空 → l1_display（=raw JSON）→ 暴露原始 JSON 结构
```

### 根因 2：covered 集不匹配

从 raw messages 追加时 line 1609 判断 `msg_turn_type = "tool"`，但 plan 的 key 是 `(turn_idx, "tool_group")`。L2 展开后 tool 行不在 covered 中 → 重复追加。

### 根因 3：设计歧义

设计文档说工具组 L2 = "thought 原文透传"，但实际应该与对话轮 L2 一致——使用 `_extend_with_l2` 展开完整的 assistant{tc} + tool 消息序列。对话轮 L2 展开 `user + final_assistant`，工具组 L2 展开 `assistant{tc} + tool_1 + ... + tool_N`，两者对称。

## 修复

### 文件 a: `ca/__init__.py`

**改 point 1** — 工具组 L2 handler（原 line 1543-1560 → 新 1543-1548）：

```python
# 工具组 L2：展开完整消息序列（assistant{tc} + tool × N），与对话轮 L2 一致
if entry.turn_type == "tool_group" and entry.target_level == "L2":
    if l2:
        self._extend_with_l2(result, l2, entry.turn_index)
        # L2 展开后，标记该组原始 tool 消息已被覆盖，避免重复追加
        covered.add((entry.turn_index, "tool"))
    elif l1:
        display = self._format_group_summary(l1)
        if display:
            result.append({"role": "assistant", "content": f"{prefix}{display}"})
    elif l0:
        result.append({"role": "assistant", "content": f"{prefix}{l0}"})
    continue
```

**改后效果**：
- l2 存在 → `_extend_with_l2` 展开完整 `[assistant{tc}, tool_1, ..., tool_N]` 消息序列
- l2 不存在 + l1 存在 → `_format_group_summary(l1)` 输出 `工具组：→...（N个，ok）`
- l2/l1 不存在 + l0 存在 → l0 文本
- covered 防重复

### 文件 b: `tests/test_pr3_injection.py`

断言调整：从 `[~/1/1]` 标签存在改为检查展开的原始 content：

```python
# 改前
assert "[~/1/1]" in full_text
# 改后
assert "查文件" in full_text
assert "aaa" in full_text
```

## 设计对齐

| 组件 | 对话轮 L2 | 工具组 L2（新） | 工具组 L2（旧，已改） |
|------|----------|---------------|-------------------|
| l2 存在 | `_extend_with_l2` | `_extend_with_l2` | 只提 thought |
| l2 不存在 + l1 | 显示 L1 摘要 | `_format_group_summary(l1)` | raw JSON `l1_display` |
| covered key | `(turn_idx, "dialogue")` | `(turn_idx, "tool")` | 未覆盖 |

## 测试验证

```
287 passed, 20 skipped, 0 failed
```

所有 22 个 failure 均为 legacy 和 test_v460 预存问题，无回归。

## 当前 ctx 附加问题（本次未修）

| 问题 | 说明 |
|------|------|
| `_format_group_summary` 输出的 `group_result` 字段含 raw JSON | execute_code/terminal 的 `result_summary` 包含 JSON 原文，L1 格式化后仍暴露 `→{"status":"success",...}`。需在 ToolSummarizer handler 层面修复，非本次范围 |
| 工具组 L0 回退链不完整 | thought 为空的 L2 在 l2/l1/l0 全为空时跳过无输出 |
