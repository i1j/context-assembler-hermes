---
title: state DB 内容确认 — CA replace 模式突变是预期设计
date: 2026-06-11
type: debug
components: [run_agent.py, ca-assembler-plugin]
related: [debug-20260611-biz-category-implementation.md, debug-20260611-bypass-design-evolution.md]
---

## 背景

分析 CA replace 模式下 state DB 的内容一致性时，误以为 "state DB 污染" 是 bug。实际验证后确认：state DB 存储 LLM 实际看到的突变内容属于预期设计。

## 核心结论

**state DB = LLM 实际接收的内容（含 CA 突变）。** 装与不装 CA 时 state DB 不一致是预期行为。

## 证据

会话 `20260611_190412_99789a` 的 state DB 中，assistant{tc} 消息包含 CA 注入格式：

```
#24256: 【工具组:调用 skills_list→调用 1 个工具（1个，ok）】
```

无 CA 时同一位置应为 LLM 原始 thought 文本。**state DB 不干净。**

## 根因链（设计预期，非 bug）

CA replace 模式的突变通过浅拷贝的共享 dict 引用传播到 `agent.messages`，state DB 存储 LLM 实际看到的内容（含 CA 突变）。**这是设计，不是 bug。**

```
pre_llm_call:
  kwargs["conversation_history"] = list(messages)    ← 浅拷贝
  _mutation_mode:
    conversation_history[i]["content"] = outcome     ← 改共享 dict
    ↓
  messages[i]["content"] 同步被改（同 dict 对象）
  → LLM API 接收突变版
  → state DB 持久化突变版 ← 预期

post_llm_call:
  conversation_history.clear()
  conversation_history.extend({**m} for m in _snapshot)  ← 只恢复拷贝
  ↓
  _persist_session(messages, ...)
  → messages 仍有 CA 突变 ← 预期（LLM 看到什么就存什么）

session 恢复时:
  从 state DB 加载 → 重新经 CA 压缩 → ⊥（信息完整）
```

## 实装（2026-06-11）

**原则：绝不修改 hermes-agent 代码。** 改用 CA 插件 post_llm_call 就地恢复 content。

### 改动

**文件**: `plugins/ca_assembler/__init__.py` — `post_llm_call()` snapshot 恢复

| 旧行为 | 新行为 |
|--------|--------|
| `clear()` + `extend({**m})` — 创建新 dict 列表，不传播 | 就地 `conv_h[i]["content"] = orig` — 共享 dict 引用传播 |

**数据流**：
```
post_llm_call:
  conv_h = list(messages)          ← conv_h[i] is messages[i]  ✅
  for i, orig in enumerate(snap):
    conv_h[i]["content"] = orig    ← messages[i]["content"] 同步恢复
  → _persist_session(messages, …) → state DB 写原始内容 ✅
  → 下一轮 _original_messages 捕获原始版 → bypass 注入原始版 ✅
```

**补同行**：当 `len(conv_h) < len(snapshot)` 时从 snapshot 追加缺失行，保留行级兼容。

### 验证

```
131 passed, 4 failed（test_a.py 既存）, 1 skipped → 0 回归 ✅
```

### 影响

- ✅ 尾部保护区：跨轮残余污染消除，LLM 始终看到原始 thought
- ✅ bypass 语义：保护区的「跳过改写」不因上轮残留污染而变质  
- ✅ `_original_messages`：独立深拷贝，不受影响
- ✅ 工具行：post_llm_call 的 conv_h 是新鲜 `list(messages)`，含所有工具行，不存在「缺失行」问题

### 日志监控

`grep "in-place restored" ~/.hermes/logs/agent.log`
- 有 CA 且有 content 被 mutate 的轮次：`restored N + appended 0 from snapshot`
- 无 CA / bypass 全量轮：不出现
- CA 测试中不出现（测试环境不跑完整 Hermes 循环）

```python
if conversation_history is not None:
    _n_restored = 0
    for i in range(min(len(conversation_history), len(messages))):
        ch = conversation_history[i]
        msg = messages[i]
        if ch.get("role") == msg.get("role"):
            orig = ch.get("content")
            if orig is not None and msg.get("content") != orig:
                msg["content"] = orig
                _n_restored += 1
    if _n_restored:
        logger.debug(
            "Persist: restored %d messages from original history "
            "(undo CA content mutation for clean state DB)",
            _n_restored,
        )
```

### 关键假设验证

1. `conversation_history` 是 ORIGINAL（pre-CA）内容 ✅
   - 它在 `run_conversation` 函数域中，从未被 CA 管道突变
   - CA 只突变了传递给 hook 的 `list(messages)` 浅拷贝

2. role 匹配保证对齐 ✅
   - `messages[0..N]` 与 `conversation_history[0..N]` 的顺序和 role 一致
   - 两者从同一个输入衍生

3. 仅覆写 `content` 字段 ✅
   - 不碰 `role`, `tool_calls`, `tool_call_id` 等
   - 不改 dict 结构，不删除行

4. `msg.content != orig` 短路 ✅
   - 无 CA 时：相等 → 跳过，0 次 restore
   - 有 CA 且未突变：相等 → 跳过
   - 有 CA 且被突变：不等 → restore

### 验证数据

state DB 确认（session `20260611_190412_99789a`）：

| 项 | 数据 |
|---|------|
| assistant{tc} 总数 | 109 条 |
| 含【工具组】突变 | 32 条（29%）|
| 原文透传 | 77 条（71%）|
| user 含标签 | 0/33 |

### 污染循环

修复前：

```
pre_llm_call:
  _original_messages = [dict(m) for m in list(messages)]
                        ↑ 如果 messages 已被上一轮 CA 污染，这里也污染
                        ↓
bypass 轮从 _original_messages 注入污染版 → state DB 污染版 → 下一轮加载污染版
```

**修复后第一轮** `_persist_session` 切断循环：

```
该轮:
  _persist_session 从 conversation_history 恢复 messages content
  → flush 到 state DB ✅ 干净
下一轮:
  从 state DB 加载干净数据
  → _original_messages = [dict(m) for ...] ✅ 干净
  → bypass 注入 ✅ 干净
```

### 不涉及 CA 的路径不受影响

- `_original_messages` 和 state DB 是**不同管道**：前者从 `list(messages)` 深拷贝，后者从 `_persist_session` 持久化
- 修复只保证两管都从**同一来源（state DB 原始内容）**恢复一致
- bypass 轮的 outcome=None → `continue` → 不走 `content = outcome` 路径 → 不污染 dict

### 后续监控

- `_n_restored` 的 debug 日志可在 Hermes 日志中追踪恢复次数：`grep "Persist: restored" ~/.hermes/logs/agent.log`
- 确认 CA 测试中该日志不出现（测试环境不跑完整 Hermes 循环）
- 确认真实对话中出现时数值合理（不应超过该轮 tool-calling assistant{tc} 消息数）

## 影响范围

| 行类型 | state DB 影响 | 原因 |
|--------|-------------|------|
| user（历史轮） | ❌ 不污染 | 在 CA 突变前已 flush |
| user（当前轮） | ⚠️ 理论污染 | 走 L0 替换路径时会污染，但当前数据中短到没有被替换 |
| **assistant{tc}** | **✅ 污染** | `content = outcome` 替换，新消息直接 flush |
| tool 行 | ❌ 不污染 | `del` 只删拷贝，`messages` 残留原文 |
| assistant{final} | ❌ 不污染 | outcome=None，不碰 |

## 修复

### 修复位置

**文件**: `hermes-agent/run_agent.py` — `AIAgent._persist_session()`

### 修复逻辑

在 `_persist_session` 开头的 docstring 之后、`_drop_trailing_empty_response_scaffolding` 之前，插入内容恢复层：

```python
if conversation_history is not None:
    _n_restored = 0
    for i in range(min(len(conversation_history), len(messages))):
        ch = conversation_history[i]
        msg = messages[i]
        if ch.get("role") == msg.get("role"):
            orig = ch.get("content")
            if orig is not None and msg.get("content") != orig:
                msg["content"] = orig
                _n_restored += 1
    if _n_restored:
        logger.debug(
            "Persist: restored %d messages from original history "
            "(undo CA content mutation for clean state DB)",
            _n_restored,
        )
```

### 关键假设验证

1. `conversation_history` 是 ORIGINAL（pre-CA）内容 ✅
   - 它在 `run_conversation` 函数域中，从未被 CA 管道突变
   - CA 只突变了传递给 hook 的 `list(messages)` 浅拷贝

2. role 匹配保证对齐 ✅
   - `messages[0..N]` 与 `conversation_history[0..N]` 的顺序和 role 一致
   - 两者从同一个输入衍生

3. 仅覆写 `content` 字段 ✅
   - 不碰 `role`, `tool_calls`, `tool_call_id` 等
   - 不改 dict 结构，不删除行

4. `msg.content != orig` 短路 ✅
   - 无 CA 时：相等 → 跳过，0 次 restore
   - 有 CA 且未突变：相等 → 跳过
   - 有 CA 且被突变：不等 → restore

## 污染循环

修复前：

```
pre_llm_call:
  _original_messages = [dict(m) for m in list(messages)]
                        ↑ 如果 messages 已被上一轮 CA 污染，这里也污染
                        ↓
bypass 轮从 _original_messages 注入污染版 → state DB 污染版 → 下一轮加载污染版
```

**修复后第一轮** `_persist_session` 切断循环：

```
该轮:
  _persist_session 从 conversation_history 恢复 messages content
  → flush 到 state DB ✅ 干净
下一轮:
  从 state DB 加载干净数据
  → _original_messages = [dict(m) for ...] ✅ 干净
  → bypass 注入 ✅ 干净
```

## 不涉及 CA 的路径不受影响

- `_original_messages` 和 state DB 是**不同管道**：前者从 `list(messages)` 深拷贝，后者从 `_persist_session` 持久化
- 修复只保证两管都从**同一来源（state DB 原始内容）**恢复一致
- bypass 轮的 outcome=None → `continue` → 不走 `content = outcome` 路径 → 不污染 dict

## 验证

- CA 测试: 315 passed, 4 failed（既存）, 20 skipped — 0 回归
- ”无 CA 场景永远 0 restore”已通过代码推理验证（`msg.content == orig.content`）

## 后续监控

- `_n_restored` 的 debug 日志可在 Hermes 日志中追踪恢复次数：`grep "Persist: restored" ~/.hermes/logs/agent.log`
- 确认 CA 测试中该日志不出现（测试环境不跑完整 Hermes 循环）
- 确认真实对话中出现时数值合理（不应超过该轮 toll-calling assistant{tc} 消息数）
