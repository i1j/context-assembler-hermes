---
title: 按工具类型 Fct/Hdl 设计
slug: design-fct-hdl-per-tool
category: decision
date: 2026-06-18
alternatives:
  - "统一元数据摘要：所有工具 Fct 只存字段名+计数"
  - "全量 Elm 复制：Fct = 原始 tool 响应原文"
  - "逐工具结构化提取（选定）：按响应结构做优先级分配 + 章节索引"
chosen: "逐工具结构化提取"
affects: [tool-summarizer]
updated: 2026-06-18
---

# 按工具类型 Fct/Hdl 设计

## 问题

每个 tool call 被汇编成两层级摘要：
- **Fct** 在 A-stage 注入，供 LLM 阅读
- **Hdl** 在话题回顾时供 LLM 判断是否展开

对每种工具，Fct 保留什么、Hdl 提供什么？

## 备选方案

| 方案 | Fct 内容 | 优缺点 |
|------|---------|--------|
| **A: 统一元数据摘要** | 所有工具只存字段名+计数 | ✅ 大小可控 ❌ 丢失互信息 |
| **B: 全量 Elm 复制** | Fct = 原始响应原文 | ✅ 信息完整 ❌ 不可控膨胀 |
| **C: 逐工具结构化提取** (选定) | 按响应结构分析，优先级分配 + 保留关键内容 + 索引 | ✅ 信息与大小平衡 ✅ 适配每类工具 |

## 各工具实现策略

### read_file — Fct = Elm 全量
- Fct 保留文件原文 + total_lines
- 切除冗余：path/offset/limit 已在 tool_args
- Hdl: `路径(范围, N行)` 或 `路径 — 错误前80字`

### skill_view — 结构化 Markdown 提取
- 解析 frontmatter（描述/标签/引用文件）
- 章节识别：## / ### 切割
- 优先级：踩坑/警告 > 代码块 > 使用/步骤 > 其余
- 高优先级全文保留，其余章节索引
- Hdl: `名称 — 描述[:60] (N行)`

### terminal — 命令前缀 + 关键输出
- pytest 检测：测试结果结构化
- 通用：去重前5行关键输出 + 命令简写
- Hdl: `exit=N: cmd[:40]: 输出首行[:50]`
- **旧问题**：Hdl 只有输出首行，丢了命令线索

### todo — 全量列表
- Fct = [{content, status}, ...] — 全量 tasks
- 大小通常 < 2KB，互信息价值高
- **旧问题**：Fct 只存计数，A-stage 看不见具体任务

### 通用 fallback — p1 提值
- **旧问题**：p1 以 <field> 占位符存储，无实际值
- **新方案**：p1 存实际值（full=True, truncate=True），p0 无结果时 fallback

## 统一产出格式

```python
(l1_dict, l0_str) = handler(tool_call_msg, tool_responses)
```
- fct: Fct JSON dict → fct_text
- hdl: Hdl 纯文本，_safe_truncate(text, 100) ≤100字

## 优点

- Fct 保留的互信息足够 LLM 多数场景无需回查 Elm
- Hdl 让 LLM 快速判断"这轮操作是否相关"
- 结构化优先原则可扩展到新增工具

## 约束

- Hdl 100 字在长路径+长命令+长输出场景仍会截断
- skill_view 的结构化解析依赖章节标题关键词匹配
- 通用 fallback 的 p1 值可能含低信息量结构，_sanitize_summary_text 进一步清理
