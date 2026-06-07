# [测试技术方案 v2: L1 摘要系统重构]

## 1. 测试策略

| 维度 | 选择 | 说明 |
|------|------|------|
| **类型** | 单元 + 集成 + 回归 | 纯函数单元测试 + C-stage/L-stage 集成追测 |
| **级别** | 单元 → 集成 → 回归 → 全量 | 按依赖顺序：先测纯函数，再测集成路径，最后全量回归 |
| **方法** | 全自动化 | pytest + monkeypatch + unittest.mock |
| **隔离** | LLM 和 Embedding 全部 mock | 不允许连接真实 Ollama 服务 |

**分层映射**：

```
单元（纯函数）         集成（mock LLM）        回归（全量）
─────────────         ─────────────           ──────────
parse_v1_markdown_xml  _call_llm_for_l1       pytest tests/
_safe_truncate         L1TruncatedException   --ignore=test_system.py
_json_to_v1_markdown   C-stage 截断降级
format_previous_summary L-stage _backfill_dialogue
_new config items      OODAParser 新别名
```

---

## 2. 测试场景

### 2.1 新增 `tests/test_parse_v1.py` — `parse_v1_markdown_xml` 全场景（~15 用例）

| # | 场景 | 输入示例 | 预期结果 | 关联需求 |
|---|------|---------|---------|---------|
| P1 | 正常完整输出（4 类 Markdown + `<core_change>`） | `"### 现象与问题\n- CPU 使用率 90%\n### 背景与约束\n- 内存 8G\n### 决策与共识\n- 扩容\n### 后续行动\n- 采购单\n<core_change>CPU 过高决定扩容</core_change>"` | `({"现象与问题": ["CPU 使用率 90%"], "背景与约束": ["内存 8G"], "决策与共识": ["扩容"], "后续行动": ["采购单"]}, "CPU 过高决定扩容")` | REQ-7 |
| P2 | 输出无 `<core_change>` 标签 | 同上但无 `<core_change>...</core_change>` | fallback: `l0_text=None`，l1_dict 仍包含 4 类列表 | REQ-7 |
| P3 | 截断输出（无 `</core_change>` 闭合） | `"### 现象与问题\n- 测试\n<core_change>部分内容"`（缺少 `</core_change>`） | `_safe_truncate` 截断，返回部分结果不抛异常 | REQ-5, REQ-7 |
| P4 | 空字符串输入 | `""` | 返回 `({}, None)` 或 `({"core_change": "本轮无新内容"}, None)` | REQ-7 |
| P5 | 纯 Markdown 无 XML | `"### 现象与问题\n- A\n### 决策与共识\n- B"` | 正确解析 4 类标题，`l0_text=None` | REQ-7 |
| P6 | `<core_change>` 内容为 `"无"` | `"...<core_change>无</core_change>"` | 语义短路 → `l0_text=None`（无变更） | REQ-7, REQ-11 |
| P7 | `<core_change>` 内容为 `"本轮无新内容"` | `"...<core_change>本轮无新内容</core_change>"` | 语义短路 → `l0_text=None` | REQ-7 |
| P8 | 混合 OODA 旧别名（兼容性） | 使用旧 5 类别名如 `"核心摘要"` + `"后续行动"` | `TITLE_ALIASES` 兼容新旧别名 | REQ-9, REQ-11 |
| P9 | 中文标题别名全映射 | `"现象与问题"` / `"背景与约束"` / `"决策与共识"` / `"后续行动"` | 4 类别名全部映射到对应字段 | REQ-9 |
| P10 | 某类标题下无列表项目 | `"### 决策与共识\n无"` | 该字段返回空列表 `[]` | REQ-7 |
| P11 | 列表项超过 3 个 | 某类有 5 个 `-` 项 | 只保留前 3 项 | REQ-7 |
| P12 | 尾随噪音字符 | core_change 后有额外换行/垃圾字符 | 物理截断尾随噪音 | REQ-7 |
| P13 | `_safe_truncate` 标点降级优先 | 长文本在逗号、句号、分号位置截断 | 句号优先于逗号，逗号优先于字符硬截 | REQ-7 |
| P14 | `_safe_truncate` 超长 l0_text（200 字） | core_change 内容 200 字符 | 智能截断至 100 字符 | REQ-7 |
| P15 | 预编译正则匹配 `MEANINGLESS_CORE` 集合 | core_change 为 `"无"`/`"无变更"`/`"None"` 等 | 语义短路 → `l0_text=None` | REQ-7 |

### 2.2 新增 `tests/test_store_adapter.py` — `format_previous_summary_for_prompt`（~8 用例）

| # | 场景 | 输入 | 预期结果 | 关联需求 |
|---|------|------|---------|---------|
| S1 | None 输入 | `None` | 返回 `"无"` | REQ-8, REQ-13 |
| S2 | 空字符串 | `""` | 返回 `"无"` | REQ-8 |
| S3 | 旧 5 类 JSON → Markdown | `'{"core_change":"修复Bug","new_materials":["日志"],"objective_facts":["OOM"],"consensus":["加内存"],"todo":["采购"]}'` | 转换为 4 类 Markdown 格式（现象与问题/背景与约束/决策与共识/后续行动） | REQ-8, REQ-13 |
| S4 | 纯文本原样返回 | `"纯文本摘要内容"` | 原样返回同一字符串 | REQ-8 |
| S5 | JSON 缺少部分字段 | `'{"core_change":"test"}'` | 缺失字段对应类别写 `"无"` | REQ-8 |
| S6 | JSON 含空列表字段 | `'{"core_change":"test","new_materials":[],"objective_facts":[],"consensus":[],"todo":[]}'` | 空列表 `[]` → 对应类别写 `"无"` | REQ-8 |
| S7 | 已是最新 Markdown 格式 | 已是新 4 类 Markdown 字符串 | 原样返回（幂等） | REQ-8 |
| S8 | 超大 JSON（模拟 DB 历史记录） | 5 个字段各含 10 个列表项的 JSON | 只输出前 3 项，不崩溃 | REQ-8, REQ-13 |

### 2.3 交叉评审补充测试（~10 用例）

根据双线交叉评审结果，补充以下测试场景：

| # | 测试文件 | 场景 | 输入 | 预期结果 | 关联需求 |
|---|---------|------|------|---------|---------|
| X1 | `test_parse_v1.py` | **REQ-1: prompt 内容验证** | import `L1_GENERATION_PROMPT`，检查常量内容 | 包含"研发对话意图分析器"，包含 `{previous_summary}` 和 `{current_dialog}` 占位符，包含 `<core_change>` | REQ-1 |
| X2 | `test_parse_v1.py` | **REQ-1: prompt 不含旧特征** | 检查常量内容 | 不包含"会议纪要摘要助手"，不包含"自我检查"提示 | REQ-1 |
| X3 | `test_parse_v1.py` | **L1TruncatedException 独立验证** | `L1TruncatedException("msg", "resp_text")` | 继承 `Exception`，属性 `message` 和 `response_text` 正确 | REQ-6 |
| X4 | `test_parse_v1.py` | **`_json_to_v1_markdown` 直接单元测试** | 输入含 5 类英 key 的 dict | 输出正确 4 类 Markdown 格式，含 `<core_change>` | REQ-7 |
| X5 | `test_parse_v1.py` | **`_json_to_v1_markdown` 缺失 key 容错** | 输入缺 `consensus` key | 不抛异常，该节输出"- 无" | REQ-7 |
| X6 | `test_parse_v1.py` | **`_json_to_v1_markdown` 特殊字符** | 列表项含 `-` / `#` / `\n` | Markdown 输出可被 `parse_v1_markdown_xml` 正确回读 | REQ-7 |
| X7 | `test_c.py` 追测 | **REQ-11: DB 写入格式端到端验证** | 执行 `_run_c_stage` 后检查 store 中 `l1_text` | 可 JSON 解析，含 `core_change`/`new_materials`/`objective_facts`/`consensus`/`todo` 五个英 key | REQ-11 |
| X8 | `test_c.py` 追测 | **REQ-4: 部分 mock — config 参数接线验证** | mock `urllib.request.urlopen` 而非整体 mock `_call_llm_for_l1`，设置 `L1_TEMPERATURE=0.5`, `L1_MAX_TOKENS=600` | LLM 请求体包含 `"temperature": 0.5` 和 `"max_tokens": 600` | REQ-4 |
| X9 | `test_parse_v1.py` | **`_safe_truncate` 边界值** | `max_len=0`, `max_len=1`, 全标点文本, 全空格文本, 负值 max_len | 不抛异常，返回长度不超过绝对值 | REQ-7 |
| X10 | `test_a.py` 回归 | **回归验证：旧格式别名仍正常工作** | OODAParser 解析旧 5 类中文标题（资源与观察/事实与约束等） | 正确映射到 5 类英 key | REQ-9 |

### 2.5 回归追测 `tests/test_c.py` — C-stage 截断检测（~3 用例）

| # | 场景 | Mock 设置 | 预期结果 | 关联需求 |
|---|------|-----------|---------|---------|
| C-T1 | LLM 返回截断（finish_reason='length'） | `_call_llm_for_l1` 返回 `(response_text, 'length')` | 抛 `L1TruncatedException` → `_assemble_status=1`（backfill） | REQ-5, REQ-10 |
| C-T2 | LLM 返回正常（finish_reason='stop' 且含 `</core_change>`） | `_call_llm_for_l1` 返回 `(完整Markdown+XML, 'stop')` | 正常解析，`_assemble_status=0` | REQ-5, REQ-10 |
| C-T3 | LLM 返回不含 `</core_change>` 但 finish_reason='stop' | `_call_llm_for_l1` 返回 `(无闭合标签内容, 'stop')` | 触发 `endswith` 检测 → 抛 `L1TruncatedException` | REQ-5 |

### 2.6 回归追测 `tests/test_v440.py` — L-stage 截断路径（~2 用例）

| # | 场景 | 设置 | 预期结果 | 关联需求 |
|---|------|------|---------|---------|
| L-T1 | L-stage `_backfill_dialogue` 调用新签名 | 插入 `_assemble_status=1` 记录，mock `_call_llm_for_l1` 返回 `(text, 'stop')` | 补全成功，DB 记录更新，状态变为 0 | REQ-10 |
| L-T2 | L-stage 截断检测触发降级 | mock `_call_llm_for_l1` 返回截断信号 | backfill 捕获异常 → attempts++，3 次后永久失败标记 | REQ-5, REQ-10 |

### 2.7 Config 项测试（追测 `tests/test_config.py`）

| # | 场景 | 操作 | 预期结果 | 关联需求 |
|---|------|------|---------|---------|
| CFG-1 | `CA_L1_TEMPERATURE` 默认值 | 不设环境变量 | `Config.L1_TEMPERATURE == 0.3` | REQ-2 |
| CFG-2 | `CA_L1_MAX_TOKENS` 默认值 | 不设环境变量 | `Config.L1_MAX_TOKENS == 800` | REQ-3 |
| CFG-3 | `CA_L1_TEMPERATURE` 热重载 | 设 env → `Config.reload()` | 新值生效 | REQ-2, REQ-4 |
| CFG-4 | `CA_L1_MAX_TOKENS` 热重载 | 设 env → `Config.reload()` | 新值生效 | REQ-3, REQ-4 |
| CFG-5 | validate 校验范围和边界 | 非法值（负数/零/超大） | `Config.validate()` 抛出 `ValueError` | REQ-2, REQ-3 |

### 2.8 Metrics 接入测试（追测 `tests/test_c.py` 或独立文件）

| # | 场景 | 操作 | 预期结果 | 关联需求 |
|---|------|------|---------|---------|
| M-1 | `ca.l1.truncated_fallback` 计数 | 触发截断降级 | Counter 自增 1 | REQ-12 |
| M-2 | `ca.l1.parse_fallback_count` 计数 | 解析无 `<core_change>` | Counter 自增 1 | REQ-12 |
| M-3 | `ca.l0.skipped_empty` 计数 | L0 为空字符串 | Gauge 更新 | REQ-12 |
| M-4 | `ca.l1.latency_ms` 记录 | `_call_llm_for_l1` 正常返回 | Histogram 记录延迟 | REQ-12 |

---

## 3. 环境与工具

### 3.1 测试框架/工具

| 项目 | 版本/说明 |
|------|----------|
| pytest | >= 7.0（推荐 9.x） |
| pytest markers | `@pytest.mark.critical`, `@pytest.mark.l1` 等（已在 `conftest.py` 中注册） |
| unittest.mock | `patch`, `patch.object`, `MagicMock` |
| monkeypatch | 用于环境变量和模块级属性覆盖 |
| temporary directory | `tmp_path` fixture（SQLite 测试用） |

### 3.2 Mock 策略

```
┌──────────────────────────────────────────────────────┐
│                  Mock 策略一览                         │
├────────────────────┬────────────┬─────────────────────┤
│ 调用点             │ 必须 Mock  │ Mock 方式            │
├────────────────────┼────────────┼─────────────────────┤
│ EmbeddingClient    │    ✅      │ conftest 已全局       │
│ .embed()           │            │ autouse fixture      │
├────────────────────┼────────────┼─────────────────────┤
│ ContextAssembler   │    ✅      │ 返回 Tuple[str,str]   │
│ ._call_llm_for_l1()│            │ (新签名)             │
├────────────────────┼────────────┼─────────────────────┤
│ parse_v1_markdown  │    ❌      │ 纯函数，直接调用      │
│ _xml()             │            │                      │
├────────────────────┼────────────┼─────────────────────┤
│ _safe_truncate()   │    ❌      │ 纯函数，直接调用      │
├────────────────────┼────────────┼─────────────────────┤
│ _json_to_v1_mark   │    ❌      │ 纯函数，直接调用      │
│ down()             │            │                      │
├────────────────────┼────────────┼─────────────────────┤
│ format_previous    │    ❌      │ 纯函数，直接调用      │
│ _summary_for_prompt│            │                      │
├────────────────────┼────────────┼─────────────────────┤
│ urllib.request     │    ✅      │ 间接（通过 mock      │
│ (LLM HTTP 调用)    │            │ _call_llm_for_l1）   │
├────────────────────┼────────────┼─────────────────────┤
│ Metrics 上报       │    ⚠️       │ 先用 logger.warning │
│ (Prometheus)       │            │ 计数，确认接口后接入  │
└────────────────────┴────────────┴─────────────────────┘
```

**具体 Mock 实现说明**：

1. **LLM Mock（新签名）**：`_call_llm_for_l1` 将返回 `Tuple[str, str]`。mock 必须返回元组：
   ```python
   @pytest.fixture
   def mock_llm_normal():
       with patch.object(ContextAssembler, '_call_llm_for_l1',
                         return_value=(
       "### 现象与问题\n- 测试\n### 背景与约束\n- 无\n"
       "### 决策与共识\n- 无\n### 后续行动\n- 无\n"
       "<core_change>测试</core_change>",
       "stop"
       )):
           yield
   
   @pytest.fixture
   def mock_llm_truncated():
       with patch.object(ContextAssembler, '_call_llm_for_l1',
                         return_value=(
       "### 现象与问题\n- 部分内容...",
       "length"
       )):
           yield
   ```

2. **Embedding Mock**：使用 `conftest.py` 现有的 `_mock_embed` autouse fixture，无需额外操作。

3. **Config Mock**：使用 `monkeypatch.setenv('CA_L1_TEMPERATURE', '0.5')` + `Config.reload()`。

### 3.3 环境要求

| 要求 | 说明 |
|------|------|
| Python | >= 3.10（推荐 3.12） |
| 操作系统 | Linux / macOS |
| 第三方包 | pytest, 项目已有依赖（无需新增） |
| LLM 服务 | **不需要**（全部 mock） |
| Embedding 服务 | **不需要**（全部 mock） |
| 磁盘 | 临时目录（pytest `tmp_path`） |
| 环境变量 | 无需预设，测试中通过 `monkeypatch` 设置 |

---

## 4. 实施步骤

### 阶段 1：新增 `tests/test_parse_v1.py`

```python
# 关键测试结构示例（伪代码框架）
import pytest
from ca.post_process import parse_v1_markdown_xml, _safe_truncate

class TestParseV1MarkdownXml:

    @pytest.mark.critical
    @pytest.mark.l1
    def test_normal_full_output(self):
        """P1: 正常完整 4 类 + <core_change>"""
        llm_output = (
            "### 现象与问题\n- CPU 使用率 90%\n"
            "### 背景与约束\n- 内存 8G\n"
            "### 决策与共识\n- 扩容\n"
            "### 后续行动\n- 采购单\n"
            "<core_change>CPU 过高决定扩容</core_change>"
        )
        l1_dict, l0_text = parse_v1_markdown_xml(llm_output)
        assert "现象与问题" in l1_dict
        assert l1_dict["现象与问题"] == ["CPU 使用率 90%"]
        assert l0_text == "CPU 过高决定扩容"

    @pytest.mark.high
    @pytest.mark.l1
    def test_no_core_change_tag(self):
        """P2: 无 <core_change> 标签"""
        llm_output = "### 现象与问题\n- 测试"
        l1_dict, l0_text = parse_v1_markdown_xml(llm_output)
        assert l0_text is None  # fallback

    # ... 其余 ~13 用例
```

**执行命令**：
```bash
pytest tests/test_parse_v1.py -v --tb=short
```

### 阶段 2：新增 `tests/test_store_adapter.py`

```python
# 关键测试结构示例
import pytest
import json
from ca.store import format_previous_summary_for_prompt

class TestFormatPreviousSummary:

    @pytest.mark.high
    @pytest.mark.l1
    def test_none_input(self):
        """S1: None → '无'"""
        assert format_previous_summary_for_prompt(None) == "无"

    @pytest.mark.high
    @pytest.mark.l1
    def test_json_to_markdown(self):
        """S3: 旧 5 类 JSON → 4 类 Markdown"""
        old_json = json.dumps({
            "core_change": "修复Bug",
            "new_materials": ["日志文件"],
            "objective_facts": ["OOM"],
            "consensus": ["加内存"],
            "todo": ["采购"]
        })
        result = format_previous_summary_for_prompt(old_json)
        assert "现象与问题" in result or "资源与观察" in result
        assert "修复Bug" in result

    # ... 其余 ~6 用例
```

**执行命令**：
```bash
pytest tests/test_store_adapter.py -v --tb=short
```

### 阶段 3：追测 `tests/test_c.py` — 截断检测

```python
# 追加到现有 test_c.py 中
@pytest.mark.high
@pytest.mark.l1
def test_tc_c_016_truncation_detection(engine):
    """C-T1: finish_reason='length' 触发降级"""
    from ca import L1TruncatedException
    with patch.object(engine, '_call_llm_for_l1',
                      return_value=("### 现象与问题\n- 部分", "length")):
        tidx = max(engine._turn_counter, 0) + 1
        with pytest.raises(L1TruncatedException):
            engine._run_c_stage(engine._session_id, tidx, {},
                                "User: hi\nAssistant: hi", 0)
```

**执行命令**：
```bash
pytest tests/test_c.py -v -k "truncation or L1Truncated" --ignore=tests/test_system.py
```

### 阶段 4：追测 `tests/test_v440.py` — L-stage 截断路径

```python
# 追加到 TestLStageBackfill 类中
@pytest.mark.l1
def test_TC_L_008_backfill_truncation(self, ca_engine):
    """L-T2: L-stage 截断检测"""
    from ca import L1TruncatedException
    tidx = max(ca_engine._turn_counter, 0) + 1
    ca_engine.store.conn.execute(
        "INSERT INTO turn_cache (session_id, turn_index, turn_type, "
        "tool_sub_index, l2_text, _assemble_status, l1_text) "
        "VALUES (?,?,?,?,?,?,?)",
        (TEST_SESSION, tidx, "dialogue", 0, "User: hi", 1,
         json.dumps({"core_change": "本轮无新内容"}))
    )
    ca_engine.store.conn.commit()
    with patch.object(ca_engine, '_call_llm_for_l1',
                      return_value=("部分内容", "length")):
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        # 验证 attempts 递增
        row = ca_engine.store.conn.execute(
            "SELECT backfill_attempts FROM turn_cache WHERE session_id=? AND turn_index=?",
            (TEST_SESSION, tidx)
        ).fetchone()
        assert row[0] >= 1
```

**执行命令**：
```bash
pytest tests/test_v440.py -v -k "backfill_truncation" --ignore=tests/test_system.py
```

### 阶段 5：全量回归

```bash
pytest tests/ --ignore=tests/test_system.py -v --tb=short 2>&1 | tail -30
```

**通过标准**：全部测试通过，零失败、零 error、零 skip（除明确因环境跳过的测试）。

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| `_call_llm_for_l1` 改签名后现有 mock 失效 | 大量测试失败 | 统一更新 `conftest.py` 中的 `_mock_llm` fixture 返回 `(str, str)` 元组 |
| `parse_v1_markdown_xml` 尚未实现 | 无法运行测试 | 先定义函数桩（`def parse_v1_markdown_xml(): raise NotImplementedError`），测试驱动开发 |
| Metrics 基础设施未就绪 | 指标测试无法验证 | 先用 `logger.warning` 计数，测试验证日志输出；确认 `Hermes Metrics` 接口后补充 |
| `TITLE_ALIASES` 扩展后旧格式不兼容 | 回归测试失败 | `test_ooda_parser.py` 中已有解析器测试，需验证新旧别名均正常 |
| `_json_to_v1_markdown` 遇到未知 JSON 结构 | 转换失败 | fallback 原样返回纯文本，测试验证降级路径 |

---

## 6. 测试文件清单

| 文件 | 操作 | 预计用例数 |
|------|------|-----------|
| `tests/test_parse_v1.py` | **新增** | 20 |
| `tests/test_store_adapter.py` | **新增** | 8 |
| `tests/test_c.py` | 追加 | +5（含交叉评审补充） |
| `tests/test_v440.py` | 追加 | +2 |
| `tests/test_config.py` | 追加 | +5 |
| `tests/test_ooda_parser.py`（legacy） | 回归验证 | 0（仅验证现有用例通过） |
| `tests/conftest.py` | 更新 `_mock_llm` 返回值 -> `Tuple[str,str]` | — |

---

## 自检

- [x] 所有测试需求有对应测试场景（REQ-1 ~ REQ-13 全覆盖）
- [x] 测试场景可执行、可重复（纯函数 + mock LLM）
- [x] 环境准备已明确（Python ≥ 3.10, pytest, 无外部服务依赖）
- [x] Mock 策略清晰：LLM 和 Embedding 必须 mock，纯函数不 mock
- [x] 新签名 `_call_llm_for_l1` 返回 `Tuple[str,str]` 已在 mock 策略中体现
- [x] 截断检测双重校验（`finish_reason` + `endswith`）有独立测试覆盖
- [x] 语义短路（`MEANINGLESS_CORE`）有测试覆盖
- [x] 向后兼容（旧 JSON → 新 Markdown 格式）有测试覆盖
- [x] Config 新增项有独立验证
- [x] Metrics 接入点有测试列（先日志后正式接入）
- [x] 全量回归范围明确：`pytest tests/ --ignore=tests/test_system.py`
