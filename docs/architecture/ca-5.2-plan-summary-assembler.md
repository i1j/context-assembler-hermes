
> **⚠️ 历史存档 — v5.2 计划文档（CE 层移除前的过渡方案）。CA v5.10 的实际演进路线是直接移除 CE 层，改用 8 hooks (P-004)，未实现 `PlanSummaryAssembler`。**

# CA 5.2.0 — PlanSummaryAssembler: plan→纯文本组装器

## 变更摘要

移除 CE 层 4 个内联闭包函数，在 CA Core 新增 `assemble_plan_summary()` 方法。
统一 plan→纯文本的输出路径，消除 `_ce_build_summary` 的特殊包裹格式。

| 旧名 | 新名 |
|------|------|
| `_ce_build_summary()` | `ContextAssembler.assemble_plan_summary()` |
| `_ce_select_tail()` | 移除，用 `plan_result.bypass_turns` |
| `_ce_get_thought()` | 移除，逻辑内聚到 assembler |
| `_ce_get_l2_for_turn()` | 移除，逻辑内聚到 assembler |
| HistoryMemoryAssembler（设计文档名） | PlanSummaryAssembler |

## 职责

```
_ca_assemble 管线
  │
  ├── engine._compute_assemble_plan()           → plan_result (含 bypass_turns)
  ├── engine.assemble_plan_summary(...)          → 纯文本摘要（新增）← ★
  ├── _ce_extract_tail(messages, bypass_turns)  → 尾区消息列表
  └── 组装: [system, user(summary), *tail_msgs]
                （摘要与尾区皆空 → user_message fallback）
```

**只负责**：遍历 plan entries，跳过 bypass_turns，调 formatter 输出纯文本。
**不负责**：降级决策（CA plan 已定）、尾区计算（`plan_result.bypass_turns` 已提供）、
state.db 写回、turn_cache 写入。

## 方法签名

```python
def assemble_plan_summary(
    self,
    plan: List[TurnPlanEntry],
    session_id: str,
    bypass_turns: Set[int],
    messages: List[Dict],
) -> str:
```

**参数**：
- `plan` — 从 `_compute_assemble_plan` 得来
- `bypass_turns` — 从 `plan_result.bypass_turns` 得来
- `messages` — 原始消息列表（`_ca_assemble` 的入参，传给 `_format_tool_group_assembly` 用于翻工具数据）

## 内部逻辑

```python
lines = []

for entry in plan:
    if entry.turn_index in bypass_turns:
        continue

    elm, fct, hdl = self.store.read_turn_texts(
        session_id, entry.turn_index, entry.turn_type,
        entry.tool_sub_index,
        tool_group_api_count=entry.api_call_count,
    )

    if entry.turn_type == "dialogue":
        text = self._format_l1_for_display(fct) or hdl
        if not text:
            text = "本轮无新内容"
        lines.append(text)                      # 顶格

    elif entry.turn_type == "tool_group":
        thought_text = ""
        if elm:
            try:
                msgs = json.loads(elm)
                if msgs and msgs[0].get("role") == "assistant":
                    thought_text = (msgs[0].get("content") or "")[:100]
            except (json.JSONDecodeError, IndexError, TypeError):
                pass
        text = self._format_tool_group_assembly(
            "",                                  # group_l1_json 参数
            messages, entry.turn_index,
            entry.api_call_count,
            detail_level=entry.target_level,
            thought_text=thought_text,
        )
        if text:
            lines.append(f"  {text}")           # 缩进 2 空格

return "\n".join(lines)                         # 纯文本，无包裹符
```

## 输出格式

```
检查服务器状态 — uptime负载0.5正常
  查磁盘 — 运行系统命令 | df→/data 80% | free→mem 12/16G
你好，我是助理小明
本轮无新内容
  查日志 | nginx→timeout
```

| 元素 | 缩进 | 说明 |
|------|------|------|
| 对话行 | 0（顶格） | `_format_l1_for_display(fct)` 或 `hdl` |
| 工具组行 | 2 空格 | `_format_tool_group_assembly` pipe 格式 |
| 无有效内容 | — | 写入 `"本轮无新内容"` |
| 空摘要 + 空尾区 | — | `_ca_assemble` 直接透传 `user_message`（首轮/无历史时 fallback） |

消除旧 `_ce_build_summary` 的 `━` 包裹符和 `\n\n` 段间分隔。

## 与旧 `_ce_build_summary` 的关键差异

| 维度 | 旧 `_ce_build_summary` | 新 `assemble_plan_summary` |
|------|----------------------|---------------------------|
| 归属 | CE 层内联闭包 | CA Core 方法 |
| 包裹符 | `━` * 40 + `\n\n` 段间分隔 | 无包裹，`\n` 拼接 |
| 工具组缩进 | 无 | 2 空格 |
| Elm thought 来源 | `_ce_get_thought()` 从 messages 翻 | Assembler 从 store elm JSON 提取 |
| 空轮处理 | 跳过 | 写"本轮无新内容" |
| 尾区判断 | `_ce_select_tail()` 自算 | `plan_result.bypass_turns` |

## 变更文件

### `ca/__init__.py` — 新增 ~40 行

`ContextAssembler` 类内新增 `assemble_plan_summary()`，放在 `_format_tool_group_assembly` 之后（合理相邻），或 `_build_messages_from_plan` 之前（功能分组）。

### `__init__.py` — 替换 3 行 + 移除 ~130 行

`_ca_assemble` 内（line 704-711 附近）：

```python
# 旧
tail_set = _ce_select_tail(plan, n=2)
summary = _ce_build_summary(engine, plan, tail_set, messages)
tail_msgs = _ce_extract_tail(messages, tail_set)

# 新
summary = engine.assemble_plan_summary(
    plan, session_id, plan_result.bypass_turns, messages
)
tail_msgs = _ce_extract_tail(messages, plan_result.bypass_turns)
```

移除的函数（按移除顺序）：

1. `_ce_get_thought()` — 行 446-467，仅被 `_ce_build_summary` 调用
2. `_ce_select_tail()` — 行 469-485，被 `plan_result.bypass_turns` 替代
3. `_ce_get_l2_for_turn()` — 行 507-521，仅被 `_ce_build_summary` 调用
4. `_ce_build_summary()` — 行 523-653，被 `assemble_plan_summary()` 替代

保留：`_ce_extract_tail()` — 仍需要提取尾区消息。

### `tests/test_ce_pipeline.py` — 更新 3 个测试

| 测试 | 旧断言 | 新断言 |
|------|--------|--------|
| `test_basic_summary_output_format` | `"━" in summary` | 验证无 `━`、验证顶格/缩进格式 |
| `test_summary_with_tool_groups` | 验证 thought+工具行 | 验证新格式（内容不变） |
| `test_summary_separator_format` | 验证 `━` 分隔符 | 改为验证无 `━` + 行间 `\n` |

其余 7 个测试无需修改（M1、降级、尾区完整、重新汇编等与输出格式无关）。

## 影响面

```
影响文件:   3
新增代码:  ~50 行 (Assembler 方法)
移除代码:  ~130 行 (4 个 _ce_* 闭包)
受影响测试: 3/10（断言调整）
基线通过:  test_aligned_outcomes.py 29/29
           test_plugin.py         31/31
           test_ce_pipeline.py    10/10（改后仍全通过）
```

## 评审意见处理

| 问题 | 处理 |
|------|------|
| A: Elm thought 来源变化 | ✅ 接受。统一 formatter 路径，且 Elm  обычно 在尾区 |
| B: `group_l1_json` 未用 | ⚠️ 传 `""` 标记。既存问题不引入新 bug |
| C: 4 函数无其他调用者 | ✅ 安全移除，`test_ce_pipeline.py` 不引用它们 |
| D: 测试文件名 `test_ce_pipeline.py` 含 CE | ❌ 未动。功能测试完整，不影响运行 |
