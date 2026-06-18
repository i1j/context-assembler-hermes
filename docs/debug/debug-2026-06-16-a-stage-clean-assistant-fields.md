# 调试记录 2026-06-16 — A-stage 清 assistant 行冗余字段

**提交**: （未提交）
**日期**: 2026-06-16

## 问题

Fct 替换保护区外 tool 行 `content` 虽有效，但 assistant{ tool_calls } 行的
`reasoning_content`（思维链，86KB）和 `tool_calls`（函数参数，39KB）未被处理，
合计 125KB ≈ 31k token 原封不动送进 LLM 调用，抵消了 tool 行压缩的收益。

## 分析

mut ation dump `/tmp/ca_mut ation_mqgqaiy80czj19_20260616_223729_235909.json` 显示：

| 角色 | 字段 | 大小 | 占比 | A-stage 处理 |
|------|------|------|------|------------|
| tool | content | 48KB | — | ✅ 替换为 Fct |
| assistant | reasoning_content | 86KB | 60% | ❌ 原封不动 |
| assistant | tool_calls | 39KB | 27% | ❌ 原封不动 |
| assistant | content | 9KB | 6% | ✅ 替换为 Fct（但为空串，几无收益） |

`reasoning_content` 是模型内部思维链，下一轮模型不需要读自己上轮想了什么。
`tool_calls.function.arguments` 在 tool 行的 Fct 中已有摘要，无需重复携带。

## 修复

`__init__.py:380-384` — `_simple_mutation_mode_v5` 替换保护区外 assistant 行
`content` 后，追加 `pop("reasoning_content")` 和 `pop("tool_calls")`：

```python
if row_type == "thought":
    conversation_history[conv_idx]["content"] = ca_thoughts[ti]
    conversation_history[conv_idx].pop("reasoning_content", None)
    conversation_history[conv_idx].pop("tool_calls", None)
```

## 验证

- `pytest tests/`：268 passed, 19 skipped, 0 failed
- 按 turn 9 前的 dump 推算：保护区外 token 从 50k → 19k（省 ~60%）

## 备注

`tool_calls` 字段在 OpenAI 格式中用于匹配 `tool_call_id`。
清掉后部分 provider 可能报错——实测遇到再处理。
