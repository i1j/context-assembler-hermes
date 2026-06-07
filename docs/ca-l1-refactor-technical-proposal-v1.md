# L1 摘要系统重构 — 技术方案初稿 (v1)

**状态**: 🟡 草案，未完成多视角评审  
**存档时间**: 2026-06-15  
**对应白皮书**: `docs/ca-l1-whitepaper-v1.2-gm.md`  
**基线版本**: v4.6.0 (commit `1afc1df`)  
**说明**: 本文档是技术方案第一次草稿，在完成需求开发阶段后产出。因 devtest-workflow 流程不可用（见 `ca-devtest-workflow-selfcheck-report.md`），尚未进行多视角评审和测试方案交叉评审。暂存档，待新话题中流程问题解决后继续。

---

## 变更概要

全面吸收白皮书 v1.2 GM 设计，保留现有 DB 存储格式（5 类英文 key），通过 `ooda_parser.TITLE_ALIASES` 扩展实现中文→英文映射。

---

## 变更清单

### 1. `ca/config.py`

| 变更 | 值 | 说明 |
|------|----|------|
| 新增 `L1_TEMPERATURE` | `os.getenv("CA_L1_TEMPERATURE", "0.3")` | ClassVar[float]，默认 0.3（保守值） |
| 新增 `L1_MAX_TOKENS` | `os.getenv("CA_L1_MAX_TOKENS", "800")` | ClassVar[int]，白皮书推荐 800 |
| validate 新增 | `pos_float("L1_TEMPERATURE", min_v=0.01)` + `pos_int("L1_MAX_TOKENS", min_v=100)` | |
| reload 新增 | `cls.L1_TEMPERATURE = float(...)` + `cls.L1_MAX_TOKENS = int(...)` | |

### 2. `ca/prompts.py`

| 项 | 旧值 | 新值 |
|----|------|------|
| 人设 | "你是一个会议纪要摘要助手" | "你是一个研发对话意图分析器" |
| 输出格式 | 纯文本 5 类（核心摘要/资源与观察/事实与约束/决策与结论/后续行动） | Markdown+XML 4 类（现象与问题/背景与约束/决策与共识/后续行动 + `<core_change>`） |
| `<example>` | 无 | 有，展示完整输出范例 |
| 规则后置 | 规则在前 | 规则在 `<example>` 之后 |
| "自我检查"提示 | 有 | 移除，防废话污染 |
| 结尾 | "请生成本轮增量摘要：" | "请直接输出分析结果，不要包含任何解释或检查过程：" |

### 3. `ca/post_process.py` — 新增

| 新增项 | 类型 | 说明 |
|--------|------|------|
| `MEANINGLESS_CORE` | `Set[str]` | `{"无","暂无","无有效增量","无新增","none","null",""}` |
| `WHITESPACE_PATTERN` | `re.Pattern` | `re.compile(r'\s+')`，预编译免疫转义丢失 |
| `CORE_CHANGE_PATTERN` | `re.Pattern` | `re.compile(r'<core_change>(.*?)(?:</core_change>|\Z)', re.DOTALL)` |
| `OODA_PATTERNS` | `Dict[str, re.Pattern]` | 4 条正则（现象与问题/背景与约束/决策与共识/后续行动） |
| `parse_v1_markdown_xml()` | 函数 | 主入口：截断尾随噪音 → 提取 core_change → OODA → 语义短路 → 返回 `(dict, l0_text)` |
| `_safe_truncate()` | 函数 | 智能截断 100 字符上限，`.` 优先级降低防 `config.py` 被腰斩 |
| `_build_empty_result()` | 函数 | 空结果工厂 |

保留不动：`robust_json_parse` + `clean_increment`（adapter 底层用）

### 4. `ca/ooda_parser.py` — TITLE_ALIASES 扩展

```python
TITLE_ALIASES = {
    "core_change": ["核心摘要", "核心更新", "本轮摘要", "关键变化", "核心变更", "核心结论"],
    "new_materials": ["资源与观察", "资源观察", "新增资源", "文件变更", "物料清单", "代码变更",
                       "现象与问题", "现象问题"],                                              # ← 新增
    "objective_facts": ["事实与约束", "客观事实", "发现与约束", "事实发现", "环境事实", "报错与参数",
                         "背景与约束", "背景约束"],                                            # ← 新增
    "consensus": ["决策与结论", "共识与决策", "达成共识", "决定与结论", "决策结论",
                   "决策与共识"],                                                              # ← 新增
    "todo": ["后续行动", "待办事项", "下一步", "行动项", "待办"],
}
```

### 5. `ca/__init__.py` — `_call_llm_for_l1`

| 变更 | 旧 | 新 |
|------|----|----|
| 温度 | `"temperature": 0.3`（硬编码） | `"temperature": Config.L1_TEMPERATURE` |
| max_tokens | 无独立参数（用 `num_predict: LLM_NUM_PREDICT`） | 新增 `"num_predict": Config.L1_MAX_TOKENS` |
| 返回签名 | `str` | `Tuple[str, str]` — `(response_text, finish_reason)` |
| 截断检测 | 无 | 双重校验：`finish_reason == 'length'` + `not output.strip().endswith('</core_change>')` |
| 截断行为 | 静默传入解析器 | `raise L1TruncatedException` |
| prompt 组装 | `json.dumps(prev_l1)` 直传 | `format_previous_summary_for_prompt(prev_l1)` |

### 6. `ca/store.py` — 新增

| 新增函数 | 签名 | 说明 |
|---------|------|------|
| `format_previous_summary_for_prompt()` | `(l1_text_from_db: str) -> str` | 入口判断路由：None/"无"→"无"；JSON→转 Markdown；纯文本原样返回 |
| `_legacy_json_to_v1_markdown()` | `(data: dict) -> str` | 旧 5 类 field 映射到新 4 类 Markdown 格式 + `<core_change>` |
| `_current_json_to_v1_markdown()` | `(data: dict) -> str` | 新 5 类 field（key 与旧相同）→ 同一格式 |

### 7. `ca/lstage.py` — `_backfill_dialogue`

同步 `_call_llm_for_l1` 的变更：温度 → `Config.L1_TEMPERATURE`、max_tokens → `Config.L1_MAX_TOKENS`、截断检测。

### 8. 新增异常类

```python
# ca/exceptions.py 或 ca/__init__.py 模块级
class L1TruncatedException(Exception):
    """LLM L1 摘要输出被截断时抛出，触发 L-stage fallback"""
```

---

## 数据流总图

```
旧 DB JSON (5类英key)               LLM 输出 (4类中文+XML)
    │                                      │
    ▼                                      ▼
format_previous_summary_for_prompt()    parse_v1_markdown_xml() [可选独立入口]
    │                                      │
    ├─ None/"无" → "无"                    ├─ 物理截断尾随噪音
    ├─ JSON → _legacy_json_to_v1_markdown  ├─ 提取 <core_change>
    │        → 4类 Markdown                ├─ 提取 OODA 4 类列表
    └─ 纯文本 → 原样返回                   └─ 语义短路 → (l1_dict, l0_text)
           │                                      │
           ▼                                      ▼
    ┌──────────────────────────────────────────────┘
    │  主流程仍走 ooda_parser.parse()
    │  （TITLE_ALIASES 扩展后兼容新旧两种格式）
    ▼
robust_json_parse() → clean_increment() → DB (5类英key JSON)
```

---

## 测试计划（初稿）

### 新测试文件

| 文件 | 范围 | 用例数 |
|------|------|--------|
| `tests/test_parse_v1.py` | `parse_v1_markdown_xml` + `_safe_truncate` + `MEANINGLESS_CORE` | ~15 |
| `tests/test_store_adapter.py` | `format_previous_summary_for_prompt` + `_legacy_json_to_v1_markdown` | ~8 |

### 追测

| 文件 | 范围 | 用例数 |
|------|------|--------|
| `tests/test_c.py` | 截断检测 + `L1TruncatedException` | ~3 |
| `tests/test_lifecycle.py` | `_backfill_dialogue` 截断路径 | ~2 |

### 回归

全量测试 `pytest tests/ -v --ignore=tests/test_system.py`

---

## 未决事项（待交叉评审）

1. `parse_v1_markdown_xml` 与 `ooda_parser` 职责分开还是合并？
2. `_legacy_json_to_v1_markdown` 与 `_current_json_to_v1_markdown` 是否合并为一个函数？
3. Metrics 接入（ca.l1.truncated_fallback 等 4 个指标）是否本期做？
4. `endswith` 截断检测是否会导致 L-stage 降级率过高？
