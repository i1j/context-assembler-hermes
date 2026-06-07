# 交叉评审：可测试性视角

## 发现项

| # | 类型 | 涉及接口/模块 | 描述 | 严重度 |
|---|------|-------------|------|--------|
| 1 | 回归破坏 | `conftest.py`, `test_c.py`, `test_v440.py`, `test_v460.py`, `legacy/test_c_stage.py`, `legacy/test_review_fixes.py` | **`_call_llm_for_l1` 返回类型变更导致 ~15 处现有 mock 全部失效**。原返回 `str`，新签名返回 `Tuple[str, str]`。conftest.py 中 `_mock_llm` fixture 为 `autouse=True`，影响所有依赖 `engine` 的测试。实现方案仅提到追加 3 个截断用例，未提及更新现有 mock 的 return_value（需改为 `return_value=('...', 'stop')`）。若不提前处理，Step 6 的回归验证 `pytest tests/ -k "test_c"` 将大面积失败。 | 高 |
| 2 | 依赖不可 Mock | `_call_llm_for_l1`（`ca/__init__.py`） | **LLM 客户端无抽象层，底层 http 调用不可注入**。函数内直接 `import urllib.request` + `urlopen()`，无接口/抽象类/D注入。现有测试被迫在 method 级别 patch 整个 `_call_llm_for_l1`，无法精细控制重试逻辑、超时、截断检测分支。若未来需要测试 `retry` → `error` → 降级全路径，必须模拟三次 `urlopen` 异常，但当前架构只能整体 mock 或跳过。 | 高 |
| 3 | 竞态风险 | `_call_llm_for_l1` + `self._turn_counter` | **`self._turn_counter` 在 metrics 日志中存在竞态条件**。`_call_llm_for_l1` 读取 `self._turn_counter` 写入 [CA-METRIC] 日志，但该属性在 `reset()` 和 `process_turn_async` 中被修改。C-stage 在单独线程中运行 `_run_c_stage`，若多个 turn 的 LLM 调用并发（或 reset 与 LLM 调用交错），可能读到脏值。实现方案未提及任何线程安全措施（锁/原子操作）。 | 中 |
| 4 | 测试覆盖缺口 | 全链路集成 | **缺少 `format_previous_summary_for_prompt → parse_v1_markdown_xml → ooda_parser.parse` roundtrip 集成测试**。三个模块在隔离测试中各自通过，但数据格式链存在隐式耦合：`_json_to_v1_markdown` 输出 Markdown 的 key 映射（`todo` 输出为 `actions`），再经 `parse_v1_markdown_xml` 解析回英 key，最后 `ooda_parser.parse` 接收。任一环节的 key 名映射不一致（如 `todo` vs `actions`）只有集成测试能发现。 | 中 |
| 5 | 测试覆盖缺口 | `_safe_truncate` | **缺少 `max_len` 边界值测试**。方案中测试了截断 5 和 100，但未覆盖：`max_len=0`（返回空字符串？）、`max_len=1`（硬截断逻辑）、`max_len` 为负数或非 int 类型（不通过 type hint，但 Python 不强制）、`text` 全为标点（所有优先级标点匹配失败时是否硬截断？）、`text` 全为空格。 | 中 |
| 6 | 测试覆盖缺口 | `Config` 环境变量 | **`Config.L1_TEMPERATURE` / `L1_MAX_TOKENS` 环境变量隔离未提及**。这两个 ClassVar 在模块加载时通过 `os.getenv` 初始化，测试间 env 状态会污染。例如 test A 设置 `CA_L1_TEMPERATURE=2.5`（非法值）后未清理，test B 的默认值预期被破坏。方案未说明如何隔离（`monkeypatch.setenv` / `pytest.env` / 显式 cleanup）。 | 中 |
| 7 | 测试覆盖缺口 | `_extract_l0` | **`_extract_l0` 改造后无专用测试**。该函数新增了 `core in ("无", "本轮无新内容")` 检测 + `_safe_truncate` 调用 + Metrics logger，但仅在 `test_c.py` 的 ~3 个追测中通过 `_run_c_stage` 间接覆盖。没有针对以下场景的独立单元测试：l1_dict 缺 `core_change` key、core_change 为空白字符串、core_change 为 `MEANINGLESS_CORE` 外的无意义文本。 | 低 |
| 8 | 测试覆盖缺口 | `_json_to_v1_markdown` | **列表项含特殊字符时 Markdown 输出可能损坏**。当 `new_materials` 包含 `"- 以短横线开头的项"`、`"# 带井号的标题文本"`、`"多行\n文本"` 时，生成的 Markdown 无法被 `parse_v1_markdown_xml` 正确回读。方案未覆盖此类用例。 | 低 |
| 9 | 测试覆盖缺口 | `OODAParser.TITLE_ALIASES` | **TITLE_ALIASES 扩展仅在纯断言层面验证，未集成测试**。实现方案的验证步骤仅验证了映射关系正确性，未在 `ooda_parser.parse()` 实际调用中测试新 4 类中文标题能否正确解析为 5 类英 key。若 `ooda_parser.parse` 内部对 key 名有额外校验逻辑，纯断言无法发现。 | 低 |
| 10 | 可测试性 | `L1TruncatedException` | **`_call_llm_for_l1` 中截断检测条件 `not response_text.strip().endswith('</core_change>')` 在测试中难以精确触发**。要触发截断降级路径（finish_reason != "length" 但缺 `</core_change>`），mock 返回的文本必须精心构造以不包含结尾标签。当前模拟 `_call_llm_for_l1` 抛异常的方式（side_effect=L1TruncatedException）虽可行，但无法覆盖截断检测逻辑本身的正确性——测试只能验证"抛异常后的行为"，不能验证"异常是否被正确抛出"。 | 中 |

---

## 总体评价

- **可测试性：中**

### 主要风险项

1. **回归风险（高）**：`_call_llm_for_l1` 签名变更未同步更新 15+ 处现有 mock，实施 Step 6 后将引发大面积测试失败。必须先在 conftest.py 的 `_mock_llm` fixture 及所有 `patch.object(engine, '_call_llm_for_l1', ...)` 调用处更新 return_value 为 `('response_text', 'stop')`。

2. **LLM 调用不可注入（高）**：`_call_llm_for_l1` 内部直接 `import urllib.request` + `urlopen()`，没有 DI 或接口抽象。尽管 method-level patch 能绕过，但重试/超时/截断检测的精确分支覆盖只能通过 mock 返回值来间接测试，无法测试真实的 HTTP 交互流程。

3. **并发竞态（中）**：`self._turn_counter` 在多线程场景下的读取未加锁，metrics 日志可能读到过期值。建议至少添加 `_task_lock` 保护或使用线程局部变量。

4. **链式集成空白（中）**：三个新解析/转换函数各自有单元测试，但"旧 JSON → Markdown → 解析 → OODA → DB"的端到端格式一致性无任何测试，key name 映射的微小错误需到集成阶段才能发现。

### 建议

1. **优先修复回归问题**：在实施 Step 6 前，全局替换所有 `_call_llm_for_l1` 的 mock return_value 为 `(str, str)` 元组。可以编写一个迁移脚本或 pytest 自定义 fixture 统一处理。

2. **补充集成测试**：新增 `test_roundtrip.py` 包含至少 3 个 roundtrip 用例：完整 5 类 JSON → `format_previous_summary_for_prompt` → (模拟 LLM) → `parse_v1_markdown_xml` → `ooda_parser.parse` → 断言 5 类英 key 完整保留。

3. **LLM 客户端抽象（可选增强）**：建议将 `_call_llm_for_l1` 中的 HTTP 调用提取为一个 `_post_llm(body) -> dict` 方法（或注入一个 `llm_client` 对象），使测试可以 mock 底层响应而不替换整个方法。当前 method-level patch 方式也能工作，但牺牲了重试/超时逻辑的覆盖率。

4. **并发安全**：`self._turn_counter` 的读取/写入增加 `self._task_lock` 保护，或在 metrics 日志中使用局部变量快照。

5. **补全边界测试**：
   - `_safe_truncate`: max_len=0, max_len=1, 全标点文本, 空文本
   - `_json_to_v1_markdown`: 列表项含 `-`/`#`/`\n` 的用例
   - `Config.validate()`: L1_TEMPERATURE=-1/NaN/3.0, L1_MAX_TOKENS=0/100000
   - `format_previous_summary_for_prompt`: 传入 Python `None`（非字符串 `"None"`）

6. **环境变量隔离**：在涉及 `Config` 环境变量的测试中使用 `monkeypatch.setattr(os.environ, ...)` 配合 try/finally 或 conftest fixture 的自动清理，防止 env 泄漏。

7. **`_safe_truncate` 暴露为纯函数后**：所有测试文件应直接从 `ca.post_process` import，不依赖 ContextAssembler 实例，确保测试隔离。
