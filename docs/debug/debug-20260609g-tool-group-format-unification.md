# CA 调试报告：工具组格式化统一 + 4 handler 字段提取修复（2026-06-09）

**日期**: 2026-06-09
**来源**: 本 session 全链路代码审查与修复
**涉及提交**: `f8e9207`, `98dbcc1`（在 `a65b64c` v5.0-pr3 基础上）

---

## 问题清单 & 修复

### 已确认修复（共 10 项）

| # | 问题 | 根因 | 修复 | 文件 |
|---|------|------|------|------|
| 1 | `group_result` raw JSON 泄漏 | `_sanitize_summary_text` 只认双引号 JSON，Python 单引号 `{'...'}` 漏过 | 加 `ast.literal_eval` fallback | `tool_summarizer.py` |
| 2 | `_format_group_summary` JSON 泄漏 | 同上 | 加 `ast.literal_eval` fallback | `__init__.py` |
| 3 | C-stage L1 含 debug 描述 | LLM 错误输出泄漏到 ctx | `_L1_DEBUG_PATTERNS` 13 个模式检测 | `__init__.py` |
| 4 | 绝对路径 `/home/i1j/` 泄漏 | handler 构造 result_summary 时未脱敏 | `_sanitize_path()` + `replace("/home/i1j", "~")` 三处 | `tool_summarizer.py` + `__init__.py` |
| 5 | L1 工具组显示 thought | thought 对历史认知信息价值为零 | 去掉 `if thought:` 分支 | `__init__.py` |
| 6 | `text[:200]` 组级总上限 | thought 和 result 争用 200 字符预算 | 去掉，tool 各自截断 | `__init__.py` |
| 7 | L0/L1 两套格式 | L0 直接拼 raw `l0_text` | 统一走 `_format_group_summary(l1)` | `__init__.py` |
| 8 | 组级 `[:60]` / `[:55]` / `[:80]` 二次截断 | 字符限制基于工具组而非工具轮 | 全部移除 | 3 文件 |
| 9 | `_summarize_todo` raw dict 泄漏 | 无专用 handler，走通用 fallback | 新 handler 提取 `summary` 字段 | `tool_summarizer.py` |
| 10 | 4 handler 字段提取不全 | `_summarize_skill_manage` / `_write_file` / `_terminal` / `_search_files` 未从已知 JSON 字段提取 | 每个 handler 改为从 JSON 提取结构化字段 | `tool_summarizer.py` |

### 4 handler 具体修复

| Handler | 改前（泄漏） | 改后（结构化） |
|---------|-------------|---------------|
| `_summarize_skill_manage` | `{"success": true, "message": "..."}` → `output[:300]` | `success=True` → 提取 `message` 字段 |
| `_summarize_write_file` | `{"bytes_written": 4000, ...}` → `output` 直出 | 提取 `bytes_written` → `"已写入: {path} ({N} 字节)"` |
| `_summarize_terminal` | `"[cd ~/... && cmd] {body}"`（cmd 占 60 字符） | body 在前 cmd 在后（结果优先） |
| `_summarize_search_files` | `str(f)` 对 `{"path":..., "line":N}` → Python repr | `f.get("path", str(f))` |

---

## 修复原则（架构澄清）

1. **工具组 = 单工具摘要的并联**：组级只做数量限制（`[:3]` / `[:5]`），不做字符二次截断
2. **字符限制基于工具轮，非工具组**：每个 handler 的 L0（≤100 字符）和 `result_summary` 在 handler 层各自截断
3. **字段提取而非文本清洗**：Hermes 返回 JSON 结构，handler 从已知字段提取，不做文本清洗
4. **thought 不注入**：thought 是 AGENT 运行时状态，对历史认知信息价值为零

---

## 生效条件

重启 Hermes tester profile 进程后生效。存量 `ca_cache/*.db` 中的旧格式数据不会自动重写。

---

## 后续未决（非问题，认知）

- `_summarize_memory` 的 `result_summary` 也走 `output[:300]` 路径，但 memory 操作的 JSON 字段（`success`/`error`）已被提取处理。如果出现 `{"success": true}` 而无 `message` 字段的响应，仍会回滚到 `"memory: {action} {target} (ok)"`（L0格式），可接受。
- 连续同工具空结果合并：已在插件层实现，核心引擎不动。
