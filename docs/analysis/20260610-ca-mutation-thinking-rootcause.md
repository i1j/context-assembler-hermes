# CA Mutation 思维问题根因分析

**会话**: 20260610_094540_d13093 (对话轮嵌入覆盖率澄清)
**日期**: 2026-06-10
**类型**: 事后根因分析
**背景**: 助理在对话中将用户的项目文档错误地放入 OV memories/，经用户多次纠正后仍将纠正意图误记为"已实施"，最终忽略用户明确的迁移计划。

---

## 结论总览

思维问题的根因是 **CA 在大规模会话（286K tok）中被强制降级**，结合 tail→turn 映射 bug 和用户快速插入对话（T38→T41 连续 4 轮）的三重叠加失效。助理并非"思维不好"，而是基于被篡改的对话历史在决策。

---

## 术语说明

- **压缩**: 特指 Hermes 自身的强制压缩机制（preflight compression, `_compress_context`）
- **汇编**: CA 对对话历史的组装和注入操作（`assemble()`、`_build_aligned_outcomes()`、topic grading、L0/L1/L2 标记）
- **降级**: 对话轮被分配到 L0/L1 而非 L2（topic_grading 结果）

---

## 失效层 ①：总会话规模 >> CA 上下文限制

| 位置 | 累计 token | CA_CONTEXT_LIMIT |
|---|---|---|
| T38（用户首次纠正） | **286,109 tok** | 200,000 |
| T39（"不应该放memories"） | 286,812 tok | 200,000 |
| T40（"不是要你存这个目录"） | 300,302 tok | 200,000 |
| T41（"怎么老盯着user"） | 318,820 tok | 200,000 |
| 全量 | 368,468 tok | 200,000 |

到 T38 时已超出 CA 上下文限制 86K token。CA 的 10K tail 保护区在这种规模下只能保最后 2-3 轮，其余全部被 topic grading 降级。

**结果**: 大部分轮被降级到 L0/L1，包括包含关键纠正的 T38-T40。

---

## 失效层 ②：tail→turn 映射 bug

### 预期行为

`_compute_tail_start()`（`ca/__init__.py:1949`）从尾部反向计算非 tool 消息的累计 token：

```python
def _compute_tail_start(self, messages):
    tail_tokens = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "tool":
            continue
        tail_tokens += self._token_estimate(messages[i].get("content", ""))
        if tail_tokens >= Config.PROTECT_TAIL_TOKENS:
            return i
    return 0
```

实际 PROTECT_TAIL_TOKENS=10000（默认，config.yaml 的 `protect_tail_tokens: 20000` 因 YAML→env 映射断链而未生效）。

非 tool 内容尾部反向累计结果：

```
T48 → T38:  6,374 tok  ← 应进 tail
T48 → T33:  9,953 tok  ← 应进 tail（< 10000）
T48 → T32: 10,152 tok  ← 边界
```

T38-T41 累计仅 6,374 tok，**远低于 10K tail 预算**。

### 实际行为

turn_plan 显示 T38-T41 为 L1(topic_baseline)，T42 起才是 L2(tail)。T41 在 turn_plan 中完全不存在。

### 问题代码

```python
# _compute_turn_plan_v2, lines 1422-1425 (已修复)
in_tail = any(i >= tail_start for i, mi in enumerate(messages)
             if mi.get("_turn_index", i) == turn
             and mi.get("role") not in ("system", "tool")
             and "tool_calls" not in mi)
```

根因：`_compute_tail_start` 与 `_compute_turn_plan_v2` 使用了不同的消息过滤逻辑。`_compute_tail_start` 跳过 `role="tool"` 但**计入**其他所有消息（含无 `_turn_index` 的）。而 `in_tail` 的 `any()` 只匹配明确设置了 `_turn_index` 的消息。无 `_turn_index` 的消息消耗了 tail 预算，导致 `tail_start` 的消息索引值偏高，映射到轮索引后 T33-T41 被排除在 tail 区外。

### 修复状态（2026-06-10 已实施）

将原 `any(i >= tail_start ...)` 替换为基于 `_turn_index` 的轮索引计算：

```python
_tail_start_turn: int = 0
for _i in range(tail_start, len(messages)):
    _turn_idx = messages[_i].get("_turn_index")
    if _turn_idx is not None and isinstance(_turn_idx, int) and _turn_idx > 0:
        if _tail_start_turn == 0 or _turn_idx < _tail_start_turn:
            _tail_start_turn = _turn_idx

in_tail = (_tail_start_turn > 0 and turn >= _tail_start_turn)
```

**不扩大 tail 预算**（保持 `Config.PROTECT_TAIL_TOKENS` 默认值 10000），仅修正同一预算下分配的正确性。`CA_PROTECT_TAIL_TOKENS` 不作调整、不追加到 `.env`。

注意：`config.yaml` 的 `protect_tail_tokens: 20000` 仍是孤魂设置（CA 仅读 env var），暂不修复以避免干涉默认平衡点。

---

## 失效层 ③：快速用户插入（T38→T41 连续 4 轮）

| 轮 | 用户消息 | 工具输出量 | 状态 |
|---|---|---|---|
| T38 | "是不是拆成wiki页面" | 1,366 chars | 正常 |
| T39 | "不应该放memories里" | 2,813 chars | 新工具调用触发 |
| T40 | "不是要你存这个目录！！！" | **53,958 chars** | 大终端输出 |
| T41 | "怎么老盯着user" | **74,075 chars** | 大量工具调用 |

每次用户插入：
1. 前一轮的工具输出已附加到历史
2. CA 每次都要处理更臃肿的上下文
3. 下一次 topic grading 把更多轮降级到 L0/L1
4. 形成正反馈：**纠正→工具输出→上下文更大→降级更狠→错得更远**

T40 的 52K 和 T41 的 74K 工具输出来自助理在纠正过程中的终端操作——每次"纠正"产生的工具输出反而成为让历史更臃肿的元凶。

---

## 失效层 ④：用户强制插入的检测与汇编对策

### 问题

Hermes 的 `run_conversation()` 内部有一个工具循环（`while api_call_count < max_iterations`）：

```
pre_llm_call (C-stage)  ← 整个 turn 只触发一次
while 工具循环
  ├── API call (A-stage)
  ├── tool execution
  ├── if _interrupt_requested: break  ← 用户插入时中断
  └── loop
post_llm_call            ← 整个 turn 只触发一次
```

用户插入时，`_interrupt_requested` 标记 interrupt → 工具循环 break → 当前 turn 结束。但 CA 的 `pre_llm_call`（C-stage）已经完成，**不知道后续发生了中断**。被中断的 turn 的工具输出会被当作"已完成"纳入 CA 汇编。

下一次 `run_conversation`（用户的插入消息触发）时，CA 重新汇编全部历史——**包括被中断 turn 的不完整工具输出**。

### 检测信号

在 CA 的 `assemble()` 中，可以从 `conversation_history` 的结构中检测插入模式：

```python
# 检测逻辑：两个 user 消息之间，前一个 user 的 assistant 回应没有 text
# 说明前一个 turn 被用户中断了
def _is_interrupted_turn(self, messages, turn_index):
    """检查指定轮是否被用户强制插入中断。"""
    # 找该轮的用户消息
    user_msgs = [m for m in messages
                 if m.get("role") == "user"
                 and m.get("_turn_index") == turn_index]
    if not user_msgs:
        return False

    # 找该轮的 assistant 回应
    asst_msgs = [m for m in messages
                 if m.get("role") == "assistant"
                 and m.get("_turn_index") == turn_index]

    # 没有 text 回应（只有 tool_calls）= 被中断
    has_text = any(m.get("content", "").strip() for m in asst_msgs)
    return not has_text and len(asst_msgs) > 0
```

### 检测规则

**两个 user 消息之间，前一个 user 的 assistant 回应中没有 text content（只有 tool_calls）** → 该轮被中断。

在这个会话中：T38→T39→T40→T41 连续 4 轮全部是这个模式。

### 理想对策

1. **对话轮标记**：在 turn_plan 或 turn_cache 中加入 `is_interrupted: bool` 字段
2. **汇编策略差异**：被中断的轮：
   - 优先保留原文（L2）而非降级到 L1/L0
   - L1 摘要首词加 `[INTERRUPTED]` 标记
   - 不将其中间工具输出总结为"已完成动作"
3. **包含中断的用户消息**（如 T39 "不应该放memories"）→ 标记为 `[CORRECTION]`
4. **工具输出处理**：被中断轮的工具组自身也应标记，避免被后续汇编误当作"用户采纳的结果"

### 状态

**未实施**。检测逻辑可以独立实装，不依赖其他修复。

---

## 失效层 ⑤：L1 摘要语义丢失

CA 的 L1 摘要将用户意图篡改为事实陈述：

| 轮 | 原文 | L1 摘要 |
|---|---|---|
| T38 | "是不是将整个技术方案拆成若干wiki页面" | **【已实施】** 技术方案已拆分为... |
| T39 | "你怎么把项目文档丢memories里了？不应该放source/project下面吗？" | **【已实施】** 技术方案已拆分为 9 个模块化... plugins/ca_assembler/docs/ |

- 问题 → 结论
- 纠正 → 已实施
- 路径名被隐式替换（source/project → plugins/ca_assembler/docs/）

---

## 失效层 ⑥：T41 完全消失

T41 在 turn_plan 中不存在（无对应 plan entry）。用户在 T41 的"怎么老盯着user"——这场对话中最后的清醒纠正——被 CA 完全吞没。

`_assemble_status=1`（DEGRADED）导致该轮在 `_build_messages_from_plan()` 中被 skip。

---

## 附加问题：config.yaml 配置未生效

查看当前配置代码 `ca/config.py:71`：
```python
PROTECT_TAIL_TOKENS: ClassVar[int] = int(os.getenv("CA_PROTECT_TAIL_TOKENS", "10000"))
```

而 `~/.hermes/profiles/tester/config.yaml` 中写的是 `protect_tail_tokens: 20000`。
CA 读的是 env var 而非 config.yaml，YAML→env 映射链断裂。

**不修复**。20K char ≈ 5K tok 的默认平衡点是经过验证的信息密度最优值，
保持现状。若后续需要调整，改 `.env` 中的 `CA_PROTECT_TAIL_TOKENS` 即可。

---

## 修复记录

| 项目 | 状态 | 说明 |
|------|------|------|
| tail→turn 映射 bug | ✅ 已修 | `_tail_start_turn` 轮索引计算，不扩大预算 |
| 用户插入检测 | 📝 待办 | 需独立实装 `_is_interrupted_turn()` 检测逻辑 |
| L1 意图标签 | 📝 待办 | `[CORRECTION]`/`[QUESTION]`/`[INTERRUPTED]` 标记 |

---

## 数据来源

- `ca_cache/20260610_094540_d13093.db` — turn_cache, turn_plan 表
- `ca/__init__.py` — _compute_tail_start, _compute_turn_plan_v2, _build_messages_from_plan
- `agent/conversation_loop.py` — run_conversation 主循环结构
- `~/.hermes/profiles/tester/config.yaml` — protect_tail_tokens 配置
- Session FTS5 搜索 — 用户原文验证
