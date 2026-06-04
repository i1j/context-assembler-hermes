# CA Plugin 测试报告：A-stage 触发时机偏差与 compress() 参数类型错误

报告编号: t_b022f3a9
报告人: python-api-dev (T&D)
报告日期: 2026-05-23
版本: v1

---

## 发现 1（严重）：A-stage 触发时机与设计文档不符

### 设计文档要求

| 文档 | 章节 | 原文 |
|------|------|------|
| TECHNICAL_PLAN.md | §1.3 术语表 | `A-stage \| Assembly 阶段，pre_llm_call 钩子中执行` |
| TECHNICAL_PLAN.md | §2.1 架构图 | A-stage 入口 = `pre_llm_call` |
| DEVELOPMENT_REPORT.md | 架构 | `A-stage (Assemble, pre_llm_call) ← 用户提问前` |

三份设计文档一致：**A-stage（汇编）应在每轮 `pre_llm_call` 钩子中执行**，即用户提问前每轮都跑一次。

### 实现现状

A-stage 没有挂钩到 `pre_llm_call`，而是放在了 Hermes ContextEngine 协议的 `compress()` 方法中。

```python
# ~/.hermes/hermes-agent/plugins/context_engine/ca_assembler/__init__.py
def compress(self, messages, current_tokens=None, ...):
    result = self._engine.assemble(
        user_input=current_tokens or "",
        messages=messages,
        context_length=self.context_length or 200_000,
    )
```

### 影响

`compress()` 的调用时机由 Hermes 主循环控制 — 仅在 `prompt_tokens >= threshold_tokens` 时触发。即：

- 对话未超阈值 → `should_compress()` 返回 False 或未被调用 → **不发生汇编**
- 对话超阈值 → `compress()` 被调用 → 汇编发生（但见发现 2）

**结论：当前实现下，CA 的 A-stage 汇编在生产路径上只在压缩阈值被触发后才执行，并非每轮执行。设计文档要求的 pre_llm_call 路径未实现。**

---

## 发现 2（中-严重）：compress() → assemble() 参数类型错误

### 问题描述

`compress()` 收到 Hermes 传来的 `current_tokens`（对话近似 token 数，int），直接传给 `assemble()` 的 `user_input` 参数：

```python
# ~/.hermes/hermes-agent/plugins/context_engine/ca_assembler/__init__.py:372-376
result = self._engine.assemble(
    user_input=current_tokens or "",  # current_tokens 是 int，比如 85000
    messages=messages,
    context_length=self.context_length or 200_000,
)
```

`assemble(user_input: str, ...)` 期望 `user_input` 是字符串（用户当前输入文本），用于 BM25 检索和向量编码。但在生产调用中收到的是 int。

### 调用链崩溃

```
compress(messages, current_tokens=85000)
  → assemble(user_input=85000, ...)
    → retriever.retrieve(user_input=85000, ...)
      → tokenise(user_input).lower()  # int 没有 .lower() → AttributeError
```

### 影响

崩溃被 `compress()` 内的 `try/except Exception` 捕获，回退到 `_hard_truncation()`。这意味着：

- 即使 `compress()` 被触发调用，也**不会真正执行 CA 组装**
- 每次压缩都静默崩溃并降级为硬截断
- 日志中记录 `ca_assembler.assemble() failed: ...`，但不会阻断 Hermes 运行

### 验证方法

单元测试 `test_online.py` 中已验证：mock `current_tokens=int` 时，`compress()` 回退到硬截断路径。

---

## 发现 3（需求变更）：组装 ctx 上限取值

### 当前行为
`compress()` 传给 `assemble(context_length=...)` 的是云端模型完整 max_ctx（`self.context_length`），如 200K。

CA 内核 `_available_budget()` （`ca/__init__.py:290-302`）内部硬编码了：

```python
return max(0, int(context_length * 0.95) - used)
```

### 需求变更要求
组装 ctx 上限应为 **threshold_tokens** = `ctx_max × compression.threshold`。

| 参数 | 来源 | 默认值 |
|------|------|--------|
| `ctx_max` | Hermes 传入的云端模型最大上下文 | 如 200K |
| `compression.threshold` | Hermes `config.yaml → compression.threshold` | 0.50 |
| **threshold_tokens** | `int(ctx_max × compression.threshold)` | 如 100K |

`context_length * 0.95` 仅作 fallback——当没有 threshold_tokens 传入时使用。

### 相关代码

**Plugin 侧**（`ca_assembler/__init__.py`）：
- L250-252：`self.threshold_tokens = int(ctx_len * threshold_percent)` — **已计算，未使用**
- L372-376：`compress()` 调用 `assemble(context_length=self.context_length, ...)` — **传的是完整 ctx，不是 threshold**

**CA 内核**（`ca/__init__.py`）：
- L290-302：`_available_budget()` 内 `int(context_length * 0.95)` — **应降级为 fallback**

---

## 严重性评估

| 编号 | 问题 | 严重度 | 影响范围 |
|------|------|--------|---------|
| #1 | A-stage 未挂到 pre_llm_call | 严重 | CA 汇编在大部分轮次不执行 |
| #2 | compress() int→str 类型错误 | 中 | 即使触发 compress() 也回退硬截断 |
| #3 | context_length 未传 threshold 值 | 建议 | 组装上限偏大，但不影响正确性 |

**综合结论：CA Plugin 在生产路径上实际未生效。** 问题 #1 导致大部分轮次不走汇编；问题 #2 导致即使走 compress() 也回退硬截断。两者叠加 = 任何轮次都不走真实 CA 组装。

---

## 建议修复方向

1. **#1（优先）**：将 A-stage 挂钩到 Hermes `pre_llm_call` 插件钩子，每轮在用户提问前执行 `engine.assemble()`
2. **#2（随 #1 修复）**：在 `pre_llm_call` 钩子中正确传入用户消息文本作为 `user_input`
3. **#3（可选）**：`_available_budget()` 接收外部传入的 ctx 上限，`* 0.95` 降为 fallback

---

## 复现步骤

### 环境
- CA Plugin: `plugins/context_engine/ca_assembler/`
- 测试文件: `tests/test_online.py`
- Hermes 集成: `agent/conversation_loop.py` + `agent/agent_init.py`

### 验证 #1：A-stage 是否在 pre_llm_call 中执行
1. 在 `conversation_loop.py` 的 `pre_llm_call` 钩子调用点（约 L550）加日志
2. 运行一个未超阈值的对话
3. 检查日志：CA 的 assemble 是否被调用

### 验证 #2：compress() 参数类型错误
```python
# 单元测试：传 int 给 compress() 是否会触发 fallback
engine.compress(messages, current_tokens=85000)
assert engine._engine_errored  # True
```
