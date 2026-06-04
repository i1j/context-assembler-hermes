# ContextAssembler AI 测试代理指南 v4.4.0

> **版本**：v4.4.0（覆盖 146 个测试用例）  
> **基线**：测试计划 v4.4.0 / 用例定义 JSON  
> **存放位置**：`tests/agents.md`

---

## 目录结构

```
tests/
├── agents.md              # 本文件 — AI 测试代理执行指南
├── docs/                  # 测试文档（测试计划、报告、bug报告等）
│   └── testplan.md        # 测试计划 v4.4.0
├── testcases/             # 测试用例 JSON 定义
├── data/                  # 测试数据（畸形 JSON 等）
├── legacy/                # 旧版测试（已归档）
├── generate_tests.py      # 测试代码生成器（v4.3.4 用例）
├── conftest.py            # pytest 共享 fixture
├── test_*.py              # pytest 测试文件（12 个 v4.3.4 + 1 个 v4.4.0）
└── benchmark_l1.py        # L1 性能基准脚本

---

## 1. 环境准备

```bash
cd ~/projects/context-assembler

# 安装依赖
pip install pytest pytest-mock pytest-cov psutil freezegun

# 1. 生成原有 100 个测试用例代码（v4.3.4）
python3 tests/generate_tests.py \
    --json tests/testcases/ContextAssembler_testcases_v4.3.4.json \
    --output-dir tests

# 2. v4.4.0 补充测试已直接可用（test_v440.py，手写代码，非生成）
# 确认测试文件就绪
ls tests/test_*.py
```

生成产物：`tests/conftest.py` + 12 个 `test_*.py`（原有 v4.3.4）+ `test_v440.py`（新增 46 个 v4.4.0 用例）

---

## 2. 文件与用例映射

### 原有测试文件（v4.3.4，100 个用例）

| 文件 | 用例范围 | 数量 | 外部依赖 |
|------|---------|------|----------|
| `test_c.py` | TC‑C‑001 ~ TC‑C‑014 | 16 | 无（mock LLM） |
| `test_a.py` | TC‑A‑001 ~ TC‑A‑019 | 19 | 无 |
| `test_store.py` | TC‑S‑001 ~ TC‑S‑010 | 10 | 无（TC‑S‑010 限 Linux） |
| `test_embedding.py` | TC‑E‑001 ~ TC‑E‑005 | 5 | 无（fallback） |
| `test_config.py` | TC‑CF‑001 ~ TC‑CF‑006 | 6 | 无 |
| `test_health.py` | TC‑M‑001 ~ TC‑M‑003 | 3 | 无 |
| `test_circuit.py` | TC‑CB‑001 ~ TC‑CB‑007 | 7 | 无 |
| `test_lifecycle.py` | TC‑RESET‑001 ~ TC‑INTF‑003 | 10 | 无 |
| `test_concurrency.py` | TC‑CONC‑001 ~ TC‑CONC‑004 | 5 | 无 |
| `test_degradation.py` | TC‑DEGR‑001, TC‑DEGR‑002 | 2 | 无 |
| `test_quality.py` | TC‑QUAL‑001 ~ TC‑QUAL‑007 | 7 | L1: 无；L2: GPU/LLM |
| `test_system.py` | TC‑ST‑001 ~ TC‑ST‑003 | 3 | Ollama 服务 |

### 新增测试文件（v4.4.0，46 个用例）

| 文件 | 用例范围 | 数量 | 外部依赖 |
|------|---------|------|----------|
| `test_v440.py` | 全部 46 个 v4.4.0 补充用例（C 工具轮/A 工具轮/L 补全/配置/存储/并发/性能），pytest marker `v440` | 46 | 需要 Ollama 的 L‑stage 测试（TC‑L‑001/003/004/006）除外；其余无外部依赖 |

用例分组：

| Group | 类名 | 用例 ID | 数量 |
|-------|------|---------|------|
| C‑stage 工具轮 | `TestToolTurnCStage` | TC‑C‑015 ~ TC‑C‑026 | 12 |
| A‑stage 工具轮 | `TestToolTurnAStage` | TC‑A‑020 ~ TC‑A‑027 | 8 |
| L‑stage 补全 | `TestLStageBackfill` | TC‑L‑001 ~ TC‑L‑007 | 7 |
| 配置/存储/统计/并发/性能 | `TestConfigAndOthers` | TC‑CF‑007~009, TC‑STORE‑011~012, TC‑STATS‑001, TC‑CONC‑005~006, TC‑PERF‑002~004 | 11 |

---

## 3. 执行命令

### 3.1 快速全量（跳过 L2 与系统测试）

```bash
# 原有 v4.3.4 测试
pytest tests/ -v --tb=short -m "not l2" --ignore=tests/test_system.py

# v4.4.0 补充测试（排除需真实 Ollama 的 L‑stage）
pytest tests/test_v440.py -v --tb=short -k "not (TC_L_001 or TC_L_003 or TC_L_004 or TC_L_006)"

# v4.4.0 全部（含 L‑stage，需本地 Ollama + qwen3.5:hermes）
pytest tests/test_v440.py -v --tb=short

# 仅 v4.4.0 中无外部依赖的用例
pytest tests/test_v440.py -v --tb=short -k "not TC_L_"
```

### 3.2 按优先级

```bash
# 原有 v4.3.4 按标签筛选
pytest tests/ -v -m "critical or high" --ignore=tests/test_system.py
pytest tests/ -v -m "medium" --ignore=tests/test_system.py
pytest tests/ -v -m "low"

# v4.4.0 按 marker 筛选
pytest tests/test_v440.py -v -m "v440"
```

### 3.3 质量分层

```bash
# L1 快速门禁（每次 CI）
pytest tests/test_quality.py -v -m "l1"

# L2 深度评估（需 GPU/LLM Judge，Nightly）
pytest tests/test_quality.py -v -m "l2"
```

### 3.4 平台特定（Linux 锁行为验证）

```bash
pytest tests/ -v -m "linux_only"
```

### 3.5 带覆盖率报告

```bash
pytest tests/ -v --cov=ca --cov-report=term-missing \
    -m "not l2" --ignore=tests/test_system.py

# v4.4.0 单独覆盖率
pytest tests/test_v440.py -v --cov=ca --cov-report=term-missing
```

---

## 4. 数据与 Mock 策略

| 测试场景 | 数据位置 / Mock 方式 |
|----------|----------------------|
| 畸形 JSON (TC‑C‑005) | `tests/data/malformed_json/` 目录下 ≥100 个文件 |
| LLM 异常输出 (TC‑C‑011~014) | `unittest.mock.patch` 注入 |
| 嵌入服务不可用 | **全 v4.4.0 测试 autouse fixture** `_mock_embed` 自动 mock `EmbeddingClient.embed` 返回虚拟向量（768维 [0.1]*768），防止无 Ollama 时挂起 |
| 并发竞争 (TC‑CONC‑004,006,007) | `threading.Event` 精确控制 C/A/L 三阶段时序 |
| SQLite 死锁 (TC‑STORE‑012) | `patch.object(store, 'write_turn', return_value=False)` — **不能 patch `sqlite3.Connection.execute`**（CPython 3.12+ C 扩展不可变） |
| 中文 BM25 检索 (TC‑C‑022) | 注意 `json.dumps` 默认 `ensure_ascii=True` 会把中文转义为 `\uXXXX`，BM25 tokenizer 会将 `\u5929` 当作 ASCII token `u5929` 而非中文字符 `天`。测试中需显式设置 `ensure_ascii=False` |
| 工具轮规则引擎 (TC‑C‑015~022) | 直接调用 `ToolSummarizer.summarize()`（传入单个 tool_call 条目 `{"id":..., "function":...}`，不是完整 assistant 消息）或触发 C‑stage |
| L‑stage 补全 (TC‑L‑001~007) | 数据库插入 + `trigger()` + 轮询等待；需运行 Ollama 的测试：TC‑L‑001/003/004/006（对话轮 LLM 补全）；无需 LLM 的测试：TC‑L‑002（规则引擎）、TC‑L‑005/007（已 mock `_call_llm_for_l1`） |
| 质量评估 L2 | 需安装 `bert-score rouge-score sentence-transformers jieba spacy` 并下载 `zh_core_web_sm` 模型 |
| 端到端系统测试 | 需本地 Ollama 服务 + 模型 `qwen3.5:hermes` 和 `Qwen3-Embedding-0.6B:Q8_0`，否则自动跳过 |

### v4.4.0 特殊 Mock 说明

`test_v440.py` 的 `_mock_embed` autouse fixture 会自动 mock 所有涉及 C‑stage/L‑stage 测试中的嵌入调用。纯单元测试（直接调 `ToolSummarizer`/`Config`）不受影响。

判断依据：`request.fixturenames` 中检测到 `ca_engine` 或 `engine` 时自动注入。

---

## 5. 结果解析与报告

AI Agent 必须解析 pytest 输出，对每个 FAILURE 提取 `AssertionError` 及堆栈，对照 JSON 用例的 `expected` 字段生成结构化报告。

```json
{
  "execution_time": "ISO8601",
  "total": 146,
  "passed": 130,
  "failed": 4,
  "skipped": 12,
  "failures": [
    {
      "id": "TC-...",
      "title": "...",
      "error": "AssertionError: ...",
      "analysis": "根因分析"
    }
  ],
  "coverage": {
    "ca/__init__.py": "82%",
    "ca/tool_summarizer.py": "90%",
    "ca/lstage.py": "75%"
  }
}
```

报告文件保存为 `tests/test_execution_report_v4.4.0_<timestamp>.json`。

---

## 6. CI 集成

### L1 门禁（每次推送）

```yaml
- run: |
    pip install pytest pytest-mock pytest-cov psutil freezegun
    python3 tests/generate_tests.py --json tests/testcases/ContextAssembler_testcases_v4.3.4.json --output-dir tests
    # v4.4.0 补充测试（跳过需 Ollama 的 L‑stage）
    pytest tests/ -v --cov=ca --cov-report=xml -m "not l2" --ignore=tests/test_system.py
    pytest tests/test_v440.py -v --cov=ca --cov-report=xml -k "not (TC_L_001 or TC_L_003 or TC_L_004 or TC_L_006)"
```

### Nightly + L2（自托管 GPU 节点）

```yaml
- run: |
    pip install pytest pytest-mock pytest-cov psutil freezegun bert-score rouge-score sentence-transformers jieba spacy
    python -m spacy download zh_core_web_sm
    python3 tests/generate_tests.py --json tests/testcases/ContextAssembler_testcases_v4.3.4.json --output-dir tests
    pytest tests/ -v --cov=ca --cov-report=xml
    pytest tests/test_v440.py -v --cov=ca --cov-report=xml
```

---

## 7. 扩展与新用例

1. v4.3.4 范围用例：在 `tests/testcases/` 下的对应 JSON 文件中新增定义，重新运行 `generate_tests.py`。
2. v4.4.0 范围用例：直接在 `tests/test_v440.py` 中添加测试函数（手写）。
3. 所有 v4.4.0 测试函数需标记 `@pytest.mark.v440`。
4. 常用 Fixtures：`ca_engine`（创建临时引擎，调用 `destroy()` 自动清理）、`fd_checker`（文件描述符泄漏检查）、`hardware_info`（硬件基线）、`_mock_embed`（autouse 自动 mock 嵌入）。

---

## 8. 追溯矩阵

| 测试计划章节 | 对应测试文件 |
|--------------|-------------|
| C‑stage（含工具轮） | `test_c.py`, `test_v440.py` |
| A‑stage（含工具轮组装） | `test_a.py`, `test_v440.py` |
| 存储层 | `test_store.py`, `test_v440.py` |
| 嵌入服务 | `test_embedding.py` |
| 配置管理（含新增） | `test_config.py`, `test_v440.py` |
| 健康检查 | `test_health.py` |
| 断路器 | `test_circuit.py` |
| 生命周期 / 接口 | `test_lifecycle.py` |
| 并发与销毁 | `test_concurrency.py`, `test_v440.py` |
| 降级质量 | `test_degradation.py` |
| 摘要质量评估 | `test_quality.py` |
| 系统端到端 | `test_system.py` |
| L‑stage 补全 | `test_v440.py` |
| 性能基线 | `test_v440.py` |

---

**文档结束**

*本文档为 AI 测试代理提供 v4.4.0 完整执行指令，覆盖全部 146 个用例，实现零人工干预的自动化回归。*
