# `_system_overhead` 测量范围分析报告

> 用途：为后续「基于摘要表组装 ctx」重构提供事实依据
> 日期：2026-06-14

---

## 一、问题陈述

`set_system_overhead()` 的意图是：首次 `post_llm_call` 时从 `conversation_history` 测量系统提示词的真实 token 数，覆盖默认值 20,000，使预算计算更准确。

实测却显示「不起作用」——测量值 9,522 比默认值 20,000 小，反而释放了预算。即使设 100K 也改变不了行为。

**根因：测量范围只覆盖了消息中 `role="system"` 的 `content` 字段，完全漏掉了非消息结构的系统开销。**

---

## 二、测量代码

```python
# plugins/__init__.py:324-332
if conversation_history:
    sys_tokens = sum(
        len(m.get("content", "")) // 4
        for m in conversation_history if m.get("role") == "system"
    )
    if sys_tokens > 0 and sys_tokens != self._engine._system_overhead:
        self._engine.set_system_overhead(sys_tokens)
```

**测量范围** = 遍历 `conversation_history`，筛选 `role == "system"` 的消息，取 `len(content) // 4` 求和。

---

## 三、缺失清单

`conversation_history` 是 Hermes 发送给 LLM 的**消息列表**。但 LLM API 请求体中还有**非消息参数**，它们也消耗 token 但完全不在测量范围内：

### 3.1 Tool schemas — 最大缺失项

Hermes 的 API 调用格式：

```python
client.chat.completions.create(
    model=self.model,
    messages=conversation_history,  # ← 消息列表
    tools=get_tool_definitions(),   # ← 工具定义，独立参数！
)
```

`tools` 是 API 请求的独立顶层参数，**不在 `messages` 里**。每个工具 schema 包含：

- 工具名（name）
- 描述（description）— 每个工具数十到数百字符
- 参数 schema（parameters.properties + required）— 递归 JSON schema

Hermes 默认加载的工具集（含 `_HERMES_CORE_TOOLS` + 各 toolset），典型大小：

```python
# 实测：~50 个工具
# 每个含 name + description + parameters（含递归 properties）
# 序列化后约 80KB-120KB ≈ 20K-30K tokens
```

**测量覆盖：❌ 0%。** `conversation_history["role"] == "system"` 只匹配消息体，tools 参数根本不进入 messages 结构。

### 3.2 Response format / structured output

```python
completions.create(
    response_format={"type": "json_object"},  # ← 独立参数
    ...
)
```

如果模型使用 `response_format` 或 `guided_json` 约束输出格式，这部分 token 消耗也不在 messages 中。

**测量覆盖：❌ 0%。**

### 3.3 Tokenizer 层面的非消息开销

每个消息在 LLM 的 tokenizer 中都有结构开销：

- `{"role": "system", "content": "..."}` → 协议框架 token（role 标签、content 键、引号、花括号）
- 每个消息的 `<|im_start|>` / `<|im_end|>` 分隔 token
- BOS（Begin of Sequence）token
- 消息角色之间的隐式分隔符

这些在 `len(content) // 4` 中完全不体现。

**测量覆盖：❌ 0%。**

### 3.4 CA injection 自身开销

CA 注入的 `[~/N]` / `[~/N/M]` 摘要在 `pre_llm_call` 时追加到 `user_message`，而 `post_llm_call` 拿到的 `conversation_history` 已经包含了这批注入文本。`system_overhead` 测量在 `post_llm_call`，此时注入文本已在 messages 中——但不在 `role=="system"` 里，所以不计。

这是正确的（CA 注入不应计入 system overhead），但需要注意：**system_overhead 定义的是「不在 conversation_history 中的系统级开销」**。按这个定义，仅 3.1-3.3 属于 overhead。

### 3.5 汇总

| 缺失项 | 量级估计 | 在 conversation_history 中？ | 测量覆盖 |
|--------|---------|-----------------------------|---------|
| Tool schemas | ~20K-30K tokens | ❌ 独立 API 参数 | ❌ |
| Response format | ~几十 tokens | ❌ 独立 API 参数 | ❌ |
| Tokenizer 框架开销 | ~10-20% 膨胀 | ❌ `len(content)//4` 不体现 | ❌ |
| CA injection 自身 | ~0（不应计入） | 在 messages 但不在 system role | ✅ 应跳过 |
| **实测 system_overhead** | **~9,522** | — | **仅 content 字面量** |

**实际系统总开销可能是测量值的 3-10 倍。**

---

## 四、为什么「不起作用」

即使修复测量范围（加上 tool schemas + tokenizer 框架），`_system_overhead` 的约束效果仍然有限：

### 4.1 约束对象不对

```python
# ca/__init__.py:712-713  — _available_budget()
used = system_tokens + head_tokens + tail_tokens + self._system_overhead
return max(0, int(context_length * 0.95) - used)
```

`_system_overhead` 只影响**检索升级的剩余预算**。它不影响：

- Middle 区 L0 摘要的生成（永远发生）
- Tail 区的原文保护（永远发生）
- `_hard_truncation()` 的触发条件（仅看 `tokens_before > context_length × 0.95`，完全不看 overhead）

即使设 `_system_overhead = 100000`，也只是让 `budget = 0` → 跳过检索升级。Middle L0 照出不误。

### 4.2 测量时机滞后

```python
# plugins/__init__.py:324 — 首次 post_llm_call
# plugins/__init__.py:248-280 — 但第一次 pre_llm_call 已提前跑完
```

`set_system_overhead()` 在 T1 的 `post_llm_call` 中设置。但 T1 的 `pre_llm_call`（用于注入上下文时）已经带着默认 20000 跑完了。所以第一次对话的预算计算用的是默认值——这本身不影响正确性，但意味着「系统提示词的实际大小」从未在第一个 assemble 周期中被考虑。

### 4.3 动态测量反而释放预算

```python
# ca/__init__.py:205
self._system_overhead: int = 20000  # 默认值
# plugins/__init__.py 测量值：9522
# 调用后 → _system_overhead = 9522 < 20000
```

默认 20K 比测量值 9.5K 大。动态测量在首次 `post_llm_call` 后将 `_system_overhead` **从 20K 降到了 9.5K**。预算反而变大了。

即使用 100K：

```
default 20K: 71250 - (8000 + 2000 + 2500 + 20000) = ~39K 正预算 ✅
    设 100K: 71250 - (8000 + 2000 + 2500 + 100000) = max(0, -41250) = 0 ❌
```

设到 100K 确实会让 budget=0 跳过检索。但跳过检索不影响 Middle L0——它只是「不升级已有摘要」，不是「省 token」。

---

## 五、对重构的建议

### 5.1 需要区分两个概念

| 概念 | 当前实现 | 重构方向 |
|------|---------|---------|
| **检索升级预算** | `_available_budget()` 约束 L0→L1 升级数 | 保留，但可把名字改成 `_upgrade_budget` 避免混淆 |
| **总输出约束** | 不存在。Middle L0 永远生成 | `_hard_truncation()` 是唯一截断点，但只在大幅超限时触发 |

### 5.2 如果要在总输出中加入 tool schemas 开销

当前架构中，`_available_budget()` 和 `_hard_truncation()` 都不知道 tool schemas 的存在。Hermes 发送 API 请求时加的 `tools` 参数不在 CA 可触及的任何数据结构中。

可能的切入方式：

```
方式 A：在 post_llm_call 中，从 Hermes 获取 tool_definitions 大小
  → 需要 Hermes hook 暴露 tool_definitions 的 token 数
  → 或 CA 自己同步调 get_tool_definitions() 估算

方式 B：在 pre_llm_call 返回的注入文本中嵌入 tool schema 开销标记
  → 不需要改 Hermes，但注入本身也会消耗 token

方式 C：放弃精确测量，用一个保守的大常数（如 MAX_SYSTEM_OVERHEAD）
  → 简单，但和「动态准确」的初衷矛盾
```

### 5.3 当前已知受影响的代码位置

| 文件 | 行 | 内容 |
|------|----|------|
| `ca/__init__.py` | 205 | `_system_overhead` 默认值 20000（仅保留作保守缓冲区） |
| `ca/__init__.py` | 712-713 | `_available_budget()` 消费点 |
| `ca/__init__.py` | 715-718 | `set_system_overhead()` **已弃用**，方法保留签名以防外部调用 |
| ~~`plugins/__init__.py`~~ | ~~324-332~~ | ~~测量代码~~ **已移除**（2026-06-14）|
| `ca/__init__.py` | 630-633 | budget=0 时跳过检索升级 |
| `ca/__init__.py` | 977 | `_hard_truncation()` — 唯一的总输出截断 |
| `ca/config.py` | 117 | `if False:` 屏蔽 Hermes 运行时查表 |
| `ca/config.py` | 134-136 | 未知模型走 `CONTEXT_LENGTH=150000` |
