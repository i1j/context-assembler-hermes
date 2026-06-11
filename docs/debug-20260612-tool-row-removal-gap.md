# 调试报告：CA Replace 模式未移除 Tool 行 — 压缩近乎无效

**日期**: 2026-06-12
**会话**: current (本调试分析过程)

---

## 现象

1. CLI 显示的 token 量约 241K（实际 state DB 总量 91,760 tok ≈ 367K chars），与 CA 声称的压缩量（~10.5K tok 节省）严重不符。
2. CLI 活动区域显示 `【工具组:调用 execute_code→调用 1 个工具（1个，ok）】` CA 格式文本。

## 根因

### 根因 1：`del conv_h[i]` 只删 `conv_h` 列表，不删 `messages`

CA 的 `_mutation_mode`（`plugins/ca_assembler/__init__.py:462-471`）：

```python
conv_h = list(messages)   # 新列表

for i in range(len(outcomes) - 1, -1, -1):
    outcome = outcomes[i]
    if outcome == "":
        del conversation_history[i]   # ← 只删 conv_h 列表
        skipped += 1                  #   messages 完全不受影响
    else:
        conversation_history[i]["content"] = outcome  # ← 共享 dict，messages 同步
```

`conv_h = list(messages)` 创建**新列表**。`del conv_h[i]` 只对新列表生效，`messages` 原样保留。

之后 `api_messages = [msg.copy() for msg in messages]` 将 tool 行原文原样送入 LLM。

### 压缩量对比

| 角色 | 数量 | 文本量 | 占比 |
|------|------|--------|------|
| tool | 94 条 | 340,360 chars = 85,090 tok | **92.7%** |
| assistant | 93 条 | 26,342 chars | 7.2% |
| user | 11 条 | 340 chars | 0.1% |

CA replace 模式只替换了 user（0.1%）和 assistant{tc}（7.2%）的 content，**tool 行（92.7%）原封不动**。

### 根因 2：`【工具组】` 格式出现在 CLI

`conv_h[i]["content"] = outcome` 通过共享 dict 引用（`conv_h[i] IS messages[i]`）将 CA 格式的 `【工具组:调用 execute_code→调用 1 个工具（1个，ok）】` 写入 `messages[i]["content"]`。

CLI 的 `tool_progress_callback` 读取 `messages` 或从 streaming 输出中提取，展示了这些 CA 格式文本。

## 修复方向

修复方案需在 CA 插件边界内，不修 hermes-agent 核心代码。

### 思路 A：在 `_mutation_mode` 中从 `messages` 移除 tool 行

不能。`messages` 的引用在调用方的作用域内，CA 无法获取。

### 思路 B：在 `_mutation_mode` 中替换 conv_h 的 dict 对象（不共享引用），同时拦截 api_messages 构建

不能。`api_messages = [msg.copy() for msg in messages]` 始终从 `messages` 读取，CA 无法拦截。

### 思路 C：在 `pre_llm_call` 中不 mutate shared dict，改为返回结构化文本（append 模式）

这是方案 A（已否决——用户要求保持 1:1 替换）。

### 思路 D：修改 `_build_aligned_outcomes` 对 tool 行生成 `None` 而非 `""`

tool 行的 outcome 是 `""`（表示删除）。如果将 tool 行的 `_build_aligned_outcomes` 输出改为 `None`（保留原文），content 不变但不会被 `del conv_h[i]` 影响。但这样 tool 行仍会保留原文内容。

这是一个格式问题而非内容问题——tool 行 content 本身就是原始输出，留着才是正确的。问题在于 `api_messages` 包含 tool 行原文。

### 思路 E：让 tool 行的 outcome 为字符串而非 `""`

outcome 为 `""` → `del conv_h[i]`
outcome 为字符串 → `conv_h[i]["content"] = 该字符串`

如果 tool 行的 outcome 不是 `""` 而是一个空的/轻量的占位符字符串（如 `""`），则 `conv_h[i]["content"] = ""` 会清空 tool 行内容（通过共享 dict），但行本身仍保留在 `messages` 中。

```python
# 需要改 _build_aligned_outcomes
# 当前：tool 行 → outcome = ""
# 改为：tool 行 → outcome = "（工具结果已移除）"  # lightweight placeholder
```

但这样 `api_messages` 中仍保留空的 tool 行——LLM 仍然要为这些空行消耗 attention slots。

### 思路 F（推荐）：从 `_build_aligned_outcomes` 移除 tool 行映射，改为在 `_mutation_mode` 中直接修改 `messages` 引用

实际上不可行（没法获取 `messages`）。

### 思路 G（唯一可行）：conv_h 的 del 改为直接在 messages 上操作

无法从 CA 插件内部直接修改 `messages`，因为 `conv_h = list(messages)` 创建的是新列表。

但可以通过以下方式间接实现：不修改 conv_h 也不修改 messages，而是在 `_format_tool_group_assembly` 生成的摘要中**包含工具结果摘要**，让 LLM 看到的 context 中工具行内容被**替换为摘要**而非原文。

对应的方案：将 `_build_aligned_outcomes` 中的 tool 行 outcome 从 `""` 改为 `"..."`（工具行摘要），通过共享 dict 引用替换 content。

## 当前状态

- tool 行移除操作毫无效果——`del conv_h[i]` 只影响新列表，`messages` 原样
- CA 声称的压缩（~10.5K tok 节省）仅覆盖 user + assistant 的 7.3% 文本量
- 92.7% 的 tool 输出原文送入 LLM
- 总 token 量 241K 是原样的，压缩几乎无效

## 修复实施（2026-06-12）

### 方案 A：tool 行 content 清空

**改动**：`__init__.py:467` `del conversation_history[i]` → `conversation_history[i]["content"] = " "`

**原理**：通过共享 dict 引用（`conversation_history[i] IS messages[i]`）将 tool 行 content 替换为单空格占位符，规避 DeepSeek API 对空 content 的潜在风险。行结构（role + tool_call_id）保留。

**效果估算**：94 条 tool 行，340K chars → 94 chars（单空格），~85K tok → ~1.9K tok overhead。压缩比 ~3540×。

**恢复机制**：post_llm_call 从 `_saved_history_snapshot` 通过 `ch["content"] = orig_content`（共享 dict）恢复原文 → state.db 存原始数据。

**测试验证**：`pytest tests/test_plugin.py tests/test_aligned_outcomes.py -v` → 60 passed, 0 failed。snapshot restore、aligned outcomes 均正常。

### 遗留约束

- tool 行无法从 `messages` 列表中物理删除（CA 插件操作的是 `list(messages)` 浅拷贝，`del` 不传播）
- 每行 ~20 tok 的 JSON 结构 overhead（`{"role":"tool","tool_call_id":"call_xxx","content":" "}`）仍然保留，无法在 CA 插件边界内消除
- state.db 完整保留原始数据，不受影响
- 后台信息轮（turn_cache）独立于 messages，不受影响
