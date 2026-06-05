# CA v4.4.0 — t3 摘要降级调试记录

> 记录时间: 2026-06-05, 对话期间

## 发现

CA 引擎将一条系统自动触发的技能库维护 prompt 错误地摘要为 `{"core_change": "无有效增量"}`。

## 上下文

| 字段 | 值 |
|------|-----|
| Session DB | `20260605_021225_efd58c.db` |
| 轮次 | t3.0 (dialogue) |
| 时间 | 2026-06-04 18:15:03 (UTC) |
| CA cache 路径 | `ca_cache/20260605_021225_efd58c.db` |

## 原始 L2 文本

用户消息全文为系统技能库维护指令（~1500 字符）：
```
Review the conversation and update the skill library.
Be ACTIVE — most sessions produce at least one skill update, even if small.
...
Signals to look for (any one of these warrants action):
  • User corrected your style, tone, format, legibility, or verbosity.
  • User corrected your workflow, approach, or sequence of steps.
  • Non-trivial technique, fix, workaround, debugging path, or tool-usage pattern...
  • A skill that got loaded or consulted this session turned out to be wrong...
Preference order — prefer the earliest action that fits...
  1. UPDATE A CURRENTLY-LOADED SKILL.
  2. UPDATE AN EXISTING UMBRELLA...
```

## CA 输出

L0: `无有效增量`
L1: `{"core_change": "无有效增量"}`

## 问题分析

CA 引擎的 `assemble()` 中检测到本轮用户消息与当前会话 topic (CA 插件 agents.md 阅读) 不相关，遂判定为"无有效增量"，生成最小摘要。

**根因**: CA 的 OODA/L-stage 摘要逻辑缺乏对"系统自身触发消息"的识别与分类。系统消息（非用户自由输入）不应被降级，即使 topic 不同也应保留其指令意图。

## 影响

- A-stage 注入时，t3 的有效内容不会被纳入上下文
- 若后续对话需要回溯该技能库维护记录，CA 无法提供

## 观察记录者

Hermes agent (tester profile), 2026-06-05 session
