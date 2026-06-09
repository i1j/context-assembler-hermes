# ToolSummarizer 结构化摘要改进报告

**日期**: 2026-06-06  
**来源**: 调试会话中基于实际注入数据的观察  
**状态**: 进行中（已实现 10 个 handler，待改进 3 项）

---

## 一、已实现（10 handlers）

### 1.1 结构化摘要 handler

| # | handler | L0 改前示例 | L0 改后示例 | 说明 |
|---|---------|------------|------------|------|
| 1 | `_summarize_terminal` | `terminal: DB: 20260605...…L1: 13.4` | `terminal: find /home -name "*.py" (4 lines)` | 命令摘要 + 前5去重关键行。内置 pytest 检测 → `pytest: 8 passed, 2 skipped — 0.44s` |
| 2 | `_summarize_execute_code` | 同 terminal 碎片 | `execute_code: from hermes_tools... (42 lines)` | 代理到 terminal handler，替换 tool_name |
| 3 | `_summarize_write_file` | `write_file: 无返回数据` | `write_file: /home/i1j/test.txt` | 只保留文件路径 |
| 4 | `_summarize_patch` | `patch: 无返回数据` | `patch: /home/i1j/tool_summarizer.py` | 目标文件 + replace_all 标记 |
| 5 | `_summarize_read_file` | `read_file: {"content": "1|import pytest..."` | `read_file: /tmp/test.py` | 文件名 + 行数范围 |
| 6 | `_summarize_search_files` | `search_files: {"total_count": 0}` | `search_files: *.py → 0 hits` | 查询模式 + 命中数 + 前3文件名 |
| 7 | `_summarize_skill_manage` | `skill_manage: 失败` | `skill_manage: patch tester-workflow (error)` | 提取 action/name/file_path 关键参数；old_string/new_string 不进入 result_summary |
| 8 | `_summarize_memory` | `memory: {"success": false, ...}` | `memory: replace memory (error)` | 提取 action/target/old_text；content 预览 80 字符 |
| 9 | `_summarize_skills_list` | `skills_list: 无返回数据` | `skills_list: 3 skills (tester-workflow, ...)` | 提取技能名列表（前5） |
| 10 | `_summarize_skill_view` | `skill_view: 无返回数据` | `skill_view: tester-workflow — 12 lines` | 技能名 + 行数 |

### 1.2 架构设计

- **分发路径**: `summarize()` → `getattr(self, f"_summarize_{sanitized}", None)` → handler
- **工具名标准化**: `.` 和 `-` 自动映射为 `_`（`read-file` → `_summarize_read_file`）
- **失败兜底**: handler 异常时 `try/except` 回退到通用字段提取逻辑
- **L-stage 自动生效**: `lstage.py` 第 110 行调 `tool_summarizer.summarize()`，同一入口

### 1.3 关键设计决策

| 问题 | 讨论 | 结论 |
|------|------|------|
| 指纹去重 TODO | 旧累积快照模式的遗留注释，独立切片架构下不适用 | ❌ 删除标签，不入跟踪 |
| 失败工具 tool_args 截断 | 根因信息可能藏在 tool_args 中（如 memory 超容量原因） | ❌ 不截断 |
| terminal 跨子轮碎片 | 经查是独立工具调用，不是同一输出的分片 | ❌ 撤回判断 |
| 格式不统一（L1 JSON vs L0 文本） | L1/L0 不同抽象层级，正常设计 | ❌ 非问题 |
| 参数重排 | 不在 tool_args 动顺序（原始数据），在 result_summary 白名单提取 | ✅ 通过 `_pick_key_fields()` 实现 |
| pytest 输出结构化 | 检测 `passed` + `in X.Ys` 模式 | ✅ terminal handler 内建 |

---

## 二、已发现但待改进（3 项）

### 2.1 连续同工具空结果合并

**现象**:
```
[~/38/1] search_files: {"total_count": 0}
[~/38/2] search_files: {"total_count": 0}
```

两个连续 `search_files` 返回空，各自独立处理。LLM 不需要知道它搜了两次都空，一次就够了。

**根因**: `ToolSummarizer.summarize()` 是逐调用调用的，没有跨调用上下文。当前架构每次 `summarize()` 只处理一条 tool_call + 对应的 tool_responses，不知道前一条的结果。

**影响评估**: 低。偶发，且频繁空结果意味着工具调用在失败，LLM 看到一次就够了。

**修复方向**:
- 方案 A（CA 外部）: 在 A-stage 层合并连续同工具空结果 → 改 `__init__.py` 的 `_rebuild_messages()` 层
- 方案 B（CA 内部）: handler 内缓存上一个结果，匹配时合并 → 引入状态，复杂且不保证正确
- **推荐方案 A**: 在 A-stage 组装时去重（已有 `_deduplicate_messages` 机制）

**状态**: ✅ 已修复（插件层 `pre_llm_call()` 合并连续相同摘要，8 行，不动核心引擎）

---

#### 2.2 terminal 错误输出未突出标记 — ✅ 已修复

**现象**:
```
[~/38/39] terminal: Traceback (most recent call last):\n  File "<string>",...te3.OperationalEr
[~/38/48] terminal: Traceback (most recent call last):\n  File "<string>",...ModuleNotFoundEr
```

**修复**: `_summarize_terminal()` 内加 `_ERROR_RE` 正则检测关键词（Traceback/Error:/Exception/ModuleNotFound/ImportError/NotFound/failed/FAILED 等）。匹配时 `result_summary` 前缀 `[ERROR]`，L0 前缀 `[ERROR]`。

**改前 → 改后**:
```
terminal: python3 -c "bad_code()" (3 lines)
  result=[python3 -c] Traceback | File... | ModuleNotFound...
```
→
```
[ERROR] terminal: python3 -c "bad_code()" (3 lines)
  result=[ERROR] [python3 -c] Traceback | File... | ModuleNotFound...
  error=[ERROR]
```

---

#### 2.3 search_files 大量命中不显示分布 — ✅ 已修复

**现象**:
```
[~/38/39] terminal: Traceback (most recent call last):\n  File "<string>",...te3.OperationalEr
[~/38/48] terminal: Traceback (most recent call last):\n  File "<string>",...ModuleNotFoundEr
[~/38/49] terminal: Invalid config for tool 'version', skipping...
```

当前 terminal handler 对正常输出和错误输出一视同仁，只取前 5 行去重。`Traceback`、`ModuleNotFoundError`、`ImportError` 等错误行不特殊标记。

**影响评估**: 低。错误信息本身在前 5 行内，LLM 能看到错误类型。但 LLM 无法快速区分"这是错误"和"这是正常输出"，需要从头 parse。

**修复方向**: `_summarize_terminal()` 内检测 `Traceback|Error:|Exception|ModuleNotFound|ImportError|NotFound|Invalid` 等关键词，匹配时 `result_summary` 前缀加 `[ERROR]`，LLD 前缀加 `[ERROR]`。

```python
# 检测 terminal 错误输出
error_keywords = re.compile(
    r'(Traceback|Error:|Exception:|ModuleNotFound|ImportError|'
    r'NotFound|Invalid|Permission denied|No such file|syntax error)',
    re.IGNORECASE
)
has_error = any(error_keywords.search(l) for l in non_empty)
```

**状态**: ⏳ 待改

---

#### 2.3 search_files 大量命中不显示分布 — ✅ 已修复

**现象**:
```
[~/38/55] search_files: {"total_count": 66, "matches": [...]}
```

`_summarize_search_files` 只取前 3 个文件名，66 个命中全貌不可见。LLM 不知道匹配分布在哪些目录。

**修复**: `total_count > 3` 时解析所有文件路径，`Counter` 按文件所在目录名分组，取前 4 组填入 `result_summary`。命中 ≤3 时按原样展示文件名。

```
search_files: 66 matches [ca:30, tests:25, docs:11]
search_files: 2 matches (a.py, b.py)       ← ≤3 时原样
```

**状态**: ✅ 已修复

---

## 三、已排除（不进跟踪）

| 问题 | 原因 |
|------|------|
| 指纹去重 | 架构过时 TODO，已删除 |
| tool_args 截断 | 根因信息可能在其中 |
| terminal 跨子轮碎片 | 查证是独立调用，非分片 |
| 格式不一致 L1 vs L0 | 不同抽象层级的正常设计 |
| 失败工具不展示参数 | 用户纠正：失败需看参数找原因 |

---

## 四、当前 handler 清单（代码即文档）

在 `ToolSummarizer` 类中增加新 handler 的步骤：

1. 定义 `def _summarize_<tool_name>(self, tool_call_msg, tool_responses) -> Tuple[Dict, str]:`
2. 返回标准 L1 dict + L0 字符串
3. 工具名中的 `.` / `-` 自动映射（`execute-code` → `execute_code`）
4. 异常时自动 fallback 到通用逻辑

```python
def _summarize_web_search(self, tool_call_msg, tool_responses):
    """样例：web_search 结构化摘要"""
    ...
    return l1, l0
# executor 自动发现，无需注册
```
