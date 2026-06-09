# CA 部署调试修复 — 2026-06-07

## 概述

针对 Hermes agent 在线分析发现的 3 个上下文注入质量问题，逐一验证、定位根因并修复。
所有修复通过 `tests/` 测试套件验证（基线 243 pass / 10 fail → 修复后 244 pass / 9 fail，无新回归）。

---

## 问题 1 — 对话轮退化条目注入

### 现象

`_assemble_status=1`（L1 生成退化）的对话轮仍然被注入上下文，内容为空 JSON：

```
[~/1/0] {"core_change": "本轮无新内容", "new_materials": [], "objective_facts": [],
         "consensus": [], "todo": [], "_assemble_status": 1}
```

### 根因

`_build_messages_from_plan()` 没有检查退化状态。退化条目的数据流：
```
DB(l0="") → read_turn_texts 返回 l0="" → if l0: False → elif l1: True → 注入退化 JSON
```

`_is_valid_summary()` 在 `_compute_turn_plan_v2` 中存在但只用于 token 估算，从未在 `_build_messages_from_plan` 中用于消息构建过滤。

### 影响范围

- turn 1/2/3/5 共 4 轮（初始 embedding 超时导致，warm-up 轮次）
- 每轮贡献约 121 chars 垃圾 token
- 原始 JSON `{"output": "..."}` 被 split("\n") 逐行切割，第一行保留 JSON 结构文本被截断至 100 chars，无信息密度

### 修复

`ca/__init__.py` — `_build_messages_from_plan()`:

```python
if entry.turn_type == "dialogue" and l2 is not None:
    db_status = self.store.read_assemble_status(
        self._session_id, entry.turn_index,
        entry.turn_type, entry.tool_sub_index
    )
    if db_status == 1:  # ASSEMBLE_PENDING_BACKFILL
        continue
```

- 仅检查 DB 记录（`l2 is not None`），不影响 `cache.add_turn()` 写入但未落库的测试用例

`ca/store.py` — 新增方法:

```python
def read_assemble_status(self, session_id, turn_index,
                         turn_type="dialogue", tool_sub_index=0):
    """返回该 turn 的 _assemble_status。记录不存在时返回 None。"""
```

---

## 问题 2 — 工具轮 L0 含原始 JSON 包裹

### 现象

terminal 工具轮的 L0 摘要包含 `{"output": "..."}` 的原始 JSON 文本：

```
[ERROR] t:grep -i 'CA plugin started\|_o → {"output": "2026-06-06 20:45:58,711 WARNING [20260"
```

### 根因

`tool_summarizer.py:129` 中 `_summarize_terminal()` 的 L0 生成逻辑：
- 工具 response 是 Hermes 标准 JSON 格式 `{"output": "...", "exit_code": 0}`
- `L0: `f"{tool_label}:{cmd_part} → {out_part}"[:100]` — 其中 `out_part = key_lines[0][:50]`
- `key_lines[0]` 来自 `non_empty[0]`，即 `split("\n")` 后的第一行
- 由于响应是 JSON 包裹（单行），`non_empty[0]` 就是完整的 `{"output":..., "exit_code": 0}` 结构

首次引入于 `dc29da6`（"ToolSummarizer L0 信息密度改进"），意图是展示输出内容而非行数，
但假设了 `key_lines[0]` 是「有意义的输出文本」而非 JSON 包裹的响应元数据。

### 修复

`ca/tool_summarizer.py` — `_summarize_terminal()`:

```python
raw_text = "".join(raw_text_parts)
try:
    parsed = json.loads(raw_text)
    if isinstance(parsed, dict) and "output" in parsed:
        extracted_output = parsed["output"]
except (json.JSONDecodeError, TypeError):
    pass

non_empty_lines = extracted_output.split("\n") if extracted_output else output_lines
non_empty = [l for l in non_empty_lines if l.strip()]
```

### 效果

```
修复前: [ERROR] t:grep -i 'CA plugin started\|_o → {"output": "2026-06-06 20:45:58,...
修复后: [ERROR] t:grep -i 'CA plugin → 2026-06-06 20:45:58,711 WARNING [20260607...
```

---

## 问题 3 — 对话轮 L1 JSON 原文注入

### 现象

对话轮 L1 摘要以原始 OODA JSON 格式注入，~37% token 消耗在 JSON 语法结构上：

```
[~/7/0] {"core_change": "确认 hermes-32k 被 standalone 脚本加载...",
         "new_materials": [...], "objective_facts": [...], ...}
```

### 根因

自 v4.5.0（`3ac8753`，turn_plan 驱动消息组装引入）起，`_build_messages_from_plan()` 一直将 `l1`（`json.dumps(cleaned, ensure_ascii=False)` 的结果）原文注入到 `[~/N/0] prefix + l1`。v4.7.0（`4ca25df`，L1 摘要系统重构）只改了 LLM 输出解析端（OODAParser → `parse_v1_markdown_xml`），存储格式和注入方式完全没变。

### 修复

`ca/__init__.py` — 新增 `_format_l1_for_display()` 方法：

```python
def _format_l1_for_display(self, l1_text: str) -> str:
    data = json.loads(l1_text)
    core = data.get("core_change", "")
    lines = [core]
    for key in ("new_materials", "objective_facts"):
        items = data.get(key, [])
        if items:
            joined = " | ".join(str(i)[:240] for i in items)
            lines.append(f"  {joined}")
    return "\n".join(lines)
```

在 `_build_messages_from_plan()` 中，对话轮统一使用 `l1_display` 替代 `l1`：

```python
l1_display = self._format_l1_for_display(l1) if entry.turn_type == "dialogue" else l1
```

替换覆盖全部 3 个分支（L2/L1/L0 fallback）。

### 效果

```
修复前: [~/7/0] {"core_change": "确认 hermes-32k 被 standalone 脚本加载，需主动卸载以释放 VRAM",
                  "new_materials": ["hermes-32k 模型被加载，占用 9.4GB VRAM",
                                    "请求使用原生 Ollama API 而非 OpenAI 兼容接口"],
                  "objective_facts": ["Ollama 原生 API 接口为 `/api/generate`",
                                      "代理日志显示 UA 为 Python-urllib/3.12"],
                  ...}
修复后: [~/7/0] 确认 hermes-32k 被 standalone 脚本加载，需主动卸载以释放 VRAM
                 hermes-32k 模型被加载，占用 9.4GB VRAM | 请求使用原生 Ollama API...
                 Ollama 原生 API 接口为 `/api/generate` | 代理日志显示 UA 为 Python-urllib/3.12
```

背景审查轮次（"系统后台审查"）从 48 字符 JSON 缩减为单一短行 `[~/N/0] 系统后台审查`。

---

## 测试验证

| 指标 | 基线（未改） | 修复后 |
|------|------------|--------|
| 通过 | 243 | **244** |
| 失败 | 10 | **9** |
| 跳过 | 10 | 10 |

修复前失败 10 个、修复后失败 9 个（`test_c_c_T1_truncated_fallback` 意外通过），
9 个 pre-existing 失败完全一致，**无新回归**。

---

## 涉及文件

| 文件 | 改动 | 行数 |
|------|------|------|
| `ca/__init__.py` | 退化条目跳过注入 + `_format_l1_for_display` 新方法 + `l1_display` 替换 | +35 / -7 |
| `ca/store.py` | `read_assemble_status()` 新方法 | +13 |
| `ca/tool_summarizer.py` | `_summarize_terminal` JSON 输出解析 | +12 / -3 |
