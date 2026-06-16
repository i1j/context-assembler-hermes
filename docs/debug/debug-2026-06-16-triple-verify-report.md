# 三源验证报告：上一轮改进效果

**日期：** 2026-06-16
**会话：** mqfhfc2vm0be4e
**代码状态：** 上轮改进已部署（未提交，6/15 修改，6/16 01:25 进程加载）

---

## 验证结论

| # | 改进 | 状态 | 证据 |
|---|------|------|------|
| 1 | Fct 精简化：删 `implicit_knowledge/next_action_hint/_assemble_status` | ✅ | tool Fct 只有 `tool_name, tool_args, result_summary` |
| 2 | 尾部保护区 3→2 | ✅ | plan 输出 `[1-7:fct, 8:elm, 9:elm]` |
| 3 | A-stage planner+injector 重构 | ✅ | `simple_mutation: replaced 106` 正常跑 |
| 4 | bg_review Fct 固定标签 | ⚠️ 无数据 | 本会话无 bg_review |

## 遗留问题：工具行未吸收

turn_stream 中 110 条工具行全部保留原文，LLM 每次调用都看到完整 JSON。这是本会话 139K+ token 的主要来源。

非尾区（turn 1-7）的 tool 行本应吸收为 `" "`，但 dump 显示：吸收=0，保留=110。

工具行吸收功能未实装，需要另开话题调查。
