# CA 调试报告：注入层微修复批（2026-06-09）

## 时间：2026-06-09

## 触发条件

用户观察到 CA 注入 ctx 中：
- `[~/N/1]` 格式的工具组条目中，同 turn 多组 L1 摘要内容相同（全部为第一个组的）
- `[~/5/1]` 工具组 thought 文本双倍重复
- `[~/1/1]`/`[~/2/1]`/`[~/3/1]` 裸文本无 `工具组：` 前缀
- `[~/3/0]` 第二行 `- 无` 为空信息噪声

## 修复清单

### 修复 1：read_turn_texts l1/l0 缺 api_call_count 过滤

**文件**: `ca/store.py:818-828`

**根因**: `read_turn_texts` 中工具组 l1/l0 的 SQL 查询未按 `api_call_count` 过滤，导致同 turn 多工具组全部返回第一个 assistant{tc} 行的 l1 文本。

**现象**: turn=1 的 4 个工具组（api=1..4）全部显示 `search_files: 0 matches`，而非各自应该的 0/1 匹配 / AGENTS.md 摘要 / changelog 摘要。

**修复**: 对 tool_group 路径加 `AND api_call_count=?` 过滤：

```python
if tool_group_api_count is not None:
    cur = self.conn.execute(
        """SELECT l1_text, l0_text
           FROM turn_cache
           WHERE session_id=? AND turn_index=? AND role='assistant' AND seq_index=0 AND api_call_count=?""",
        (session_id, turn_index, tool_group_api_count),
    )
```

### 修复 2：_format_group_summary thought + intent 重复

**文件**: `ca/__init__.py:1684-1687`

**根因**: `generate_group_summary()`（`tool_summarizer.py:786-790`）同时返回 `group_intent`（thought 首句 ≤80 字符）和 `thought`（完整 thought ≤200 字符）。`_format_group_summary` 无条件同时输出 `[思考] {thought}` 和 `{intent}`，造成相同文本两次。

**现象**: `[~/5/1] 工具组：[思考] 好问题。让我直接查 `_extend_with_l2` 和工具组 L2 数据来源。好问题。让我直接查 `_extend_with_l2` 和工具组 L2 数据来源。→search_files:...`

**修复**: `if intent:` → `elif intent:`，thought 已包含 intent 时跳过：

```python
if thought:
    parts.append(f"[思考] {thought}")
elif intent:
    parts.append(intent)
```

### 修复 3：L0 工具组缺 "工具组：" 前缀

**文件**: `ca/__init__.py:1553,1565`

**根因**: L0 级别工具组走 `elif l0:` 路径直接输出 `${prefix}${l0}`，未加 `工具组：` 前缀。L1+ 工具组经 `_format_group_summary` 有前缀，造成格式不一致。

**现象**:
```
[~/1/1] search_files: *context*assembler* → 0 hits | search_files: *ca* → 20 hits   ← 裸
vs
[~/4/1] 工具组：→skill_view: tester-workflow — 1 lines（1个，ok）                     ← 有前缀
```

**修复**: `l0` 路径改为 `${prefix}工具组：${l0}`（两处 L0 fallback 均修）。

### 修复 4：_format_l1_for_display 占位符噪声

**文件**: `ca/__init__.py:1660-1662`

**根因**: C-stage LLM 在 OODA 格式的 `new_materials`/`objective_facts` 为空时产出占位符 `"无"` / `"-"` / `"- 无"` / `"无有效内容"`。注入层不做过滤直接输出。

**现象**: `[~/3/0]` 第二行 `- 无`，消耗 token 无信息。

**修复**: 在输出前过滤掉这些占位符文本。

## 测试验证

```
119 passed, 4 skipped, 0 failed
```

全活跃测试套件，无回归。

## 问题对比

| 修复 # | 问题 | 显式测试 | 说明 |
|--------|------|---------|------|
| 1 | l1 缺 api_count 过滤 | 无（新增覆盖缺口） | 同 turn 多工具组的 l1 区分未被测试到 |
| 2 | thought+intent 重复 | 有 | `test_format_group_summary` 及各项连测覆盖 |
| 3 | L0 前缀缺失 | 无（新增覆盖缺口） | 测试未选验证 L0 工具组的输出格式 |
| 4 | L1 占位符噪声 | 无（新增覆盖缺口） | 测试未验证 `_format_l1_for_display` 的输入过滤 |

## 当前 ctx 附加问题（已标注，未修）

| 问题 | 说明 |
|------|------|
| `_format_group_summary` 的 `group_result` 含 raw JSON | ToolSummarizer handler 的 `result_summary` 包含 JSON 原文（`{"status":"success","output":...}`），非本次范围 |
| C-stage L1 摘要含 raw 调试描述 | `[~/1/0]` 第 2-3 行出现 "当前会话 CA 注入 ctx 中完全无工具组..."——LLM 输出质量问题 |
| 绝对路径暴露 | `read_file: /home/i1j/...` 文件路径在 result_summary 中透传 |

## 生效条件

重启 Hermes tester profile 进程后生效。新会话的 A-stage 使用更新后的注入逻辑。
