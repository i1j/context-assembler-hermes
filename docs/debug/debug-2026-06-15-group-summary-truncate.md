# 调试记录 2026-06-15 — `generate_group_summary` 无句尾标点不截断

**提交**: `9fb05cc`
**日期**: 2026-06-15 11:55

## 问题

`generate_group_summary` 的输入文本不含句尾标点（。！？.!?）时，`re.split` 返回单元素列表。
首轮 `result` 为空字符串（falsy），跳过截断检查，全量返回原文。

## 根因

截断逻辑依赖句尾标点作为分割点：
```python
sentences = re.split(r'(?<=[。！？.!?])\\s*', text)
```
无标点 → 单句 → 循环首轮 `result=""`（falsy）→ 不触发 `if len(result) + len(s) > 100` 检查
→ `result += s` → 返回原文本。

## 修复

最终返回值增加长度防护：`result` > 100 字时调用 `_safe_truncate()`（优先句尾截断，降级逗号，最后硬截断 100 字）。

## 文件变更

```
ca/tool_summarizer.py | 6 +++++-
```

## 验证

- `test_truncated` 从 FAIL 转为 PASS（200→100，assert≤110 成立）
- `tests/unit/test_tool_summarizer.py`：26/26 passed
- 内核测试：181 passed, 0 failed
