---
title: Fct 格式演变
slug: fct-format-evolution
category: architecture
version_introduced: v5.2
status: 已实装
decisions: ["fct-changes-format"]
depends_on: ["storage-model", "f-stage-async-summary"]
updated: 2026-06-18
source_files: ["ca/post_process.py", "ca/prompts.py"]
---

## 问题

Fct（单轮摘要）需要结构化表示当前轮相对于历史上下文的状态变更。旧格式使用 `<stage_tag>/<core_change>` 单对字符串，存在以下问题：
- 单轮可能有多个变更事项，无法表达多对
- 解析时正则匹配可能误匹配嵌套标签
- 零对输出（无变更时）无法与截断区分

## 决策

### 备选方案

1. **纯 JSON 块** — 非列表难以扩展，解析复杂
2. **固定长度截断** — 破坏语义完整性
3. **放弃旧数据兼容** — 向后兼容性要求无法满足
4. **changes 列表格式 + PAIR_PATTERN 正则 + 旧数据回退（选定）**

### 选定方案

**新格式**（`changes` 列表）：

```json
[
  {"stage_tag": "已实施", "core_change": "修复用户登录超时问题"},
  {"stage_tag": "评估中",  "core_change": "重构数据库连接池"},
  {"stage_tag": "已决",    "core_change": "API 返回格式统一"}
]
```

**解析**（`ca/post_process.py` 的 `PAIR_PATTERN`）：

```python
PAIR_PATTERN = re.compile(
    r'<stage_tag>\s*(.*?)\s*</stage_tag>\s*'
    r'<core_change>\s*(.*?)\s*</core_change>',
    re.DOTALL
)
```

**VALID_STATES**（有效 `stage_tag` 值集合）：`已实施`, `评估中`, `已决`, `已验证`, `已回退`, `标记中`。

**零对输出截断检测**：当 LLM 输出的 changes 列表为空时，特殊标记"本对话轮未发现变更"。

**旧数据兼容**：
- `clean_increment`：将旧 `<stage_tag>/<core_change>` 单对格式转换为 changes 列表
- `_extract_l0` 回退：如果解析失败，保留原始 Elm

## 数据验证

```sql
-- 查看 Fct 格式分布
SELECT turn,
       CASE 
           WHEN Fct LIKE '%"changes"%' THEN 'changes列表格式'
           WHEN Fct LIKE '%<stage_tag>%' THEN '旧标签格式'
           WHEN Fct = '' THEN '空'
           ELSE '未知'
       END AS fct_format
FROM turn_stream
WHERE role='assistant' AND seq=0
ORDER BY turn;
```

## 优点

- 支持多变更事项表达
- 正则解析简单可靠
- 旧数据向前兼容
- 零对输出有明确检测手段

## 约束 / 已知问题

- LLM 可能输出不符合 `VALID_STATES` 的 stage_tag，需用 fallback 兜底
- 极端情况下 PAIR_PATTERN 可能误匹配嵌套标签（已通过非贪婪 `.*?` 缓解）
- 旧数据回退路径 `_extract_l0` 可能丢失颗粒度

## 关联文档

| 文档 | 内容 |
|------|------|
| [决策: 工具轮规则引擎](../decisions/03-tool-summarizer-rules.md) | 工具 Fct 结构化摘要规则（含 execute_code 等 Handler） |
| E-stage 写入协议 (02) | Fct 写入时机与格式约束 |
