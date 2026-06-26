---
title: 工具轮规则引擎
slug: tool-summarizer-rules
category: decision
date: "2026-05"
version_introduced: v1.0
alternatives: ["每工具轮调 LLM 摘要（3~5 秒延迟）", "纯正则提取（丢失结构化信息）"]
chosen: "四级字段优先级规则引擎，不调 LLM"
affects: []
status: 已实装
updated: 2026-06-25
source_files: ["ca/tool_summarizer.py"]
---

## 触发条件

工具输出需要从 JSON 中提取关键信息，摘要格式需标准化。

## 备选方案

1. **LLM 摘要**（3~5 秒延迟，不可接受）
2. **规则引擎（选定）**：四级字段优先级提取
3. **纯 truncation**：丢失结构化信息

## 选定

- 工具输出 JSON 通过四级字段优先级提取（`result` > `error` > `content` > `data`）
- 摘要格式：`[tool_name] {tool_name}: core_value truncated_to_100ch`
- 10 个结构化 Handler 覆盖常见工具类型
- 配置三段回退（default → tool-specific → handler）

旧决策节点：`C-004`, `C-005`, `C-006`, `C-007`, `C-008`, `C-009`, `T-001`, `T-002`

## 影响

✅ 零 LLM 延迟：规则引擎毫秒级完成
✅ 三级回退配置灵活
❌ 100ch 截断可能丢失关键细节

---

## execute_code 结构化摘要（v5.10+ 新增 Handler）

### 背景

`execute_code` 工具输出原始 JSON：
```json
{
  "status": "success|error|timeout",
  "output": "stdout 文本（可能包含巨量数据）",
  "tool_calls_made": 2,
  "duration_seconds": 19.62,
  "error": "仅 error 状态时存在"
}
```

旧 handler 代理给 `_summarize_terminal`（按原始 stdout 处理），导致：

| 问题 | 影响 |
|------|------|
| JSON 外壳碎片泄漏 | 工具轮摘要含 `{"status":"success","output":"..."}` 前后行碎片 |
| 元数据（`duration_seconds`/`tool_calls_made`）原文注入 | A-stage 上下文 100% 被这些 LLM 决策无关值占用 |
| 状态信号丢失 | 只能通过 `exit_code` 间接判断，`execute_code` 无此字段 |
| 巨量输出不经筛选 | P99 输出 45.6KB 原文注入 Fct，A-stage 成本爆炸 |

### 真实数据验证基线

来源：9 个 CA session × 137 条 execute_code 行

| 维度 | 值 |
|------|-------|
| 可解析 JSON | 137/137 (100%) |
| 有意义的 stdout | 136/137 (99%) |
| status=success | 101 (73%) |
| status=error | 37 (27%) |
| thought ≥500 字符 | 72 (52%) — 意图已被 thought 充分覆盖 |

**输出大小分布（result_summary 原文）：**

| 百分位 | 旧 Fct | 新 Fct | 压缩倍数 |
|--------|:-------:|:-------:|:-------:|
| P50 | 903B | ~80B | **~11x** |
| P90 | 6.7KB | ~150B | **~45x** |
| P99 | 45.6KB | ~200B | **~228x** |
| 合计 | 376KB | 42KB | **89%** |

### 设计原则

**云 LLM 在 A-stage 看 execute_code 的 Fct 时，唯一需要的信息是「这段代码跑出了什么结论」。**

| 不需要 | 需要 |
|--------|------|
| JSON 外壳 `{"status","output","tool_calls_made","duration_seconds"}` | `status`：成功/出错/超时 |
| `code` 代码体（已由 `_clean_tool_args` 剥离） | output 的**前 2-3 个非空行** |
| LLM 无关元数据 `duration_seconds` | `error`：traceback 类型 + 首行错误信息 |
| stdout 巨量数据（SQL 结果、grep 匹配、DB 打印） | 状态变化的关键结论 |
| `tool_calls_made` | — |

### 实现流程

```
┌─ input ─────────────────────────────┐
│ tool_call_msg + tool_responses[].content │
└─────────┬───────────────────────────┘
          ▼
  JSON 解析 → 提取 status/output/error
          │
          ▼
  输出行清洗：
  ├─ 去空行
  ├─ 去 JSON 外壳碎片（`{"status"`, `"output"` 开头行）
  ├─ 去纯分隔符行（>50% 字符为 `=-_*`）
  └─ 取前 3 个有意义的 key_lines
          │
          ├── 检测 pytest 输出（`N passed / failed` 正则）
          │     └→ 结果摘要: `pytest: 19 passed in 0.58s`
          │
          └── 通用提取:
                ├─ 成功态: `一、测试文件存在性验证 | ❌ 缺失: tests/...`
                └─ 错误态: `error: Traceback (most recent call last):`
          │
          ▼
  ┌─ Fct 构建 ─────────────────────┐
  │ tool_name: "execute_code"      │
  │ tool_args: {}  (code 已剥离)    │
  │ result_summary: 提取后的内容     │
  │ error: 仅在 error/timeout 时填充 │
  │ implicit_knowledge: []          │
  │ next_action_hint: ""            │
  │ _assemble_status: 0             │
  └────────────────────────────────┘
  ┌─ Hdl 构建 ─────────────────────┐
  │ 错误态: `exc@{status}: {error_msg首行50ch}`  │
  │ 成功态: `exc: {首行有意义输出50ch}`          │
  │ 空输出: `exc: (N lines)`                       │
  └────────────────────────────────┘
```

### Fct 格式

```json
// 成功态
{
  "tool_name": "execute_code",
  "tool_args": {},
  "result_summary": "一、测试文件存在性验证 | ❌ 缺失: tests/audit/cross_ref_wiki_audit.py | ❌ 缺失: tests/generate_t",
  "error": null,
  "implicit_knowledge": [],
  "next_action_hint": "",
  "_assemble_status": 0
}

// 错误态
{
  "tool_name": "execute_code",
  "tool_args": {},
  "result_summary": "error: Traceback (most recent call last):",
  "error": "Traceback (most recent call last):\n  File \"/tmp/hermes_sandbox.../script.py\", line 4, in <module>\n    raise ValueError(\"nope\")\nValueError: nope",
  "implicit_knowledge": [],
  "next_action_hint": "",
  "_assemble_status": 0
}
```

**`result_summary` 内容规则：**

| 条件 | result_summary 格式 |
|------|-------------------|
| pytest passed/failed | `pytest: N passed — Xs` / `pytest: N passed, M failed — Xs` |
| 有 key_lines | `{key_line_1} \| {key_line_2} \| {key_line_3}` （最多 3 行，每行 120ch） |
| 有 error 无 key_lines | `({error第一行80ch})` |
| 完全空 | `(N lines)` |

**`result_summary` 的 Hdl（`l0`）格式：**

| 条件 | Hdl 格式 |
|------|---------|
| 错误 + error_msg | `exc@{status}: {error_msg首行50ch}` |
| 错误 + 无 error_msg + key_lines | `exc@{status}: {key_lines[0]前50ch}` |
| 错误 + 无任何内容 | `exc@{status}` |
| 成功 + key_lines | `exc: {key_lines[0]前50ch}` |
| 成功 + 空 | `exc: (N lines)` |

**Hdl 前缀说明：** 使用 `exc` 而非 `execute_code`（13ch → 4ch），与 `tool_name` 列已存在于 turn_stream 同一行配套。前缀统一风格参见其他 Handler（write_file/patch/read_file）。

### 关键边界处理

| 场景 | 处理 |
|------|------|
| 纯分隔符输出（`===...===`） | `_is_delimiter_heavy()` 检测，跳过 |
| 错误态首行为分隔符（原 `exc@error: =========`） | 改用 error_msg 作为 Hdl 来源 |
| traceback 错误 | status=`error`, error_msg 原文保留，result_summary 提取首行 |
| 超时 | status=`timeout`，与 error 相同处理路径 |
| 无 `error` 字段的错误 | status=`error` 但 error 字段缺失，回退用 stdout 首行 |
| 空输出 | `(N lines)` 兜底 |
| 大数据量（P99 45KB） | 前 3 行 key_lines 提取，89% 压缩 |
| JSON 解析失败 | 回退到通用 `_summarize_generic` 逻辑 |
| `tool_calls_made > 0` | 追加 `[N tc]` 后缀到 result_summary |

### 与旧 handler 对比

| 场景 | 旧 Hdl（_summarize_terminal 代理） | 新 Hdl |
|------|-----------------------------------|--------|
| 纯 `=====` 输出 | `{"status":"success","output":"===============` | `exc: TOPIC GRADE & SUMMARY GRADE PER TURN (from A-stage` |
| 错误 + `=====` | `{"status":"error","output":"===============` | `exc@error: Traceback (most recent call last):` |
| pytest 结果 | `pytest (19 passed) — 0.58s` | `pytest (19 passed) — 0.58s`（保留不变） |
| 有意义输出 | `一、测试文件存在性验证`（无前缀） | `exc: 一、测试文件存在性验证`（有前缀） |
| 数据量 | 376KB 总量 | 42KB 总量（89% 压缩） |

### 代码结构

```python
def _summarize_execute_code(self, tool_call_msg, tool_responses):
    """
    摘要管道:
    1. 解析 tool_responses[0].content 为 JSON
    2. 提取 status/output/error/tool_calls_made
    3. 过滤分隔符/JSON碎片行，提取前 3 个有意义 key_lines
    4. 检测 pytest 输出模式
    5. 构建 result_summary（选择最佳格式）
    6. 构建 Fct dict + Hdl
    """
```

位于 `ca/tool_summarizer.py` 的 `_summarize_execute_code` 方法。测试覆盖 13 个场景位于 `tests/unit/test_tool_summarizer.py` 的 `TestExecuteCodeHandler` 类。

### 测试覆盖

| 测试方法 | 场景 |
|----------|------|
| `test_basic_output` | 基本成功态：前 3 行提取 |
| `test_error_with_traceback` | traceback 错误态 |
| `test_pytest_detection` | pytest passed+failed |
| `test_pytest_passed_only` | pytest passed only |
| `test_empty_output` | 空输出 |
| `test_code_stripped_from_args` | 代码体从 tool_args 剥离 |
| `test_timeout_output` | 超时状态 |
| `test_error_without_error_field` | 无 error 字段的错误 |
| `test_tool_calls_count_in_summary` | tool_calls_made 追加后缀 |
| `test_no_tool_calls_no_suffix` | 无 tool_calls 不追加 |
| `test_non_json_response_fallback` | JSON 解析失败回退 |
| `test_multiple_tool_responses` | 多 response 行 |
| `test_handler_dispatch` | dispatch 路由正确
