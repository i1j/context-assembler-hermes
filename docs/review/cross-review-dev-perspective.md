# 交叉评审：需求覆盖视角

> **评审角色**: 开发线 Reviewer  
> **评审依据**: `ca-l1-refactor-requirements-v2.md` (需求) → `ca-l1-refactor-test-plan-v2.md` (测试方案)  
> **方法**: 逐条 REQ → 验收标准 → 测试场景映射

---

## 需求-测试覆盖矩阵（概览）

| 需求 | 验收标准关键维度 | 对应测试场景 | 覆盖状态 |
|------|-----------------|-------------|---------|
| REQ-1 | 新 prompt 使用"研发对话意图分析器"人设 | 无 | ❌ 未覆盖 |
| REQ-2 | CA_L1_TEMPERATURE: 环境变量 / 默认0.3 / validate / reload | CFG-1, CFG-3, CFG-5 | ✅ 完整 |
| REQ-3 | CA_L1_MAX_TOKENS: 环境变量 / 默认800 / validate / reload | CFG-2, CFG-4, CFG-5 | ✅ 完整 |
| REQ-4 | 独立 temperature/max_tokens 实际传递给 LLM 调用 | CFG-3, CFG-4 仅测配置加载 | ⚠️ 部分覆盖 |
| REQ-5 | 双重校验: finish_reason='length' + endswith('</core_change>') | C-T1, C-T3, L-T2 | ✅ 完整 |
| REQ-6 | L1TruncatedException 异常类定义正确 | 无独立测试（仅间接使用） | ⚠️ 部分覆盖 |
| REQ-7 | parse_v1_markdown_xml 全场景 | P1–P15 | ✅ 完整 |
| REQ-8 | format_previous_summary_for_prompt 全场景 | S1–S8 | ✅ 完整 |
| REQ-9 | TITLE_ALIASES 扩展 4 类中文别名 | P8, P9 | ✅ 完整 |
| REQ-10 | L-stage 新配置参数 + 截断检测 | L-T1, L-T2 | ⚠️ 部分覆盖 |
| REQ-11 | DB 存储格式仍为 5 类英文 key JSON | P6, P8 提及但无端到端验证 | ⚠️ 部分覆盖 |
| REQ-12 | Metrics 4 个指标接入 | M-1 ~ M-4 | ✅ 完整 |
| REQ-13 | 向后兼容: 旧格式自动转换 | S1, S3, S8 | ✅ 完整 |

---

## 发现项

| # | 类型 | 涉及测试场景 | 描述 | 严重度 |
|---|------|-------------|------|--------|
| 1 | **需求缺口** | 无对应场景 | **REQ-1 新 prompt 未验证**: 测试方案完全 mock `_call_llm_for_l1`，所有测试均绕过提示词加载流程。验收标准要求验证"研发对话意图分析器"人设和 4 类 Markdown+XML 输出格式，但无任何测试验证新 `L1_GENERATION_PROMPT` 被正确加载、格式串占位符正确、且旧 prompt 已被替换。 | **高** |
| 2 | **需求缺口** | CFG-3, CFG-4, C-T1~C-T3 | **REQ-4 参数接线未验证**: CFG-3/CFG-4 仅验证 Config 对象能加载新值，但所有 C-stage/L-stage 集成测试均完整 mock 了 `_call_llm_for_l1`，**从未验证**`L1_TEMPERATURE` 和 `L1_MAX_TOKENS` 实际传递给底层 LLM API 调用。验收标准要求"不再硬编码，不再共用 `LLM_NUM_PREDICT`"，但无测试能捕获"开发者添加了配置项却忘记传入 LLM 调用"的回归。 | **高** |
| 3 | **需求缺口** | C-T1~C-T3, L-T1, L-T2 | **REQ-6 L1TruncatedException 缺少独立验证**: 异常类仅在追测中被间接使用（`pytest.raises`），但无独立测试验证其：继承自正确的基类、`__init__` 接受合理的参数、异常消息包含有用上下文信息、新旧两处调用点都能正确捕获。 | **中** |
| 4 | **需求缺口** | C-T1, C-T2, C-T3 | **REQ-11 DB 存储格式端到端验证缺失**: 需求明确要求"仍使用 5 类英文 key 的 JSON"，但 C-stage 集成测试仅验证 `_assemble_status` 和异常抛出，未验证 `_run_c_stage` 结束后实际写入 DB 的 l1_text 字段是否为 5 类英文 key JSON 格式。P6/P8 标注了 REQ-11 但仅在 `parse_v1_markdown_xml` 层面验证别名兼容，非 DB 写入格式验证。 | **中** |
| 5 | **测试场景充分性** | S3~S6 | **`_json_to_v1_markdown` 仅被间接测试**: 需求文档将其列为 `ca/post_process.py` 中的独立新函数（第 3.1 节接口表）。测试方案仅通过 `format_previous_summary_for_prompt` 间接覆盖（S3~S6），缺少对该函数的直接单元测试（如独立的 boundary/edge 输入验证）。风险：若后期 `format_previous_summary_for_prompt` 内部重构调用链路，可能掩盖 `_json_to_v1_markdown` 本身的缺陷。 | **低** |
| 6 | **测试场景清晰度** | M-3 | **Metrics M-3 触发场景不明确**: "L0 为空字符串"未说明在哪个测试用例/哪个阶段触发。Gauge 更新为具体何值（0 / 1 / None?）也未指定。当前描述不足以让实现者写出可执行的测试断言。 | **低** |
| 7 | **Mock 合理性** | C-T1~C-T3 | **`_call_llm_for_l1` 整体 mock 导致 REQ-4/REQ-10 配置参数接线不可测**: 当前策略将所有涉及 LLM 调用的测试都完整 mock `_call_llm_for_l1`，使得参数传递路径成为盲区。建议增加 1~2 个"部分 mock"用例（仅 mock HTTP 调用层而非整个 `_call_llm_for_l1`），直接验证新配置参数被正确传入 LLM API。 | **中** |

---

## 总体评价

**需求覆盖**: **有缺口**

### 主要缺口项

1. **REQ-1 零覆盖**: 新 prompt 的正确加载和内容未被任何测试验证，是最显著的缺口。
2. **REQ-4 仅覆盖配置定义，未覆盖参数接线**: 测试验证了"配置项存在"，但未验证"配置项被使用"。这是典型的"配置尸体"风险——配置项存在但实际 LLM 调用仍使用硬编码值。
3. **REQ-11 DB 写入格式未端到端验证**: 虽然 `TITLE_ALIASES` 别名映射有覆盖，但最终 DB write 的 JSON key 是否为 5 类英文格式未被验证。

### 建议

1. **补充 REQ-1 验证**: 至少增加一个轻量级测试，验证 `from ca.prompts import L1_GENERATION_PROMPT` 正常加载、格式串中包含 `{previous_summary}` 和 `{current_dialog}` 占位符、且输出描述中包含"研发对话意图分析器"关键字。
2. **补充 REQ-4 接线验证**: 创建 1 个"部分 mock"集成测试——mock 底层 HTTP 请求层（`urllib.request.urlopen` 或 `httpx.Client`）而非 mock `_call_llm_for_l1` 整体，验证设置的 `temperature` 和 `max_tokens` 确实出现在请求体中。
3. **补充 DB 写入格式断言**: 在 C-T1/C-T2 中追加断言，验证 `_run_c_stage` 执行后 store 中对应记录的 `l1_text` 字段可被 JSON 解析且包含 `core_change` / `new_materials` / `objective_facts` / `consensus` / `todo` 五个英文 key（即使某些字段为空列表）。
4. **补充 `L1TruncatedException` 独立测试**: 增加 `test_l1_truncated_exception.py` 或合入现有文件，验证异常类的继承链、消息格式、异常可被 `except L1TruncatedException` 捕获。
5. **补充 `_json_to_v1_markdown` 直接测试**: 追加 2~3 个针对该函数的直接单元测试，验证 5 类英文 key 到 4 类中文 Markdown 的映射逻辑、缺失 key 的默认行为、非法输入的处理。
6. **明确 M-3 测试细节**: 指定该测试在哪个 fixture/场景下触发，明确 Gauge 断言值（例如 `assert ca.l0.skipped_empty._value.get() == 1`）。
