# CA 调试报告：3 个遗留问题修复 + ctx 验证（2026-06-09）

## 时间：2026-06-09

## 来源

debug-20260609-1842-injection-layer-fix-batch.md 的「当前 ctx 附加问题（已标注，未修）」3 项。

## 修复内容

### 修复 A：group_result raw JSON 泄漏

**文件**：`ca/tool_summarizer.py`

**根因**：
1. 通用 fallback handler (`summarize()` → `_extract_fields()`) 提取 `result_summary` 时，若 tool response 中 `result`/`output` 值是 dict/list，JSON 序列化后得到 raw JSON 字符串
2. `_format_group_summary` 的 `group_result` 直接展示此 raw JSON

**修复**：
- 新增 `_sanitize_summary_text()` 静态方法：检测 dict/list 值 → 尝试从 `result`/`output`/`summary`/`message` 提取文本字段 → 否则压缩为简短描述
- 通用 fallback handler 提取 `result_summary` 时使用 `_sanitize_summary_text()`
- `_format_group_summary`（`ca/__init__.py`）增加防御性清理：检测 `group_result` 是否以 `{` 开头 → 尝试解析 JSON 提取文本字段

### 修复 B：C-stage L1 含调试描述

**文件**：`ca/__init__.py`

**根因**：LLM 对话轮 L1 摘要生成失败时，`_format_l1_for_display` 将非 JSON 文本直接注入 ctx。debug 报告观察到 `"当前会话 CA 注入 ctx 中完全无工具组..."` 等自我引用描述出现在 `[~/N/0]` 条目中。

**修复**：
- 新增 `_L1_DEBUG_PATTERNS` 元组（13 个模式）：`"当前会话"`, `"CA 注入"`, `"CA插件"`, `"此会话"`, `"ctx 中"`, `"无工具组"` 等
- 仅在 L1 数据已损坏时（JSON 解析失败 或 JSON 无 `core_change`）执行 pattern 匹配
- 正常 L1 JSON（含有效 `core_change`）走正常格式化路径，不触发 pattern

### 修复 C：绝对路径暴露

**文件**：`ca/tool_summarizer.py`, `ca/__init__.py`

**根因**：`_summarize_read_file` / `_summarize_write_file` / `_summarize_patch` / `_summarize_search_files` 的 `result_summary` 中包含 `/home/i1j/...` 完整路径。

**修复**：
- 新增 `_sanitize_path()` 静态方法：`/home/i1j` → `~`
- 应用到 `read_file` / `write_file` / `patch` 三个 handler 的 `path_short`
- `_format_group_summary` 的 `group_result` 增加 `result.replace("/home/i1j", "~")`

## 代码变更汇总

| 文件 | 变更 |
|------|------|
| `ca/tool_summarizer.py` | 新增 `_sanitize_path()`、`_sanitize_summary_text()` 静态方法；read_file/write_file/patch handler 使用 `_sanitize_path`；通用 fallback handler 使用 `_sanitize_summary_text` |
| `ca/__init__.py` | `_format_group_summary` 增加 group_result 防御性 JSON 清理 + 路径脱敏；`_format_l1_for_display` 增加 `_L1_DEBUG_PATTERNS` 元组和 debug 文本过滤 |

## 测试验证

```
287 passed, 20 skipped, 0 failed
```

全活跃测试套件（含 tool_buffer、pr3_injection），无回归。

## ctx 验证

重启前积累的 cache 数据中仍然可见：
- `[~/1/1]` ～ `[~/2/8]`：`/home/i1j/...` 路径泄漏（Fix C 未生效前的旧数据）
- `[~/2/1]`：`({'path': ...})` Python dict 泄漏（Fix A 未生效前的旧数据）
- 无对话轮 L1（`[~/N/0]`）条目出现，Fix B 需重启后验证

## 生效条件

重启 Hermes tester profile 进程后生效。新会话的 C-stage / A-stage 使用更新后的代码。存量 cache 数据中的旧格式不会自动重写。
