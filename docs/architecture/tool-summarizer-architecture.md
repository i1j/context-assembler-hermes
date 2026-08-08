---
title: 工具摘要引擎
slug: tool-summarizer
category: architecture
decisions: [design-fct-hdl-per-tool]
updated: 2026-06-18
---

# 工具摘要引擎 (ToolSummarizer)

## 职责

为每个 tool call 生成两层级摘要：
- **Fct (fct)**: 保留完整互信息的 JSON dict → A-stage 展开时供 LLM 查看
- **Hdl (hdl)**: 100 字提示线索 → A-stage 话题回顾时供 LLM 判断是否展开

## 调用链

```
_on_post_tool_call_v5() → write_turn_v5()
  ├── turn_stream: Elm 原始数据
  ├── Fct (fct): tool_summarizer.summarize()
  └── Hdl (hdl): 同上方法返回
```

## 分发机制

`summarize()` 按 tool name 做方法名匹配：

```python
sanitized = tool_name.replace(".", "_").replace("-", "_")
handler = getattr(self, f"_summarize_{sanitized}", None)
if handler:
    return handler(tool_call_msg, tool_responses)
# fallback → 通用优先级字段提取
```

## 专用 Handler 一览

| Handler | Fct 策略 | Hdl 策略 |
|---------|---------|----------|
| **terminal** | pytest 检测 → 结构化测试结果；否则关键输出前5行+命令简写 | 命令[:40] : 关键输出首行[:50] |
| **execute_code** | 委托 terminal | 同 terminal |
| **read_file** | **Elm 全量** — 文件原文 content + total_lines | 路径(范围, N行) |
| **write_file** | 路径 + bytes，省略 content（已在 tool_args） | write_file: 路径 |
| **patch** | 目标 path + replace_all 标记 | patch: 路径 |
| **search_files** | pattern + 匹配数 + 目录分组 | N hits |
| **skills_list** | count + 名称前5 | N skills |
| **skill_view** | **结构化 Markdown** — frontmatter + 高价值章节全量 + 省略索引 | 名 — 描述 (N行) |
| **skill_manage** | action + name + file_path + status | action name (status) |
| **memory** | action + target + content_preview | action target (status) |
| **todo** | **全量 task 列表** — 每项 content+status | 计数 + 首个任务内容 |

## 通用 Fallback

1. VIP (tool_name, error, status, command, instruction) — 全量
2. P0 (result, summary, message, conclusion, output) — 全量+截断，取首个非空
3. P1 (args, parameters, input, metadata, context) — 全量+截断，P0 无结果时 fallback
4. 兜底 → status → "无返回数据"

## 设计原则

1. **Fct 保留互信息** — 不丢失 LLM 需要的内容
2. **Hdl 提供回顾线索** — 100 字内回答"这轮是什么操作"
3. **去冗余** — tool_args 已有的字段不在 result_summary 重复
4. **结构化优先** — 认识结构的 tool 用结构提取而非盲截

代码位置: `ca/tool_summarizer.py` `class ToolSummarizer`
