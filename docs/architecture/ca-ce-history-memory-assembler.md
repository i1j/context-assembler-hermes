
> **⚠️ 已废弃 — ContextEngine 管线已在 v5.10 移除。CA 不再通过 `_compress_context` 接管 Hermes 消息构造。详见 P-004 (8 hooks)。**

# HistoryMemoryAssembler — 历史记忆表压缩摘要组装器

## 背景

CA 写入 `state.db` 的历史记忆表是**固定行数**的：不能删行，只能改旧行 + 追加新行。CE（Context Engine）插件利用 `agent._compress_context` 接口实现深拷贝 → 重写整个表 → 传给 LLM → 写回原始记录的流程。

`_ca_assemble` 中需要一段**历史摘要文本**作为用户消息传给 LLM。这段文本不应由 CE 自己遍历 plan、读 turn_cache、格式化——这些是 CA 的工作。CE 需要一个专门的组件，**复用 CA 的 formatter，只负责结构组织**。

## 组件定位

```
CA 核心引擎
  ├── _compute_assemble_plan          → plan（含 bypass_turns）
  ├── _build_messages_from_plan       → injection/mutation 用（消息列表）
  ├── _format_l1_for_display          → 对话轮格式化
  └── _format_tool_group_assembly     → 工具组格式化（pipe 格式）
        ↑
HistoryMemoryAssembler（CA 包内新增）  ← 复用以上 formatter，只做结构包裹
        ↑
  CE _ca_assemble              ← 调用 Assembler 拿到文本，塞入 user 消息
```

**不做：**
- 不处理 bypass/尾区（CA plan 已提供 `bypass_turns`）
- 不处理 state.db 写回
- 不处理 turn_cache 写入
- 不参与请求级别降级（CA `_compute_assemble_plan` 已决定 level）

**只做：**
- 遍历传入的 plan entries
- 跳过 bypass_turns
- 对话轮：调 `_format_l1_for_display` → 得到一行，顶格
- 工具组：调 `_format_tool_group_assembly` → 得到一行，缩进 2 空格

## 输入

```python
def assemble_history_summary(
    self,
    plan: List[TurnPlanEntry],
    session_id: str,
    bypass_turns: Set[int],
    messages: List[Dict],          # 原始消息（_format_tool_group_assembly 所需）
) -> str:
```

- `plan` — 从 `_compute_assemble_plan` 得来
- `bypass_turns` — 从 `plan_result.bypass_turns` 得来
- `messages` — 原始消息列表（CE `_ca_assemble` 的入参）

## 内部逻辑

```python
lines = []

for entry in plan:
    if entry.turn_index in bypass_turns:
        continue

    elm, fct, hdl = self.store.read_turn_texts(
        session_id, entry.turn_index,
        entry.turn_type,
        entry.tool_sub_index,
        tool_group_api_count=entry.api_call_count,
    )

    if entry.turn_type == "dialogue":
        text = self._format_l1_for_display(fct) or hdl
        if not text:
            text = "本轮无新内容"
        lines.append(text)                          # 顶格

    elif entry.turn_type == "tool_group":
        # 获取 thought
        thought_text = ""
        if elm:
            try:
                msgs = json.loads(elm)
                if msgs and msgs[0].get("role") == "assistant":
                    thought_text = (msgs[0].get("content") or "")[:100]
            except (json.JSONDecodeError, IndexError, TypeError):
                pass

        text = self._format_tool_group_assembly(
            fct if fct else hdl,
            messages,
            entry.turn_index,
            entry.api_call_count,
            detail_level=entry.target_level,
            thought_text=thought_text,
        )
        if text:
            lines.append(f"  {text}")               # 缩进 2 空格

return "\n".join(lines)
```

## 输出格式

```
【已实施】ca-ce-shell-to-real 模块全部 6 个里程碑完成...    ← 对话行，顶格
  查磁盘 — 运行系统命令 | df→80% | free→12G               ← 工具组 Fct 行，缩进 2 空格
  查内存 — free→mem 12/16G                                ← 同轮第二个工具组行
你好，我是助理小明                                          ← 下一对话行，顶格
  查日志 | nginx→timeout                                   ← 工具组 Hdl 行，缩进 2 空格
本轮无新内容                                                ← 无有效增量时的兜底
```

### 规则

| 元素 | 缩进 | 说明 |
|------|------|------|
| 对话行 | 0（顶格） | CA `_format_l1_for_display` 输出 |
| 工具组 Fct 行 | 2 空格 | `thought | tool→result` |
| 工具组 Hdl 行 | 2 空格 | `result | result` |
| 无有效内容 | — | `core_change="无有效增量"` → 写入"本轮无新内容"；其他空字段跳过 |
| 降级 | — | CE 不做降级，CA `_compute_assemble_plan` 已决定 level |

### 工具组行的缩进语义

对话行与工具组行是**同一级别的两个实体**——工具组不属于对话详情，而是并列关系。缩进表示"属于上面那个对话轮"，不退格。

## 尾区（bypass）

CA 的 `_compute_assemble_plan` 已提供 `bypass_turns`，尾区对话轮注入原始消息（OpenAI 格式），尾区工具组不处理（已被对话轮全量覆盖）。

Assembler 跳过 `bypass_turns` 中的 entry。尾区原始消息由 CE 从 `_saved_originals` 中提取，追加到 `messages_out` 末尾。

## CE 调用方式

```python
# CE _ca_assemble 中
engine = self._engine
plan_result = engine._compute_assemble_plan(user_message, context_length)
tail_msgs = _ce_extract_tail(messages, plan_result.bypass_turns)

summary_text = engine.assemble_history_summary(
    plan_result.plan, session_id,
    plan_result.bypass_turns, messages,
)

messages_out = [
    {"role": "system", "content": system_message},
    {"role": "user", "content": summary_text},
    *tail_msgs,
]
```

## 组件归属

CA 包内方法（`ContextAssembler.assemble_history_summary`），作为 CA 的公共方法。

理由：
- 需要访问 `self.store.read_turn_texts()`（读 turn_cache）
- 需要访问 `self._format_l1_for_display()` 和 `self._format_tool_group_assembly()`
- 不需要 CE 参与格式化逻辑
