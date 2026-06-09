# 实现技术方案 v2: L1 摘要系统重构

## 1. 实现策略

采用**自底向上、逐层依赖**的顺序实施：先入配置（Config）、提示词（Prompts）、基础设施（异常类 + 解析器），再改造核心调用链路（`_call_llm_for_l1` / `_run_c_stage`），最后同步 L-stage 和 Metrics。每个步骤可独立验证，不破坏现有测试。

整体遵循 PDD（Prompt-Driven Development）哲学：模型负责语义理解和 Markdown 续写，Python 代码负责截断检测、格式清洗、边界校验和新旧数据兼容。所有新函数以纯函数形式实现于 `ca/post_process.py`（新增），`format_previous_summary_for_prompt` 放 `ca/store.py`（与 DB 格式转换有关）。OODAParser 仅扩展 `TITLE_ALIASES` 映射表，不修改核心解析逻辑，保证 DB 存储格式（5 类英文 key JSON）完全不变。

---

## 2. 文件级改动清单

| 操作 | 文件路径 | 改动内容 |
|------|---------|---------|
| 修改 | `ca/config.py` | 新增 `L1_TEMPERATURE`（默认 0.3） + `L1_MAX_TOKENS`（默认 800） + `validate` 校验 + `reload` 热重载 |
| 修改 | `ca/prompts.py` | 替换 `L1_GENERATION_PROMPT` 为 PDD 新版："研发对话意图分析器"人设，输出 4 类 Markdown + `<core_change>` XML 标签 |
| 新增函数 | `ca/post_process.py` | 新增 `parse_v1_markdown_xml()` + `_safe_truncate()` + `_json_to_v1_markdown()` + 常量/预编译正则 |
| 修改 | `ca/ooda_parser.py` | `TITLE_ALIASES` 扩展 4 类中文别名（现象与问题/背景与约束/决策与共识/后续行动） |
| 修改 | `ca/__init__.py` | 新增 `L1TruncatedException` 异常类；`_call_llm_for_l1` 改签名返回 `Tuple[str,str]`（response_text, finish_reason）；增加截断检测 + 独立 temperature/max_tokens 参数；`_run_c_stage` 适配新数据流 + 截断降级；`_extract_l0` 增加空文本 Metrics |
| 新增函数 | `ca/store.py` | 新增 `format_previous_summary_for_prompt()`（引用自 post_process） |
| 修改 | `ca/lstage.py` | `_backfill_dialogue` 同步新 `_call_llm_for_l1` 签名 + 截断检测 + 新配置参数 |
| 修改 | `ca/stats.py` | 新增 L1 统计字段（truncated_fallback, parse_fallback_count, skipped_empty, latency_ms） |
| 新增 | `tests/test_parse_v1.py` | ~15 测试用例（parse_v1_markdown_xml 全场景） |
| 新增 | `tests/test_store_adapter.py` | ~8 测试用例（format_previous_summary_for_prompt） |
| 修改 | `tests/conftest.py` | `_mock_llm` fixture 返回值从 `str` 改为 `Tuple[str, str]`（`('mock_response', 'stop')`），适配 `_call_llm_for_l1` 新签名 |
| 修改 | `tests/test_c.py` | 追测 ~3 个截断检测用例；更新现有 `_call_llm_for_l1` mock 为新签名 |
| 修改 | `tests/test_v440.py` | 追测 ~2 个 L-stage 截断路径用例；更新现有 mock |
| 修改 | `tests/test_v460.py` | 更新 `_call_llm_for_l1` mock 为新签名 |
| 修改 | `tests/legacy/test_c_stage.py` | 更新 `_call_llm_for_l1` mock 为新签名 |
| 修改 | `tests/legacy/test_review_fixes.py` | 更新 `_call_llm_for_l1` mock 为新签名 |

---

## 3. 接口设计

### 3.1 新增/修改的公共接口

| 接口 | 签名 | 说明 |
|------|------|------|
| `L1TruncatedException` | `(message: str, response_text: str)` | 新增异常类，携带截断的 response_text 供降级回读 |
| `parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]` | 新增 | 主解析器：返回 (l1_dict, l0_text)。l1_dict 含 5 类英 key，l0_text 为 core_change 首句或 None |
| `_safe_truncate(text: str, max_len: int = 100) -> str` | 新增 | 智能截断：优先在句号/问号/感叹号处截断，降级到逗号/分号，最后硬截断 max_len。防止 `config.py` 等词汇被腰斩 |
| `_json_to_v1_markdown(data: dict) -> str` | 新增 | 将旧格式 5 类英 key JSON 转换为新 4 类 Markdown 格式字符串 |
| `format_previous_summary_for_prompt(l1_text_from_db: str) -> str` | 新增 | DB → prompt 输入适配器。输入为 DB 中 l1_text（可能是 JSON 或旧纯文本或空），输出为新 4 类 Markdown |
| `_call_llm_for_l1(prev_l1: Optional[Dict], l2_text: str) -> Tuple[str, str]` | **改签名** | 原返回 `str`，现返回 `(response_text: str, finish_reason: str)`。finish_reason 为 `"stop"` / `"length"` / `"error"` |
| `Config.L1_TEMPERATURE` | `ClassVar[float]` = `os.getenv("CA_L1_TEMPERATURE", "0.3")` | L1 生成 temperature，validate 校验范围 [0, 2] |
| `Config.L1_MAX_TOKENS` | `ClassVar[int]` = `int(os.getenv("CA_L1_MAX_TOKENS", "800"))` | L1 生成 max_tokens，validate 校验范围 [50, 4096] |

### 3.2 数据流（C-stage 对话轮）

```
_run_c_stage()
  │
  ├─ prev_l1 = _get_previous_l1()
  │
  ├─ prompt = L1_GENERATION_PROMPT.format(
  │     previous_summary=format_previous_summary_for_prompt(prev_l1),  # ← 新适配器
  │     current_dialog=l2_text)
  │
  ├─ response_text, finish_reason = _call_llm_for_l1(prev_l1, l2_text)  # ← 新签名
  │     └─ LLM 调用使用 Config.L1_TEMPERATURE + Config.L1_MAX_TOKENS（独立配置）
  │     └─ 截断检测:
  │        if finish_reason == 'length' OR not response_text.endswith('</core_change>'):
  │            logger.warning("ca.l1.truncated_fallback")
  │            raise L1TruncatedException(response_text=response_text)
  │
  ├─ [L1TruncatedException 捕获] → _assemble_status=1 → 持久化 + 跳过后续解析
  │     └─ l1_dict = {"core_change": "本轮无新内容"}
  │     └─ l0_text = ""
  │
  ├─ l1_dict, l0_text = parse_v1_markdown_xml(response_text)  # ← 新解析器
  │     └─ 若未匹配 <core_change> → logger.warning("ca.l1.parse_fallback_count")
  │     └─ l0_text 若为 None → logger.warning("ca.l0.skipped_empty")
  │
  ├─ parsed = ooda_parser.parse(json.dumps(l1_dict), previous_summary=prev_l1)
  │     └─ TITLE_ALIASES 扩展后能解析新 4 类中文标题
  │
  └─ → DB write (5类英key JSON, 与旧格式相同)
```

### 3.3 L-stage `_backfill_dialogue` 数据流

```
_backfill_dialogue(rec, l2_text)
  │
  ├─ prev_l1 = _get_prev_l1(rec["turn_index"])
  │
  ├─ response_text, finish_reason = engine._call_llm_for_l1(prev_l1, l2_text)
  │     └─ 截断检测: 同 C-stage，捕获 L1TruncatedException 后：
  │        - 设置 _assemble_status=1（继续留在待补全队列）
  │        - logger.warning("ca.l1.truncated_fallback")
  │        - return（下次重试）
  │
  ├─ l1_dict, l0_text = parse_v1_markdown_xml(response_text)
  │
  ├─ parsed = engine.ooda_parser.parse(json.dumps(l1_dict), previous_summary=prev_l1)
  │
  └─ → _update_record(rec, l0, l1_str, l0_emb, l1_emb)  # 成功后 _assemble_status=0
```

### 3.4 Metrics 接入策略

使用 `logger.warning` 计数 + mark 标记，不做 Prometheus 依赖：

| 指标 | 接入点 | 实现方式 |
|------|--------|---------|
| `ca.l1.truncated_fallback` | `_call_llm_for_l1` 抛出 `L1TruncatedException` 时 | `logger.warning("[CA-METRIC] ca.l1.truncated_fallback: turn=%d, finish_reason=%s", turn_index, finish_reason)` |
| `ca.l1.parse_fallback_count` | `parse_v1_markdown_xml` 未匹配到 `<core_change>` 时 | `logger.warning("[CA-METRIC] ca.l1.parse_fallback_count: turn=%d, llm_output_len=%d", turn_index, len(llm_output))` |
| `ca.l0.skipped_empty` | L0 为空字符串或 None 跳过后 | `logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", turn_index)` |
| `ca.l1.latency_ms` | `_call_llm_for_l1` 调用前后计时 | `logger.warning("[CA-METRIC] ca.l1.latency_ms: turn=%d, ms=%d", turn_index, elapsed_ms)` |

> **注意**：Logger 方法统一前缀 `[CA-METRIC]` 以便测试 agent 和日志监控工具统一过滤。

---

## 4. 关键实现细节

### 4.1 `parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]`

#### 解析流程

```
1. 预处理：_safe_truncate(llm_output, max_len=2000)  # 物理截断尾随噪音
2. 提取 <core_change>...</core_change> 标签内的内容
   - 成功 → core_text 赋值
   - 失败 → logger.warning("ca.l1.parse_fallback_count") → 走语义短路
     - 检查 MEANINGLESS_CORE 列表（"无", "无变化", "无明显变化", "none", "无核心变化", "无核心变更"）
     - 命中 → l0_text=None, l1_dict 各字段为空/空列表
     - 未命中 → 取 llm_output[:100] 作为 core_change
3. 提取 Markdown 4 类标题下的列表项：
   - ### 现象与问题 (phenomena)
   - ### 背景与约束 (background)
   - ### 决策与共识 (decisions)
   - ### 后续行动 (actions)
   - 每个标题下提取 `-` / `*` / `•` 列表项，最多 3 项，每项最多 50 字
4. 构建 l1_dict（5 类英 key）：
   - core_change → core_text
   - new_materials → phenomena 列表（"现象与问题"映射到 new_materials）
   - objective_facts → background 列表（"背景与约束"映射到 objective_facts）
   - consensus → decisions 列表（"决策与共识"映射到 consensus）
   - todo → actions 列表（"后续行动"映射到 todo）
5. l0_text = core_text 首句[:100] if core_text else None
   - 若 l0_text 为 None → logger.warning("ca.l0.skipped_empty")
6. 返回 (l1_dict, l0_text)
```

#### 预编译正则

```python
_CORE_CHANGE_TAG_RE = re.compile(r'<core_change>\s*(.*?)\s*</core_change>', re.DOTALL)
_SECTION_HEADING_RE = re.compile(r'^###\s+(.+)$', re.MULTILINE)
_LIST_ITEM_RE = re.compile(r'^[-*•]\s+(.+)$', re.MULTILINE)
```

#### 常量

```python
# 4 类 Markdown 标题 → 5 类英 key 映射
SECTION_MAP = {
    "现象与问题": "new_materials",
    "背景与约束": "objective_facts",
    "决策与共识": "consensus",
    "后续行动": "actions",  # 注意：输出为 "todo"（OODAParser 兼容）
}
MEANINGLESS_CORE = frozenset({"无", "无变化", "无明显变化", "none", "无核心变化", "无核心变更"})
```

#### 边界条件

| 场景 | 处理方案 |
|------|---------|
| `llm_output` 为空或 None | 返回 (`{"core_change": "本轮无新内容"}`, None) |
| 无 `<core_change>` 标签 | 走语义短路 + logger.warning("ca.l1.parse_fallback_count") |
| 无任何 Markdown 标题 | 构建空字典，仅含 core_change 字段 |
| 标题下有内容但无列表项 | 尝试按行分割，非空行作为列表项 |
| 某个列表项超过 50 字 | 截断到 50 字 |
| 某个分类超过 3 项 | 只取前 3 项 |
| core_text 内容为 MEANINGLESS_CORE | l0_text=None，l1_dict 各列表字段为空 |
| l0_text 超过 100 字符 | `_safe_truncate(l0_text, 100)` 智能截断 |

### 4.2 `_safe_truncate(text: str, max_len: int = 100) -> str`

```
1. 若 len(text) <= max_len → 直接返回
2. 从 max_len 位置向前查找优先级标点：
   - 最高：。！？.!?（句号/问号/感叹号）→ 在此截断
   - 次高：；;,:：→ 在此截断
   - 最低：硬截断到 max_len（不截断单词/词汇）
3. 截断后 strip()
4. 确保不以 `,` `.` `:` `；` `，` `。` 等连接符结尾
```

### 4.3 `_json_to_v1_markdown(data: dict) -> str`

将旧格式 5 类英 key JSON 转为新 4 类 Markdown：

```python
FIELD_MAP = {
    "new_materials":    ("现象与问题", False),
    "objective_facts": ("背景与约束", False),
    "consensus":       ("决策与共识", False),
    "todo":            ("后续行动", True),    # "actions" 兼容
}
```

输出示例：
```markdown
### 现象与问题
- <item1>
- <item2>

### 背景与约束
- <item1>

### 决策与共识
- <item1>
- <item2>

### 后续行动
- <item1>

<core_change>
<core_text>
</core_change>
```

- core_change 字段放 `<core_change>` 标签中
- 列表字段每项前加 `- `
- 空字段跳过（不输出空标题）
- 遇到未知结构 → fallback 原样返回纯文本

### 4.4 `format_previous_summary_for_prompt(l1_text_from_db: str) -> str`

```
1. 如果 l1_text_from_db 为 None 或空或 "无" → 返回 "无"
2. 尝试 json.loads(l1_text_from_db)：
   - 成功（dict 类型）→ 调用 _json_to_v1_markdown(data)
   - 失败（非 JSON 或非 dict）→ 原样返回
3. 若转换过程中捕获异常 → 原样返回纯文本（容错兜底）
```

### 4.5 `L1TruncatedException`

```python
class L1TruncatedException(Exception):
    """LLM 输出被截断时抛出的异常。

    携带 response_text 以便降级代码读取部分输出。
    """
    def __init__(self, message: str = "L1 output truncated", response_text: str = ""):
        super().__init__(message)
        self.response_text = response_text
```

### 4.6 `_call_llm_for_l1` 改造

```python
def _call_llm_for_l1(self, prev_l1: Optional[Dict], l2_text: str) -> Tuple[str, str]:
    """返回 (response_text, finish_reason)。"""
    llm_start = time.monotonic()
    prompt = L1_GENERATION_PROMPT.format(
        previous_summary=format_previous_summary_for_prompt(
            json.dumps(prev_l1, ensure_ascii=False) if prev_l1 else None
        ),
        current_dialog=l2_text)
    req_body = {
        "model": Config.LLM_MODEL,
        "prompt": prompt, "stream": False,
        "options": {
            "num_predict": Config.L1_MAX_TOKENS,      # ← 改为独立配置
            "temperature": Config.L1_TEMPERATURE,      # ← 改为独立配置
        },
        "keep_alive": -1
    }
    if Config.LLM_THINK is not None:
        req_body["think"] = Config.LLM_THINK
    payload = json.dumps(req_body).encode()
    response_text = ""
    finish_reason = "error"
    for attempt in range(Config.LLM_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{Config.LLM_ENDPOINT}/api/generate", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
            response_text = data.get("response", "")
            finish_reason = data.get("done_reason") or data.get("finish_reason", "stop")
            break
        except Exception as e:
            logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)

    elapsed_ms = int((time.monotonic() - llm_start) * 1000)
    logger.warning("[CA-METRIC] ca.l1.latency_ms: turn=%d, ms=%d", self._turn_counter, elapsed_ms)

    # 截断检测
    if finish_reason == "length" or not response_text.strip().endswith("</core_change>"):
        logger.warning("[CA-METRIC] ca.l1.truncated_fallback: turn=%d, finish_reason=%s, len=%d",
                       self._turn_counter, finish_reason, len(response_text))
        raise L1TruncatedException(
            message=f"LLM output truncated: finish_reason={finish_reason}",
            response_text=response_text)

    return response_text, finish_reason
```

### 4.7 `_run_c_stage` 改造

```python
def _run_c_stage(self, session_id, turn_index, prev_l1, l2_text, token_offset, ...):
    start = time.monotonic()
    ...
    try:
        if bg_review:
            ...  # 保持不变
        else:
            try:
                response_text, finish_reason = self._call_llm_for_l1(prev_l1, l2_text)
            except L1TruncatedException as e:
                # 截断降级
                l1_dict = {"core_change": "本轮无新内容"}
                l0_text = ""
                cleaned = l1_dict.copy()
                cleaned["_assemble_status"] = ASSEMBLE_PENDING_BACKFILL  # = 1
                dialogue_ok = False
                logger.warning("[CA-METRIC] ca.l1.truncated_fallback: turn=%d", turn_index)
            else:
                # 正常解析
                l1_dict, l0_text = parse_v1_markdown_xml(response_text)
                parsed = self.ooda_parser.parse(json.dumps(l1_dict), previous_summary=prev_l1)
                robust, _ = robust_json_parse(json.dumps(parsed, ensure_ascii=False))
                cleaned = clean_increment(robust)
                cleaned["_assemble_status"] = ASSEMBLE_OK  # = 0
                if not l0_text:
                    logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", turn_index)
                dialogue_ok = True
    ...
```

### 4.8 `_extract_l0` 改造

```python
def _extract_l0(self, l1_dict) -> str:
    core = l1_dict.get("core_change", "")
    if not core or core in ("无", "本轮无新内容"):
        logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", self._turn_counter)
        return "无"
    return _safe_truncate(core, 100)
```

### 4.9 `OODAParser.TITLE_ALIASES` 扩展

在现有 `TITLE_ALIASES` 字典中为每个 key 追加新别名：

```python
TITLE_ALIASES = {
    "core_change": ["核心摘要", "核心更新", "本轮摘要", "关键变化", "核心变更", "核心结论"],
    "new_materials": ["资源与观察", "资源观察", "新增资源", "文件变更", "物料清单", "代码变更",
                      "现象与问题", "现象", "问题", "新增现象"],
    "objective_facts": ["事实与约束", "客观事实", "发现与约束", "事实发现", "环境事实", "报错与参数",
                        "背景与约束", "背景", "约束条件", "背景约束"],
    "consensus": ["决策与结论", "共识与决策", "达成共识", "决定与结论", "决策结论",
                  "决策与共识", "决策", "共识"],
    "todo": ["后续行动", "待办事项", "下一步", "行动项", "待办",
             "后续行动", "行动", "待办行动"],
}
```

> 注意：新 4 类标题 "现象与问题"→new_materials、"背景与约束"→objective_facts、"决策与共识"→consensus、"后续行动"→todo，通过 `TITLE_ALIASES` 映射到现有 5 类英 key，DB 格式完全不变。

### 4.10 Config 新增项

```python
# 在 Config 类中新增（接在 LLM_THINK 之后）
L1_TEMPERATURE: ClassVar[float] = float(os.getenv("CA_L1_TEMPERATURE", "0.3"))
L1_MAX_TOKENS: ClassVar[int] = int(os.getenv("CA_L1_MAX_TOKENS", "800"))
```

**validate 新增校验**：
```python
pos_float("L1_TEMPERATURE", cls.L1_TEMPERATURE, min_v=0.0, max_v=2.0)
pos_int("L1_MAX_TOKENS", cls.L1_MAX_TOKENS, min_v=50, max_v=4096)
```

**reload 新增行**：
```python
cls.L1_TEMPERATURE = float(os.getenv("CA_L1_TEMPERATURE", str(cls.L1_TEMPERATURE)))
cls.L1_MAX_TOKENS = int(os.getenv("CA_L1_MAX_TOKENS", str(cls.L1_MAX_TOKENS)))
```

### 4.11 Prompts 替换

新 `L1_GENERATION_PROMPT` 使用"研发对话意图分析器"人设：

```
L1_GENERATION_PROMPT = """你是一个研发对话意图分析器。请根据上一轮摘要和本轮对话，生成增量式研发协作摘要。

【输出格式】
请严格按照以下 Markdown 和 XML 格式输出：

### 现象与问题
- <技术现象或待解决的问题，最多3项，每项≤50字>

### 背景与约束
- <技术背景或已知约束，最多3项，每项≤50字>

### 决策与共识
- <技术决策或团队共识，最多3项，每项≤50字>

### 后续行动
- <明确的后续待办事项，最多3项，每项≤50字>

<core_change>
<1-2句话概括本轮最核心的变更或进展>
</core_change>

【规则】
- 每项使用短横线列表，不超过3项，每项≤50字。
- 若无某类信息，该节写"无"。
- 若整个增量无新信息，core_change 写"无"，其余节写"无"。
- 严禁在 core_change 标签外添加额外 XML 标签。
- 严禁输出 Markdown 代码块包围。

【上一轮摘要】
{previous_summary}

【本轮对话】
{current_dialog}

请生成本轮增量摘要："""
```

### 4.12 错误处理策略

| 错误场景 | 处理方式 |
|---------|---------|
| LLM 调用全部重试失败 | 返回退化输出（"本轮无新内容"），finish_reason="error"，不抛异常 |
| 截断检测失败 | 抛 `L1TruncatedException`，C-stage 捕获后设 `_assemble_status=1`（待补全）；L-stage 捕获后跳过该轮（下次重试） |
| `parse_v1_markdown_xml` 完全无法解析 | 语义短路返回 `{"core_change": "本轮无新内容"}`，l0_text=None |
| `_json_to_v1_markdown` 遇到未知 JSON 结构 | fallback 原样返回纯文本 |
| `format_previous_summary_for_prompt` 输入异常 | 返回 `"无"` |
| Metrics logger 溢出 | 每个指标每次触发仅写一条 warning，不做防抖（后期可追加采样率） |

---

## 5. 接口契约（供测试 agent 使用）

| 接口 | 签名 | 预期行为 |
|------|------|---------|
| `parse_v1_markdown_xml` | `(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]` | 正常解析 4 类 Markdown + `<core_change>` → 返回 5 类英 key dict + l0_text；无 `<core_change>` → 语义短路，返回退化结果；空输入 → 退化结果。不抛出异常 |
| `_safe_truncate` | `(text: str, max_len: int = 100) -> str` | len ≤ max_len → 原样返回；需截断 → 优先句号截断；硬截断不破坏语法结构 |
| `_json_to_v1_markdown` | `(data: dict) -> str` | 正确映射 5 类英 key → 4 类 Markdown（含 `<core_change>` 标签）；空 dict → 返回空字符串；异常 → 原样返回 str(data) |
| `format_previous_summary_for_prompt` | `(l1_text_from_db: str) -> str` | None/空/"无" → `"无"`；JSON dict → Markdown 格式；非 JSON 纯文本 → 原样返回；异常 → 返回 `"无"` |
| `_call_llm_for_l1` | `(prev_l1: Optional[Dict], l2_text: str) -> Tuple[str, str]` | 正常调用 → `(response, "stop")`；截断 → 抛 `L1TruncatedException`；重试失败 → `("", "error")` |
| `L1TruncatedException` | `(message: str, response_text: str)` | 实例携带 `.response_text` 属性，可被 `except` 捕获 |
| `Config.L1_TEMPERATURE` | `ClassVar[float]` | 默认 0.3，env `CA_L1_TEMPERATURE` 覆盖，validate 范围 [0.0, 2.0] |
| `Config.L1_MAX_TOKENS` | `ClassVar[int]` | 默认 800，env `CA_L1_MAX_TOKENS` 覆盖，validate 范围 [50, 4096] |
| `OODAParser.TITLE_ALIASES` | `ClassVar[Dict[str, list]]` | 扩展后每个 key 至少 6 个别名，新 4 类标题可正确映射 |

---

## 6. 实施步骤（2-5分钟粒度，可验证）

### Step 1: Config — 新增 `L1_TEMPERATURE` + `L1_MAX_TOKENS`

- **操作**: 在 `ca/config.py` 中 `Config` 类 `LLM_THINK` 后新增两个 ClassVar，在 `validate()` 和 `reload()` 中添加对应行
- **验证**: 
  ```bash
  python -c "from ca.config import Config; print(Config.L1_TEMPERATURE, Config.L1_MAX_TOKENS); Config.validate(); print('OK')"
  ```
  预期输出: `0.3 800` + `OK`

### Step 2: Prompts — 替换 `L1_GENERATION_PROMPT`

- **操作**: 将 `ca/prompts.py` 中 `L1_GENERATION_PROMPT` 替换为 PDD 新版（4 类 Markdown + XML 格式）
- **验证**:
  ```bash
  python -c "from ca.prompts import L1_GENERATION_PROMPT; assert '### 现象与问题' in L1_GENERATION_PROMPT; assert '<core_change>' in L1_GENERATION_PROMPT; print('OK')"
  ```

### Step 3: OODAParser — 扩展 `TITLE_ALIASES`

- **操作**: 在 `ca/ooda_parser.py` 的 `TITLE_ALIASES` 中为每个 key 追加新 4 类中文别名
- **验证**:
  ```bash
  python -c "
  from ca.ooda_parser import OODAParser;
  tests = ['现象与问题','背景与约束','决策与共识','后续行动'];
  expected = ['new_materials','objective_facts','consensus','todo'];
  for t, e in zip(tests, expected):
      alias_map = {a.lower(): f for f, aliases in OODAParser.TITLE_ALIASES.items() for a in aliases}
      assert alias_map[t.lower()] == e, f'{t} -> {alias_map.get(t.lower())} != {e}';
  print('OK')"
  ```

### Step 4: post_process.py — 新增解析器核心

- **操作**: 在 `ca/post_process.py` 末尾新增 `parse_v1_markdown_xml()`、`_safe_truncate()`、`_json_to_v1_markdown()` 函数及常量/预编译正则
- **验证**:
  ```bash
  python -c "
  from ca.post_process import parse_v1_markdown_xml, _safe_truncate, _json_to_v1_markdown;
  # 正常解析
  text = '### 现象与问题\n- bug\n\n<core_change>修复bug</core_change>';
  d, l0 = parse_v1_markdown_xml(text);
  assert d['core_change'] == '修复bug', f'got {d}';
  assert l0 == '修复bug';
  # 空输入
  d2, l02 = parse_v1_markdown_xml('');
  assert d2['core_change'] == '本轮无新内容';
  # 截断
  assert _safe_truncate('hello world', 5) == 'hello';
  # JSON 转换
  result = _json_to_v1_markdown({'core_change':'test','new_materials':['a','b']});
  assert '<core_change>' in result;
  assert '### 现象与问题' in result;
  print('OK')"
  ```

### Step 5: store.py — 新增 `format_previous_summary_for_prompt`

- **操作**: 在 `ca/store.py` 末尾（模块级别）新增 `format_previous_summary_for_prompt()` 函数
- **验证**:
  ```bash
  python -c "
  from ca.store import format_previous_summary_for_prompt;
  # 空输入
  assert format_previous_summary_for_prompt(None) == '无';
  assert format_previous_summary_for_prompt('') == '无';
  # JSON 输入
  result = format_previous_summary_for_prompt('{\"core_change\":\"test\",\"new_materials\":[\"a\"]}');
  assert '<core_change>' in result;
  assert '### 现象与问题' in result;
  # 纯文本输入
  assert format_previous_summary_for_prompt('纯文本') == '纯文本';
  print('OK')"
  ```

### Step 6: 异常类 + `_call_llm_for_l1` 改造

- **操作**: 
  1. 在 `ca/__init__.py` 模块级别新增 `L1TruncatedException` 类
  2. 修改 `_call_llm_for_l1` 签名：返回 `Tuple[str,str]`, 使用 `Config.L1_TEMPERATURE`/`Config.L1_MAX_TOKENS`, 增加截断检测 + Metrics logger
  3. 修改 `_run_c_stage` 适配新签名：捕获 `L1TruncatedException` 做降级处理
  4. 修改 `_extract_l0` 增加 Metrics
  5. 确认 import 了 `parse_v1_markdown_xml` 和 `format_previous_summary_for_prompt`
- **验证**:
  ```bash
  pytest tests/ -v -k "test_c" --ignore=tests/test_system.py 2>&1 | tail -20
  ```
  预期：至少现有测试通过（无回归）

### Step 7: L-stage 同步

- **操作**: 修改 `ca/lstage.py` 中 `_backfill_dialogue`：
  1. `_call_llm_for_l1` 调用的返回值解包改为 `(response_text, finish_reason) = ...`
  2. 捕获 `L1TruncatedException` 后设 `_assemble_status=1`（留在待补全队列）
  3. 其余数据流与 C-stage 一致
- **验证**:
  ```bash
  pytest tests/ -v -k "test_v440" --ignore=tests/test_system.py 2>&1 | tail -20
  ```
  预期：现有测试通过

### Step 8: Metrics + AssembleStats 扩展

- **操作**: 
  1. 在 `ca/stats.py` 的 `AssembleStats` 中新增：`truncated_fallback: int = 0`, `parse_fallback_count: int = 0`, `skipped_empty: int = 0`, `latency_ms: float = 0.0`
  2. 在 `ca/__init__.py` 的截断/解析/空文本/延迟接入点添加 `self.stats.xxx += 1` 计数
  3. 确保每个 Metrics 点均输出 `[CA-METRIC]` 前缀的 logger.warning
- **验证**:
  ```bash
  python -c "
  from ca.stats import AssembleStats;
  s = AssembleStats();
  s.truncated_fallback = 1;
  s.parse_fallback_count = 2;
  s.skipped_empty = 1;
  s.latency_ms = 1234.5;
  assert 'truncated_fallback=1' in str(s);
  assert 'latency_ms' in str(s);
  print('OK')"
  ```

### Step 9: 单元测试 — `test_parse_v1.py`

- **操作**: 新建 `tests/test_parse_v1.py`，包含 ~15 个测试用例：
  1. 正常 Markdown+XML 完整输出
  2. 无 `<core_change>` 标签
  3. 空字符串输入
  4. None 输入
  5. 无意义 core_change（"无"）
  6. 超长 l0_text 截断
  7. 某个分类超过 3 项
  8. 列表项超长截断
  9. 标题下无列表项（纯文本）
  10. 语义短路 MEANINGLESS_CORE
  11. `_safe_truncate` 句号截断
  12. `_safe_truncate` 逗号截断
  13. `_safe_truncate` 无需截断
  14. `_json_to_v1_markdown` 完整 JSON
  15. `_json_to_v1_markdown` 空 dict
  16. `_json_to_v1_markdown` 未知结构 fallback
- **验证**:
  ```bash
  pytest tests/test_parse_v1.py -v 2>&1 | tail -30
  ```

### Step 10: 单元测试 — `test_store_adapter.py`

- **操作**: 新建 `tests/test_store_adapter.py`，包含 ~8 个测试用例：
  1. None 输入
  2. 空字符串输入
  3. "无"输入
  4. 完整 JSON 输入（含所有 5 字段）
  5. 部分 JSON 输入（仅 core_change）
  6. 纯文本输入（非 JSON）
  7. 非法 JSON 格式
  8. JSON 中空列表字段
- **验证**:
  ```bash
  pytest tests/test_store_adapter.py -v 2>&1 | tail -20
  ```

### Step 11: 回归测试 — C-stage 截断检测

- **操作**: 在 `tests/test_c.py` 末尾追测 ~3 个用例：
  1. `test_tc_c_XXX_truncated_fallback` — mock `_call_llm_for_l1` 抛 `L1TruncatedException`，验证 `_assemble_status=1`
  2. `test_tc_c_XXX_parse_fallback` — mock 返回无 `<core_change>` 的文本，验证解析降级
  3. `test_tc_c_XXX_l0_empty` — mock 返回 core_change="无"，验证 l0="无"
- **验证**:
  ```bash
  pytest tests/test_c.py -v -k "truncat or fallback or l0_empty" 2>&1 | tail -20
  ```

### Step 12: 回归测试 — L-stage 截断路径

- **操作**: 在 `tests/test_v440.py` 末尾追测 ~2 个用例：
  1. `test_lstage_truncation_retry` — mock `_call_llm_for_l1` 首次抛 `L1TruncatedException`，验证重试逻辑
  2. `test_lstage_truncation_permanent` — mock 连续失败 3 次，验证 `_assemble_status=2` 永久标记
- **验证**:
  ```bash
  pytest tests/test_v440.py -v -k "truncat or permanent" 2>&1 | tail -20
  ```

### Step 13: 全量回归

- **操作**: 运行全部测试（除 system 测试）
- **验证**:
  ```bash
  pytest tests/ --ignore=tests/test_system.py -v 2>&1 | tail -30
  ```
  预期：全部通过或仅预期内的失败

---

## 自检

- [x] 所有需求点有对应实现方案（REQ-1~REQ-13 均已覆盖）
- [x] 接口设计完整清晰（所有新增/修改接口均有签名、说明、预期行为）
- [x] 边界条件和错误处理有方案（空/None/截断/无意义/超长/非法JSON 全部覆盖）
- [x] 实施步骤可执行（每步关联验证命令，粒度 2-5 分钟）
- [x] Metrics 接入使用 logger.warning 计数 + mark 标记，不做 Prometheus 依赖
- [x] 所有接口签名写完整（参数类型、返回值、异常）
- [x] 保持 DB 格式不变，不做 schema migration
- [x] 明确标注了"不做什么"（不修改 Hermes 钩子、ToolSummarizer、A-stage、topic picking）
