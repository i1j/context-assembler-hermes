# 调试记录：Fct 精简、尾部保护区 3→2、bg_review Fct 固定标签

**日期：** 2026-06-15
**Session：** `mqf2n0hkqqsgha`
**改动范围：** `tool_summarizer.py`、`a_planner.py`、`ca/__init__.py`

---

## 1. 三源数据分析结论

当前 session 9 轮，145 条消息。CA 插件运行正常：

- 0 crash / 0 TypeError（`@staticmethod` bug 已修复 ✅）
- turn_stream 56/57 tool 行有 Fct（98% coverage）
- A-stage `simple_mutation` 正确替换 40/104 条消息
- 尾部 2 轮保护正确生效

---

## 2. Fct 格式精简

### 发现问题

`tool_summarizer.py` 所有 `_summarize_*` 方法的 l1 dict 包含空字段：

```python
l1 = {
    "tool_name": "terminal",
    "tool_args": args,
    "result_summary": result_summary,
    "error": None,                # 98% 情况为 null
    "implicit_knowledge": [],     # 永远为空
    "next_action_hint": "",       # 永远为空
    "_assemble_status": 0,        # 已被 ToolPlan.filter() 裁剪
}
```

`ToolPlan.filter()` 在 A-stage 注入时已自动裁剪这些字段，但 DB 中仍存着它们，浪费存储。

### 短输出冗余

terminal 工具短输出（<500ch）时，`result_summary` 格式为 `"(N lines) [command]"`，与 `tool_args.command` 几乎重复：

```json
// 原文 content (45ch):  {"output": "", "exit_code": 0, "error": null}
// Fct l1_text (341ch):  {"tool_name":"terminal","tool_args":{"command":"...","timeout":10},"result_summary":"(1 lines) [...]","error":null,...}
```

`result_summary` 的 `(N lines) [command]` 格式在无有效 key_lines（路径 B）时完全冗余。

### 改动

**`tool_summarizer.py`** 全部 12 个 `_summarize_*` 方法：

| 字段 | 操作 | 原因 |
|------|------|------|
| `implicit_knowledge: []` | 删除 | 永远为空，ToolPlan 已裁剪 |
| `next_action_hint: ""` | 删除 | 永远为空，ToolPlan 已裁剪 |
| `_assemble_status: 0` | 删除 | ToolPlan 白名单不含此字段，DB 死重 |
| `error: None` | 条件写入 | 有值才写，ToolPlan 同理 |
| `result_summary` (terminal) | 有 `key_lines` 才写 | 无有效行时与 `tool_args.command` 相同 |

**效果：** 每个工具行 Fct 减少约 80-120ch（DB 层），注入的短输出行减少 50-200ch。

**终端短输出示例（改动前后）：**

```json
// 改前 (341ch)
{"tool_name":"terminal","tool_args":{"command":"# check","timeout":10},"result_summary":"(1 lines) [# check]","error":null,"implicit_knowledge":[],"next_action_hint":"","_assemble_status":0}

// 改后 (~140ch)
{"tool_name":"terminal","tool_args":{"command":"# check","timeout":10}}
```

---

## 3. 尾部保护区 3→2

### 实测数据

当前 session 的 9 轮中，保护区外（Fct）vs 保护区内（Elm）对比：

| 区间 | 轮次 | 原始大小 | Fct 后大小 | 节省 |
|------|------|---------|-----------|------|
| 保护区外 (Fct) | turn 1-2 | 178,627ch | 68,343ch | 62%↓ |
| 尾区边界 | turn 5 (tail=2 时变 Fct) | 3,383ch | 674ch | 80%↓ |
| 保护区 (Elm) | turn 7 | 86,410ch | (原文) | 0% |

改为 tail=2 后 turn 7 可省 **25,380ch（~6K tokens）**。

### 语义损失评估

保护区只影响 **tool 行** 和 **assistant{tc} 摘要化**，user 行和 assistant_fin 永远原文保留。第 3 轮的问题和回答不会被摘要掉。

---

## 4. bg_review Fct 固定标签

### 问题

原 `_run_f_stage` bg_review 路径截取 `user_elm[:80]`。bg_review 的 user message 是固定 prompt（"Review the conversation above and consider saving to memory..."），前 80 字符全一样，无区分度：

```python
# 改前：前 80 字符截取
_brief = user_elm[:80]  # "Review the conversation above and consider saving to mem" ← 无信息
```

### 改动

```python
# 改后：固定标签
if "skill" in user_elm.lower():
    _brief = "Skill review"
elif "memory" in user_elm.lower():
    _brief = "Memory review"
else:
    _brief = "Background review"
```

### 消费链路确认

| 消费方 | 是否消费 bg Fct | 影响 |
|--------|---------------|------|
| 主会话 A-stage | ❌ 跳过 | 无 |
| 主会话 Topic 分割 | 仅 `_is_bg_turn` 检测 | 4 材料字段缺省 → 正确返回 True |
| 主会话 Topic 定级 | 仅 `is_bg` 标记 | 标记正确即可 |
| 主会话 检索 | ❌ 排除 (retrieval.py:221) | 无 |
| bg_review 会话自 A-stage | ❌ 跳过 | 工具行全量原始（设计意图：大模型需要完整数据决策） |
| 调试/日志 | 固定标签 | 一眼区分 memory/skill 类型 |

---

## 5. 代码变更汇总

| 文件 | 行 | 改动 |
|------|-----|------|
| `tool_summarizer.py` | 12 处 l1 dict | 删空字段、条件 error、短输出省略 result_summary |
| `a_planner.py` | 26 | `TAIL_PROTECT_TURNS = 3 → 2` |
| `ca/__init__.py` | 314-326 | bg_review Fct 80 截断 → 固定标签 |
| `AGENTS.md` | 98, 500 | 文档同步 |
| `tests/test_astage.py` | 66 | 注释同步 |

全部测试：**269 passed, 19 skipped, 0 failed。**
