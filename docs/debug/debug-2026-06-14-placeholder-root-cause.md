# 调试记录 2026-06-14 — 占位符根因追查

## 问题

turn 7 的 L1 JSON DB 中存在 `"new_materials": ["- 无"]` 和 `"objective_facts": ["- 无"]`。

## 追查过程

### 1. 逐层检查过滤链

`clean_increment`（旧版 `post_process.py:58`）：
```python
items = [i for i in items if i and i.strip()][:3]
```
`"- 无".strip()` → `"- 无"`（非空字符串，truthy）→ **通过**。旧版 `clean_increment` 不拦截。

`parse_v1_markdown_xml`（`post_process.py:304`）：
```python
if item and item not in ("无", "無", "none"):
```
regex `^[-*•]\s+(.+)$` 从 `- 无` 捕获 group(1)=`"无"` → `"无"` 在排除集中 → **跳过**。所以 `current_items` 不应有 `"- 无"`。

### 2. 穷举 LLM 输出格式变体

测试了全部 bullet 变体：`- 无`、`-无`（无空格）、`* 无`、`• 无`、`— 无`、`– 无`、`－ 无`（全角）、纯文字 `无`、`\t- 无`、` - 无`（前导空格）——**全部被 regex 正确过滤**。

### 3. 调真实 LLM 构造输出

用 `L1_GENERATION_PROMPT` + 真实 `l2_text` + `think: false` 调 `qwen3.5:hermes-32k`，输出标准格式：
```
### 现象与问题
- 无
```
→ `parse_v1_markdown_xml` 正确过滤 → `clean_increment` 输出 `{}`（无 new_materials）。
**即当前代码 + 当前 LLM 不会产生 `["- 无"]`。**

### 4. `clean_increment` 的调用方

仅两个位置，都是 `parse_v1_markdown_xml(response_text)` → `clean_increment(l1_dict)`：
- `ca/__init__.py:452` — C-stage 主路径
- `ca/lstage.py:115` — backfill 路径

无其他函数向 `clean_increment` 传参。中间亦无其他处理步骤。

### 5. 结论

**根因未完全查明。** `["- 无"]` 在 DB 中是事实（hex: `2d20e697a0`），但 `parse_v1_markdown_xml` 对所有我能构造的 LLM 输出格式都正确过滤。最可能的原因：
- LLM 在某个时间点输出了代码预期之外的格式（缓存状态、模型版本变化、prompt 变更时的中间状态）
- 或该轮次的 `response_text` 经过了一些我没能复现的截断/拼接

## 修复

`clean_increment` 加 `_PLACEHOLDERS` 集合（`post_process.py`）：
```python
_PLACEHOLDERS = {"", "无", "無", "none", "-", "- 无", "—", "— 无", "暂无", "无有效内容"}
items = [i for i in items if i and i.strip() and i.strip() not in _PLACEHOLDERS][:3]
```

这是最终防护层——无论 LLM 输出格式如何偏离，占位符字符串不被写入 DB L1 JSON。

## 额外发现

**`LLM_THINK` 默认值 `None` → Qwen3.5 模型 response='' bug。** 不传 `think: false` 时，Ollama 的 Qwen3.5 parser 把内容放 `thinking` 字段，`response` 为空。CA `_call_llm_for_l1` 读 `data["response"]` 拿不到内容→走 fallback→写"本轮无新内容"。修复：`config.py` 改 `LLM_THINK: ClassVar[Optional[bool]] = False`。
