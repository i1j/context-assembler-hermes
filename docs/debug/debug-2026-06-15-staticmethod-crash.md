# 调试记录 2026-06-15 — `_call_llm_for_fct` `@staticmethod` 错标

**提交**: `c60d8f8`
**日期**: 2026-06-15 12:17

## 问题

`_call_llm_for_fct` 错误标注为 `@staticmethod`，导致 F-stage 全部降级。

## 根因

`@staticmethod` 阻止了 `self` 的自动注入，参数发生系统性错位：
- `self` 参数收到了 `prev_fct`
- `prev_fct` 参数收到了 `elm_text`
- `elm_text` 无参可收 → `TypeError`
- 全部 Fct 降级为「本轮无新内容」

## 证据

- `agent.log` 连续 F-stage crash：`TypeError: missing elm_text`
- `turn_stream` 所有 user 行 Fct 的 `_assemble_status=1`（降级）
- 9/9 user 行 Fct 无 `stage_tag`（LLM 未实际调用）

## 修复

移除 `@staticmethod` 装饰器（方法访问 `self.stats` / `self._turn_counter`，是实例方法不是静态方法）。

## 文件变更

```
ca/__init__.py | 1 -
docs/debug/debug-2026-06-15-triple-source-verify.md | 86 ++++++++++++++
```

## 验证

269 tests passed, 0 failed。三源验证确认 F-stage 恢复正常。
