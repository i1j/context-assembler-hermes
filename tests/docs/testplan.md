# ContextAssembler v4.3.4-hotfix3 软件需求与测试计划

> ⚠️ **本文档对应 v4.3.4-hotfix3 版本。** v5.0.0 测试体系架构见
> [`v5.0.0-test-architecture.md`](v5.0.0-test-architecture.md)

**文档版本**：v4.3.4-hotfix3‑final‑r2
**编制日期**：2026-05-27
**对应设计文档**：ContextAssembler 详细设计文档 v4.3.4-hotfix3
**对应代码版本**：ca v4.3.4-hotfix3 (完整项目代码已交付)
**修订记录**：

- rev0：初始版本
- rev1：根据同行评审补充核心单元测试、并发压力测试及降级质量验证用例，修正需求缺失与数量统计
- rev2：修正数量统计错误，吸收剩余风险建议，补充生产环境执行注意事项
- rev3（final）：强化AI Agent替代人工，将质量评估自动化，消除 pending
- rev4（final‑r2）：吸收执行层面剩余风险（算力开销、OS兼容性、性能基线漂移、FD泄漏），新增 TC-C-014、扩充 TC-RESET-001，质量测试分层执行

---

## 1. 引言

### 1.1 目的

本文档整合了 **ContextAssembler** 系统的软件需求规格与测试计划，旨在为开发、测试及验收提供统一、可追溯的依据。所有功能需求均源自设计文档与代码实现，非功能需求覆盖性能、可靠性、可观测性等维度，并与已定义的测试用例形成双向映射。
**核心目标**：通过 **AI Agent 驱动的自动化测试框架**，将传统需人工干预的质量评估、异常场景复现、性能基线校准等环节全部交由脚本或AI模型完成，实现测试执行的无人值守化。

### 1.2 范围

本文档适用于 ContextAssembler 的核心组件，包括：

- C‑stage 异步压缩引擎
- A‑stage 同步上下文组装
- 全指纹去重模块
- 存储层（SQLite）与内存缓存
- 嵌入服务客户端
- 配置管理、健康检查与断路器
- 插件层生命周期管理

### 1.3 术语与缩写

- **C‑stage**：压缩阶段（Compression Stage），负责异步生成增量 L0/L1 摘要。
- **A‑stage**：组装阶段（Assembly Stage），负责同步构建上下文消息。
- **L0**：原始事实文本（从 L1 摘要中提取的核心信息）。
- **L1**：结构化增量摘要（OODA 五节格式）。
- **OODA**：观察‑判断‑决策‑行动框架（此处用于摘要结构）。
- **RRF**：倒数排名融合（Reciprocal Rank Fusion），用于合并多路检索结果。
- **WAL**：SQLite 预写式日志。
- **AI Agent**：指自动化测试脚本、LLM‑as‑a‑Judge、基于模拟的时序控制组件等，用于替代传统人工测试操作。

---

## 2. 软件需求规格

### 2.1 功能需求

#### 2.1.1 C‑stage 异步压缩

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-CSTAGE-001 | 异步非阻塞执行：主线程调用 `process_turn_async` 后应在 50ms 内返回，不等待 LLM 完成。 | `ca/__init__.py` - `process_turn_async` | ✅ 满足 |
| REQ-FUNC-CSTAGE-002 | 增量 L1 摘要生成：当本轮对话无新增信息时，LLM 应输出“无有效增量”；若 LLM 超时或失败，引擎必须降级写入“无有效增量”摘要，保证系统不崩溃。 | `ca/__init__.py` - `_run_c_stage` / `_call_llm_for_l1` | ✅ 满足 |
| REQ-FUNC-CSTAGE-003 | L0 原文提取：从清洗后的 L1 JSON 中提取 `core_change` 字段的前 100 个字符作为 L0 文本。 | `ca/__init__.py` - `_extract_l0` | ✅ 满足 |
| REQ-FUNC-CSTAGE-004 | 按需计算优化：C‑stage 不执行全量余弦 Top‑3 升级计算；该计算移至 A‑stage 且仅在预算允许时触发。 | `ca/__init__.py` - `assemble` (仅 A‑stage 调用检索) | ✅ 满足 |
| REQ-FUNC-CSTAGE-005 | 防覆盖索引：`turn_index` 的计算公式为 `max(内部计数器+1, 历史用户消息数)`，避免重启后主键冲突。 | `ca/__init__.py` - `process_turn_async` | ✅ 满足 |
| REQ-FUNC-CSTAGE-009 | 自适应历史轮次：当传入 `history` 时，`turn_index` 应同时考虑已有用户消息数，确保索引不重复且单调递增。 | `ca/__init__.py` - `process_turn_async` | ✅ 满足 |

#### 2.1.2 A‑stage 同步组装

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-ASTAGE-001 | Head/Middle/Tail 分层：Tail 区受 `CA_PROTECT_TAIL_TOKENS` 保护；Head 区填充有效 L1 摘要；Middle 区根据升级标记注入 L1 或 L0。 | `ca/__init__.py` - `assemble` / `_compute_tail_start` / `_build_final_messages` | ✅ 满足 |
| REQ-FUNC-ASTAGE-002 | 双路检索与 RRF 融合：同时使用 BM25 和语义向量检索，结果经 RRF 融合排序；若嵌入服务不可用，自动降级为纯 BM25 检索。 | `ca/retrieval.py` - `Retriever.retrieve` | ✅ 满足 |
| REQ-FUNC-ASTAGE-003 | 动态预算闸门：可用升级预算 = `context_length * 0.95 - (Head 占用 + Tail 保护)`，预算 ≤ 0 时跳过检索。 | `ca/__init__.py` - `_available_budget` | ✅ 满足 |
| REQ-FUNC-ASTAGE-004 | 预算耗尽快速短路：当预算 ≤ 0 时，不调用 `Retriever.retrieve`，直接进入组装。 | `ca/__init__.py` - `assemble` (条件判断) | ✅ 满足 |
| REQ-FUNC-ASTAGE-005 | 无效摘要过滤：`core_change` 为“无有效增量”的 L1 摘要视为无效，组装时回退显示原始消息或 L0。 | `ca/__init__.py` - `_is_valid_summary` 及组装逻辑 | ✅ 满足 |
| REQ-FUNC-ASTAGE-006 | 统一 Token 估算：优先使用 CJK 字符比例估算（中文字符占比 > 50% 时按字符数 ×1.5），空文本返回 0；在 tiktoken 不可用的情况下以此作为保守估计。 | `ca/__init__.py` - `_token_estimate` | ✅ 满足 |
| REQ-FUNC-ASTAGE-007 | 硬截断兜底：当消息超过上下文的 95% 且无缓存时，执行硬截断，优先保留系统消息和尾部消息。 | `ca/__init__.py` - `_hard_truncation` | ✅ 满足 |
| REQ-FUNC-ASTAGE-008 | 工具组完整性保护：含 `tool_calls` 的 assistant 消息及其紧随的 tool 消息视为不可分割的整体，硬截断时整体保留或移除。 | `ca/__init__.py` - `_hard_truncation` | ✅ 满足 |
| REQ-FUNC-ASTAGE-009 | 截断提示规范化：当工具组被移除时，在原位置插入 `role: assistant` 提示消息，告知用户部分结果已省略。 | `ca/__init__.py` - `_hard_truncation` | ✅ 满足 |
| REQ-FUNC-ASTAGE-010 | 预算边界处理：当 `context_length * 0.95` 恰好等于 Head + Tail 占用时，预算应为 0；小于时预算为负数，触发短路跳过检索。 | `ca/__init__.py` - `_available_budget` | ✅ 满足 |

#### 2.1.3 全指纹去重

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-DEDUP-001 | 系统消息豁免：`role: system` 的消息无条件保留，不参与去重。 | `ca/__init__.py` - `_deduplicate_messages` | ✅ 满足 |
| REQ-FUNC-DEDUP-002 | 深度规范化指纹：计算消息指纹前，递归排序字典键、保持列表顺序、解析字符串化的 JSON、将 None 替换为空字符串。 | `ca/__init__.py` - `_deep_normalize` | ✅ 满足 |
| REQ-FUNC-DEDUP-003 | 跨角色防误杀：指纹计算必须包含 `role` 字段，避免不同角色但相同内容的合法消息被误删。 | `ca/__init__.py` - `_deduplicate_messages` (整体规范化) | ✅ 满足 |
| REQ-FUNC-DEDUP-004 | 时序保持：去重后消息的原始相对顺序不变，仅移除重复项。 | `ca/__init__.py` - `_deduplicate_messages` | ✅ 满足 |
| REQ-FUNC-DEDUP-005 | 配置与 Fail‑Safe 降级：通过 `CA_DEDUP_ENABLED` 环境变量控制，非法值时强制回退为禁用（False）并记录 WARNING 日志。 | `ca/config.py` - `_parse_bool_env` | ✅ 满足 |
| REQ-FUNC-DEDUP-006 | 调试可观测性：当 `CA_DEBUG=1` 时，去重完成后输出去重前后的消息数量。 | `ca/__init__.py` - `_deduplicate_messages` | ✅ 满足 |

#### 2.1.4 存储层

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-STORE-001 | 持久化：会话数据写入 SQLite 后，重启引擎能够完全恢复。 | `ca/store.py` - `SQLiteStore` | ✅ 满足 |
| REQ-FUNC-STORE-002 | 并发安全：多线程写入不同 `turn_index` 不会导致数据损坏；同 `turn_index` 并发写入时由唯一约束保证仅一条成功。 | `ca/store.py` - 事务 + 锁 | ✅ 满足 |
| REQ-FUNC-STORE-003 | WAL 模式：默认开启 WAL 模式，启动时校验是否生效；后台定期执行 PASSIVE checkpoint，WAL 文件大小可控。 | `ca/store.py` - `_start_checkpoint_daemon` | ✅ 满足 |
| REQ-FUNC-STORE-004 | 写入重试：遇到数据库锁时，按指数退避（50ms→100ms→200ms）重试最多 3 次。 | `ca/store.py` - `write_turn` | ✅ 满足 |
| REQ-FUNC-STORE-005 | 最大轮次查询：`max_turn_index` 空库返回 -1，正常返回最大索引值。 | `ca/store.py` - `max_turn_index` | ✅ 满足 |
| REQ-FUNC-STORE-006 | 数据老化清理：支持清理超过指定天数的旧会话数据。 | `ca/store.py` - `delete_session`（手动调用）| ⚠️ 需补充自动清理调度 |
| REQ-FUNC-STORE-007 | 会话列表缓存：`list_session_ids` 在 24 小时内返回缓存结果，写入后缓存失效。 | `ca/store.py` - `_session_cache` | ✅ 满足 |

#### 2.1.5 嵌入服务

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-EMBED-001 | 多后端支持：可切换 Ollama、sentence-transformers 或 fallback 后端，均返回合法向量。 | `ca/embedding.py` | ✅ 满足 |
| REQ-FUNC-EMBED-002 | LRU 缓存：对相同文本的嵌入结果缓存，最大容量 256，命中率期望 > 80%。 | `ca/embedding.py` - `_encode_cached` | ✅ 满足 |
| REQ-FUNC-EMBED-003 | 并行编码：支持批量并行编码，超时 15 秒，失败项降级 fallback。 | `ca/embedding.py` - `embed_batch_parallel` | ✅ 满足 |
| REQ-FUNC-EMBED-004 | 降级回退：Ollama 不可用时自动降级为 fallback 伪向量，且不缓存该伪向量。 | `ca/embedding.py` - `_embed_fallback` | ✅ 满足 |
| REQ-FUNC-EMBED-005 | 动态维度检测：首次成功调用 Ollama 时检测真实维度，fallback 向量使用相同维度。 | `ca/embedding.py` - `_detect_dimension` | ✅ 满足 |

#### 2.1.6 配置管理

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-CONFIG-001 | 环境变量覆盖：所有配置项均支持通过环境变量覆盖默认值。 | `ca/config.py` | ✅ 满足 |
| REQ-FUNC-CONFIG-002 | 校验：非法值（如负数超时）在 `validate()` 时抛出 `ValueError`。 | `ca/config.py` - `validate` | ✅ 满足 |
| REQ-FUNC-CONFIG-003 | 热重载：运行时调用 `Config.reload()` 可更新配置，失败时保留原值并记录 CRITICAL 日志。 | `ca/config.py` - `reload` | ✅ 满足 |
| REQ-FUNC-CONFIG-004 | 调试开关：`CA_DEBUG=1` 开启后输出详细日志（去重统计、阶段统计等）。 | `ca/config.py` / 各模块 | ✅ 满足 |
| REQ-FUNC-CONFIG-005 | Fail‑Safe 降级：布尔型配置非法值（如 `CA_DEDUP_ENABLED=INVALID`）强制回退为安全值（False）并告警。 | `ca/config.py` - `_parse_bool_env` | ✅ 满足 |

#### 2.1.7 健康检查与监控

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-MONITOR-001 | 健康检查端点：`check_all` 返回 store 和 embedding 的状态。 | `ca/health.py` | ✅ 满足 |
| REQ-FUNC-MONITOR-002 | Prometheus 指标：输出 `ca_store_healthy`、`ca_embedding_cache_hit_rate` 等指标。 | `ca/health.py` | ✅ 满足 |
| REQ-FUNC-MONITOR-003 | 阶段统计：A‑stage 各阶段耗时、升级数量等记录在 `CompressStats` 中，调试时可输出。 | `ca/stats.py` 及 `assemble` 集成 | ✅ 满足 |

#### 2.1.8 断路器

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-CIRCUIT-001 | 故障计数持久化：失败次数写入状态文件 `~/.hermes/.ca_assembler_state_.json`。 | `plugins/.../ca_assembler/__init__.py` | ✅ 满足 |
| REQ-FUNC-CIRCUIT-002 | 断路器触发：连续失败 3 次后，`is_available()` 返回 False。 | 同上 | ✅ 满足 |
| REQ-FUNC-CIRCUIT-003 | 自动恢复：冷却期（1 小时）结束后，`is_available()` 重新返回 True。 | 同上 | ✅ 满足 |
| REQ-FUNC-CIRCUIT-004 | 成功重置：成功后调用 `_record_success()` 将 failures 清零。 | 同上 | ✅ 满足 |
| REQ-FUNC-CIRCUIT-005 | 进程隔离：不同 PID 的状态文件独立，互不影响；过期文件（>7天）在 session 启动时清理。 | 同上 | ✅ 满足 |

#### 2.1.9 资源管理与生命周期

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-RESET-001 | 资源清理：`reset()` 关闭 store、等待后台任务、清空缓存和计数器，且不泄漏文件描述符（FD），不影响断路器状态。 | `ca/__init__.py` - `reset` | ✅ 满足 |
| REQ-FUNC-SESSION-001 | 多会话隔离：不同 session_id 的数据在存储和缓存中完全隔离。 | `SQLiteStore` + `SessionManager` | ✅ 满足 |

#### 2.1.10 接口适配（Plugin 层）

| 需求ID | 需求描述 | 实现位置 | 状态 |
|--------|----------|----------|------|
| REQ-FUNC-INTF-001 | `should_compress` 固定返回 False（由外部决策）。 | 插件层 | ✅ 满足 |
| REQ-FUNC-INTF-002 | `compress()` 触发 A‑stage 组装，必要时执行硬截断。 | 插件层 `pre_llm_call` | ✅ 满足 |
| REQ-FUNC-INTF-003 | `get_status()` 返回 token 使用量和引擎状态信息。 | 已预留扩展 | ⚠️ 暂未独立实现 |

### 2.2 非功能需求

| 需求ID | 类别 | 描述 | 验证方式 | 状态 |
|--------|------|------|----------|------|
| REQ-NFR-PERF-001 | 性能 | A‑stage 同步组装 p95 延迟 < 200ms（50轮历史，fallback embedding） | 性能测试 TC-A-001，硬件基线自动记录，支持动态阈值调整 | ⚠️ 需目标环境验证 |
| REQ-NFR-PERF-002 | 性能 | C‑stage 异步提交阻塞时间 < 50ms | 性能测试 TC-C-001 | ✅ 满足 |
| REQ-NFR-RELI-001 | 可靠性 | 嵌入失败 A‑stage 不中断，降级纯 BM25 | 异常测试 TC-A-010, TC-RELI-004 | ✅ 满足 |
| REQ-NFR-RELI-002 | 可靠性 | 磁盘满时写入不崩溃，返回 False | 异常测试 TC-S-006 | ✅ 满足 |
| REQ-NFR-MAINT-001 | 可维护性 | 公开函数 docstring 覆盖率 100% | 静态检查 TC-MAINT-001，CI lint 自动扫描 | ⚠️ 需 lint 集成 |
| REQ-NFR-MAINT-002 | 可维护性 | 热重置不抛异常，可连续调用 | 测试 TC-MAINT-004 | ✅ 满足 |
| REQ-NFR-CONC-001 | 并发 | 高并发下 C/A‑stage 交替执行不出现数据竞争或异常，10+ 线程持续运行 10 分钟无崩溃 | 压力测试 TC-CONC-003 | 新增 |

---

## 3. 测试计划

### 3.1 测试策略

测试采用 **AI Agent 驱动的全自动化金字塔** 结构，消除所有人工干预步骤。关键策略包括：

- **单元测试**：对纯函数使用 `pytest` + `unittest.mock` 覆盖所有边界条件。
- **集成测试**：利用 mock 模拟 LLM、嵌入服务、SQLite 等外部依赖，构造异常场景并验证降级行为。
- **并发与压力测试**：通过 `threading.Event` / `Barrier` 精确复现读写竞争，全自动执行。
- **质量评估 (TC-QUAL-*) **：采用分层执行策略：
  - **L1 快速门禁**（每次 CI 触发）：基于脚本规则/统计的评估（TC-QUAL-002 压缩率、TC-QUAL-005 ROUGE-L、TC-QUAL-003 余弦相似度），无需 LLM Judge，算力开销极小。
  - **L2 深度评估**（Nightly/预发布）：基于 LLM‑as‑a‑Judge 的评估（TC-QUAL-001 BERTScore、TC-QUAL-004 幻觉率、TC-QUAL-006 实体遗漏率、TC-QUAL-007 语义遗漏率），仅在有 GPU 配额时执行，否则标记为“条件跳过”。
- **性能基线**：每次 CI 自动记录硬件指纹，在监控脚本中支持部署后动态校准基线，防止因环境变更导致误报。
- **文件描述符泄漏检查**：在资源清理测试中，验证连续多次 `reset()` 后 FD 数量无持续增长。

整体分为 7 个批次，共 **100 个用例**（全部 ready，质量用例分层后均可在自动化流水线中按计划执行）。

### 3.2 测试用例汇总

#### 第一批：C‑stage 用例 (16，含 TC-C-014)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-C-001 | REQ-FUNC-CSTAGE-001 | 异步执行不阻塞主线程 | High | Functional | ready |
| TC-C-002 | REQ-FUNC-CSTAGE-002 | 增量提取无新增信息 | High | Functional | ready |
| TC-C-003 | REQ-FUNC-CSTAGE-003 | 结构化解析 JSON | High | Functional | ready |
| TC-C-004a | REQ-FUNC-CSTAGE-004 | 语义去重（>0.88） | Medium | Functional | ready |
| TC-C-004b | REQ-FUNC-CSTAGE-004 | 语义去重（=0.88） | Medium | Functional | ready |
| TC-C-004c | REQ-FUNC-CSTAGE-004 | 语义去重（<0.88） | Medium | Functional | ready |
| TC-C-004d | REQ-FUNC-CSTAGE-004 | 阈值配置变更 | Medium | Functional | ready |
| TC-C-005 | REQ-FUNC-CSTAGE-005 | 容错解析 95% | High | Functional | ready |
| TC-C-006 | REQ-FUNC-CSTAGE-002 | LLM 连接失败降级 | High | Exception | ready |
| TC-C-006a | REQ-FUNC-CSTAGE-002 | LLM 超时降级 | High | Exception | ready |
| TC-C-007 | REQ-FUNC-CSTAGE-001 | 重复提交防护 | Medium | Functional | ready |
| TC-C-008 | REQ-FUNC-CSTAGE-009 | 计数器恢复 | High | Functional | ready |
| TC-C-009 | REQ-FUNC-INTF-002 | on_session_end 等待 | High | Functional | ready |
| TC-C-009a | REQ-FUNC-CSTAGE-009 | turn_index 自适应历史轮次 | High | Functional | ready |
| TC-C-010 | REQ-FUNC-CSTAGE-004 | C-stage 不执行全量 Cosine top-3 | Medium | Functional | ready |
| **TC-C-014** | REQ-FUNC-CSTAGE-002 | LLM 返回超长字符串（1MB）时缓冲区保护与日志截断 | High | Exception | **新增** |

#### 第二批：A‑stage 用例 (21)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-A-001 | REQ-NFR-PERF-001 | A-stage p95 < 200ms（硬件基线自动记录） | High | Performance | ready |
| TC-A-002 | REQ-FUNC-ASTAGE-001 | Head/Middle/Tail 分层 | High | Functional | ready |
| TC-A-002a | REQ-FUNC-ASTAGE-001 | 按 turn_index 映射摘要 | High | Functional | ready |
| TC-A-003 | REQ-FUNC-ASTAGE-002 | 双路检索 | Medium | Functional | ready |
| TC-A-004 | REQ-FUNC-ASTAGE-002 | RRF 融合稳定 | Medium | Functional | ready |
| TC-A-005 | REQ-FUNC-ASTAGE-003 | 预算闸门 | Medium | Functional | ready |
| TC-A-006 | REQ-FUNC-ASTAGE-001 | [~/N] 标记 | High | Functional | ready |
| TC-A-007 | REQ-FUNC-ASTAGE-001 | 缓存空降级 | High | Exception | ready |
| TC-A-008 | REQ-FUNC-ASTAGE-007 | 溢出截断 | High | Functional | ready |
| TC-A-008a | REQ-FUNC-ASTAGE-007 | 95% 边界不截断 | Medium | Functional | ready |
| TC-A-009 | REQ-FUNC-ASTAGE-008 | 工具轮次保护 | High | Functional | ready |
| TC-A-010 | REQ-NFR-RELI-001 | 嵌入失败 BM25 独立降级 | High | Exception | ready |
| TC-A-011 | REQ-FUNC-ASTAGE-005 | 无效摘要过滤 | High | Functional | ready |
| TC-A-012 | REQ-FUNC-ASTAGE-005 | 无效摘要递补 | High | Functional | ready |
| TC-A-013 | REQ-FUNC-ASTAGE-004 | 预算耗尽快速短路 | High | Functional | ready |
| TC-A-014 | REQ-FUNC-ASTAGE-008 | 硬截断工具组完整性保护 | High | Functional | ready |
| TC-A-015 | REQ-FUNC-ASTAGE-009 | 截断提示消息 role 为 assistant | Medium | Functional | ready |
| TC-A-016 | REQ-FUNC-ASTAGE-006 | Token 估算 CJK 1.5 系数 | Medium | Functional | ready |
| TC-A-017 | REQ-FUNC-ASTAGE-006 | Token 估算空文本短路 | Low | Functional | ready |
| TC-A-018 | REQ-FUNC-ASTAGE-010 | 预算边界值：恰好等于 Head+Tail 时预算为 0 | High | Unit | ready |
| TC-A-019 | REQ-FUNC-ASTAGE-010 | 预算边界值：小于 Head+Tail 时预算为负，触发短路 | High | Unit | ready |

#### 第三批：存储层 + 嵌入服务 (15)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-S-001 | REQ-FUNC-STORE-001 | 持久化 | High | Functional | ready |
| TC-S-002 | REQ-FUNC-STORE-002 | 并发写入（不同 turn） | Medium | Concurrency | ready |
| TC-S-002a | REQ-FUNC-STORE-002 | 同 turn 冲突 | Medium | Concurrency | ready |
| TC-S-003 | REQ-FUNC-STORE-003 | WAL 大小 | Medium | Functional | ready |
| TC-S-004 | REQ-FUNC-STORE-004 | 写入重试退避时间验证 | Medium | Functional | ready |
| TC-S-005 | REQ-FUNC-STORE-005 | max_turn_index | Medium | Functional | ready |
| TC-S-006 | REQ-NFR-RELI-002 | 磁盘满错误 | High | Exception | ready |
| TC-S-007 | REQ-FUNC-STORE-002 | 原子写入事务 | Medium | Functional | ready |
| TC-S-008 | REQ-FUNC-STORE-006 | 数据老化清理 | Medium | Functional | ready |
| TC-S-009 | REQ-FUNC-STORE-007 | 会话列表缓存 | Low | Functional | ready |
| TC-E-001 | REQ-FUNC-EMBED-001 | 多后端支持 | High | Functional | ready |
| TC-E-002 | REQ-FUNC-EMBED-002 | LRU 缓存命中率 | Medium | Performance | ready |
| TC-E-003 | REQ-FUNC-EMBED-003 | 并行编码加速比 | Medium | Performance | ready |
| TC-E-004 | REQ-FUNC-EMBED-004 | 降级回退 | High | Exception | ready |
| TC-E-005 | REQ-FUNC-EMBED-005 | 动态维度检测 | High | Functional | ready |

#### 第四批：配置管理 + 健康检查 + 断路器 (15)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-CF-001 | REQ-FUNC-CONFIG-001 | 环境变量覆盖 | High | Functional | ready |
| TC-CF-002 | REQ-FUNC-CONFIG-002 | validate 校验 | Medium | Functional | ready |
| TC-CF-003 | REQ-FUNC-CONFIG-003 | 热重载 | Medium | Functional | ready |
| TC-CF-004 | REQ-FUNC-CONFIG-004 | 调试开关 | Medium | Functional | ready |
| TC-CF-005 | REQ-FUNC-CONFIG-004 | 非法配置 Fail-Safe | Medium | Functional | ready |
| TC-M-001 | REQ-FUNC-MONITOR-001 | check_all 健康检查 | High | Functional | ready |
| TC-M-002 | REQ-FUNC-MONITOR-002 | Prometheus 指标 | Medium | Functional | ready |
| TC-M-003 | REQ-FUNC-MONITOR-003 | 阶段统计 (CompressStats) | Medium | Functional | ready |
| TC-CB-001 | REQ-FUNC-CIRCUIT-001 | 故障计数持久化 | High | Functional | ready |
| TC-CB-002 | REQ-FUNC-CIRCUIT-002 | 断路器触发 | High | Functional | ready |
| TC-CB-003 | REQ-FUNC-CIRCUIT-003 | 自动恢复 | High | Functional | ready |
| TC-CB-004 | REQ-FUNC-CIRCUIT-004 | 成功重置 | Medium | Functional | ready |
| TC-CB-005 | REQ-FUNC-CIRCUIT-005 | 进程隔离 | Medium | Functional | ready |
| TC-CB-006 | REQ-FUNC-CIRCUIT-005 | 过期文件清理 | Low | Functional | ready |
| TC-CB-007 | 设计文档 12.1 | Plugin 层负责断路器，引擎不感知 | High | Functional | ready |

#### 第五批：资源/生命周期 + 非功能 + 接口 + 并发 (15)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-RESET-001 | REQ-FUNC-RESET-001 | 资源清理与状态重置（含 FD 泄漏检查） | Medium | Functional | ready |
| TC-RESET-002 | 设计文档 12.2 | reset 不重置断路器状态 | Medium | Functional | ready |
| TC-SESSION-001 | REQ-FUNC-SESSION-001 | 多会话数据隔离 | High | Functional | ready |
| TC-PERF-001 | REQ-NFR-PERF-002 | C-stage 后台任务完成时间 | Low | Performance | ready |
| TC-RELI-004 | REQ-NFR-RELI-001 | 嵌入失败 A-stage 不中断 | High | Reliability | ready |
| TC-MAINT-001 | REQ-NFR-MAINT-001 | 公开函数 docstring 覆盖率 100% | Medium | Maintainability | ready |
| TC-MAINT-004 | REQ-NFR-MAINT-002 | 热重置不抛异常 | Low | Maintainability | ready |
| TC-INTF-001 | REQ-FUNC-INTF-001 | should_compress 固定返回 False | High | Functional | ready |
| TC-INTF-002 | REQ-FUNC-INTF-002 | compress() 触发硬截断 | Medium | Functional | ready |
| TC-INTF-003 | REQ-FUNC-INTF-003 | get_status() 返回状态信息 | Medium | Functional | ready |
| TC-CONC-001 | REQ-FUNC-CSTAGE-001 | A/C-stage 并发无干扰 | High | Concurrency | ready |
| TC-CONC-001a | REQ-FUNC-CSTAGE-001 | C-stage 并发乱序不重复 turn_index | High | Concurrency | ready |
| TC-CONC-002 | REQ-FUNC-STORE-002 | 多会话并发操作隔离 | Medium | Concurrency | ready |
| TC-CONC-003 | REQ-NFR-CONC-001 | 高并发读写压力测试（AI Agent 控制时序） | **Critical** | Stress | ready |
| TC-CONC-004 | REQ-FUNC-CSTAGE-001 | C/A-stage 读写竞争（使用 threading.Event 强制复现） | High | Concurrency | ready |

#### 第六批：摘要质量与端到端系统 (10，分层 ready)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-QUAL-001 | 业务需求 | BERTScore 事实一致性（L2 Nightly 执行） | High | Quality | ready |
| TC-QUAL-002 | 业务需求 | 压缩率（L1 快速门禁） | High | Quality | ready |
| TC-QUAL-003 | 业务需求 | 语义余弦相似度（L1 快速门禁） | High | Quality | ready |
| TC-QUAL-004 | 业务需求 | 幻觉率检测（L2 Nightly 执行） | Medium | Quality | ready |
| TC-QUAL-005 | 业务需求 | ROUGE-L Recall（L1 快速门禁） | Medium | Quality | ready |
| TC-QUAL-006 | 业务需求 | 实体遗漏率（L2 Nightly 执行，数据来自历史脏案例） | High | Quality | ready |
| TC-QUAL-007 | 业务需求 | 语义遗漏率（L2 Nightly 执行） | High | Quality | ready |
| TC-ST-001 | 整体功能 | 正常对话流程端到端（mock 或真实 Ollama） | High | System | ready |
| TC-ST-002 | REQ-FUNC-CSTAGE-002 | LLM 不可用降级与恢复（AI Agent 自动启停容器） | High | System | ready |
| TC-ST-003 | REQ-FUNC-CIRCUIT | 断路器触发与恢复（脚本自动操作状态文件） | High | System | ready |

#### 第七批：降级质量与高级异常场景 (8)

| ID | 需求ID | 标题 | 优先级 | 类型 | 状态 |
|----|--------|------|--------|------|------|
| TC-DEGR-001 | REQ-FUNC-ASTAGE-002, REQ-NFR-RELI-001 | BM25 降级输出质量（自动化断言） | High | Quality | ready |
| TC-DEGR-002 | REQ-FUNC-EMBED-004 | Fallback 伪向量一致性（自动验证） | High | Quality | ready |
| TC-C-011 | REQ-FUNC-CSTAGE-002 | LLM 返回非 JSON 字符串解析容错（mock 自动注入） | High | Exception | ready |
| TC-C-012 | REQ-FUNC-CSTAGE-002 | LLM 返回空字符串降级（mock 自动注入） | High | Exception | ready |
| TC-C-013 | REQ-FUNC-CSTAGE-002 | LLM 返回缺少 core_change 字段的 JSON 解析（mock 自动注入） | High | Exception | ready |
| TC-S-010 | REQ-FUNC-STORE-004 | SQLite “database is locked” 重试验证（CI Linux，本地跳过） | Medium | Exception | ready |
| TC-CF-006 | REQ-FUNC-CONFIG-003 | 热重载并发安全（自动化并发脚本，验证生效时机） | Medium | Concurrency | ready |

---

## 4. AI Agent 驱动的自动化测试实现细节

### 4.1 质量评估自动化分层

- **L1 快速门禁**（每次 PR 触发）：TC-QUAL-002（压缩率）、TC-QUAL-003（余弦相似度）、TC-QUAL-005（ROUGE-L）使用纯 Python 脚本（jieba + 简单统计 / Sentence-Transformers 余弦计算）完成，无需 LLM，可在 CPU 环境 10 秒内完成。
- **L2 深度评估**（Nightly 或 Release Pipeline）：TC-QUAL-001/004/006/007 需调用本地 LLM Judge。流水线启动前检查 GPU 可用性（`nvidia-smi` 或 `torch.cuda.is_available()`），若无 GPU 则自动跳过并记录日志，避免消耗 CPU 时间。
- **数据生成**：实体遗漏率 (TC-QUAL-006) 所需测试样本来自项目 Git 历史中修复过的脏数据案例，自动转换格式，无需人工标注。

### 4.2 并发竞争精确复现

- **TC-CONC-004**：在 C‑stage 更新 `self.cache._snapshot` 前插入 `threading.Event.wait()`，A‑stage 的读取线程在获取快照前触发 `Event.set()`，100% 复现竞争。
- **TC-CF-006**：一个线程循环 `assemble`，另一个线程随机时间 `reload()`。`assemble` 内部记录开始时配置快照，结束后断言未变化；下一次 `assemble` 断言新配置生效。

### 4.3 性能测试硬件基线

- **TC-A-001**：使用 `pytest-benchmark`，通过 `platform` 和 `psutil` 记录 CPU 型号/频率/核心数/内存。CI 将性能数据与硬件指纹存储为 artifact。监控脚本（如 Prometheus + Grafana）可按硬件指纹维护动态阈值，部署后自动运行基准测试校准。

### 4.4 SQLite 锁定重试 OS 自适应

- **TC-S-010**：测试文件中使用 `pytest.mark.skipif(sys.platform != 'linux', reason='SQLite lock behavior OS-dependent, verified in CI')` 仅在 Linux 环境执行。开发人员本地运行时若跳过，属正常现象。

### 4.5 资源泄漏检测

- **TC-RESET-001**：新增步骤——记录进程初始 FD 数量（`psutil.Process().num_fds()` 或 `/proc/self/fd`），连续调用 `reset()` 100 次，最终 FD 数量 ≤ 初始 +5。

### 4.6 超长字符串防护

- **TC-C-014**：mock LLM 返回 1MB 随机字符串。验证 `_run_c_stage` 不崩溃，日志输出被截断（检查日志中无完整 1MB 内容），降级摘要正确写入“无有效增量”。

---

## 5. 需求追溯矩阵

每个需求 ID 至少对应一个已自动化的测试用例，完整映射可由前表导出，此处不再枚举。

---

## 6. 测试环境与工具

- **单元/集成**：Python 3.10+，`pytest`，`unittest.mock`，`freezegun`，`threading` 同步原语
- **AI 质量评估**：本地 LLM（Qwen3.5 等）作为 Judge（仅 Nightly），`spaCy` / `HanLP` 用于 NER，`bert-score`、`rouge-score`、`jieba`
- **并发压力**：`pytest-timeout`，`pytest-benchmark`，CI 多核 Linux
- **性能基线**：`pytest-benchmark` + `platform` + `psutil`；监控脚本动态校准
- **数据库**：SQLite WAL，OS 自适应 lock 模拟，CI 统一 Linux 执行
- **容器化**：Docker Compose 管理 Ollama（可选），mock 模式亦可
- **CI/CD**：GitHub Actions / GitLab CI，Linux runner

---

## 7. 风险评估与缓解

| 风险点 | 自动化缓解方案 | 执行层级 |
|--------|----------------|----------|
| 第六批质量测试算力开销 | L1 脚本评估（每次CI）；L2 LLM评估（Nightly，GPU可选） | 分层 |
| TC-S-010 OS 兼容性 | `pytest.skipif` 仅在 Linux CI 执行，开发规范注明 | 开发流程 |
| 性能基线漂移 | 部署后自动基准测试，动态阈值（Prometheus + 脚本） | 监控 |
| 文件描述符泄漏 | TC-RESET-001 增加 FD 计数断言 | 测试 |
| 超长 LLM 输出导致 OOM | TC-C-014 验证截断与降级 | 测试 |

---

## 8. 附录

### 8.1 环境变量速查表

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CA_DEBUG` | 0 | 调试日志开关 |
| `CA_DB_MAX_RETRY` | 3 | 数据库写入重试次数 |
| `CA_PROTECT_TAIL_TOKENS` | 20000 | 尾部保护 token 数 |
| `CA_HEAD_AUTO_L1_COUNT` | 3 | Head 摘要数量 |
| `CA_CONTEXT_LENGTH` | 32000 | 最大上下文长度 |
| `CA_OODA_DEDUP_THRESHOLD` | 0.88 | 去重相似度阈值 |
| `CA_DEDUP_ENABLED` | True | 全指纹去重开关 |
| `CA_EMBED_BACKEND` | ollama | 嵌入后端 |
| `CA_LLM_TIMEOUT` | 120 | LLM 超时（秒） |
| `CA_LLM_NUM_PREDICT` | 24768 | LLM 最大生成 token 数 |

### 8.2 测试执行顺序

1. 第一批～第四批（66 个）
2. 第五批（15 个）
3. 第七批（8 个）
4. 并发压力 TC-CONC-003（独立阶段）
5. 第六批 L1 快速门禁（3 个，与其他批次并行）
6. 第六批 L2 深度评估（4 个，Nightly/Pre-release 执行）
7. 端到端系统测试（3 个，可并行）

全量 **100 个用例**，每次 CI 核心流水线约执行 95 个（L2 可能跳过），预估耗时 25 分钟（不含 LLM Judge 计算）。

---

**文档结束**

*本最终版吸收全部执行建议，实现零人工干预的自动化测试闭环，并针对生产环境风险（算力、OS差异、基线漂移、资源泄漏）提供分层缓解策略。*

# ContextAssembler v4.4.0 补充测试需求与计划（最终版）

## 文档信息
| 项目 | 内容 |
|------|------|
| 文档版本 | v1.1（基于SRS v4.4.0最终实现及多轮审查修正） |
| 对应需求 | ContextAssembler SRS v4.4.0 |
| 编制日期 | 2025-06-30 |
| 存放位置 | `tests/testcases/ContextAssembler_testcases_v4.4.0_supplement.json` |

## 1. 引言
本文档定义 ContextAssembler v4.4.0 相对于 v4.3.4 的**补充测试需求与测试用例**，覆盖新增的**工具轮摘要**、**L‑stage异步补全**、**预选与拣选**、**去重Fail‑Safe修正**、**阶段统计重命名**及**新增配置项**等特性。

所有用例均基于最终代码实现（22个澄清问题的答复）编写，经过多轮审查修正，确保预期断言与代码行为严格一致。

## 2. 补充测试需求
| 需求 ID | 需求描述 | 验证重点 |
|---------|----------|----------|
| REQ-TOOL-C001~C008 | 工具轮识别、规则生成、错误处理、字段优先级、存储与索引、无条件生成、容错 | `ToolSummarizer`行为、`turn_cache`新列、`AssemblyCache`工具轮字典 |
| REQ-C-ERROR | C‑stage降级标识`_assemble_status=1` | 降级摘要的状态字段 |
| REQ-TOOL-P001~P006 | 预选、Tail保护、兜底L1、降级顺序、RRF排序、升级上限 | `_pre_upgraded_tool_turns`、`_compute_layers_v2`、`_select_upgrades` |
| REQ-L-STAGE-001~007 | 对话/工具轮补全、数据源、失败重试、定时扫描、超时放弃 | `BackfillThread`行为、`l2_text`、`backfill_attempts`、`_assemble_status`变化 |
| REQ-FUNC-DEDUP-005(修正) | 非法配置值强制启用去重 | `_parse_bool_env`强制回退 |
| REQ-FUNC-ASTAGE-003(增强) | 预算计算精确化（系统消息、工具组整体Token） | `_available_budget`使用`idx_to_turn`映射 |
| REQ-REL-005 | 引擎销毁顺序 | 等待C/L-stage完成再释放资源 |
| REQ-OBS-002(变更) | `AssembleStats`重命名及新增工具轮统计 | 类名、字段`tool_upgrade_count`等 |
| 新增配置项 | 8个工具轮/补全相关配置 | 默认值与校验、越界钳位 |
| 线程安全设计 | 快照隔离、组装期间竞争 | A-stage与L-stage并发一致性 |

## 3. 测试用例清单（46个）
所有用例均为`ready`状态，优先级为High/Medium/Low，类型覆盖Functional/Exception/Performance/Concurrency。

详细定义如下（完整JSON）：

```json
[
  {
    "id": "TC-C-015",
    "req": "REQ-TOOL-C001",
    "title": "C-stage 识别并拆分多个工具调用生成独立摘要",
    "preconditions": "ca_engine fixture，mock LLM 正常返回对话摘要",
    "steps": [
      "构造 messages 包含一个 assistant 消息带有两个 tool_calls（id 分别为 call_1, call_2），后跟两个 role=tool 的响应（tool_call_id 分别匹配）",
      "调用 process_turn_async",
      "调用 engine.wait_for_pending() 等待 C-stage 完成",
      "查询 turn_cache 表中 turn_type='tool' 且 turn_index 为当前轮次的记录，按 tool_sub_index 排序"
    ],
    "expected": "存在 2 条 tool 记录；tool_sub_index 分别为 1 和 2；l1_text 非空且可解析为 JSON；l0_text 非空；turn_type 均为 'tool'",
    "cleanup": "engine.store.close()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-016",
    "req": "REQ-TOOL-C002",
    "title": "工具轮规则生成 L1 包含所有必须字段",
    "preconditions": "mock ToolSummarizer.summarize_group 返回正常 L1 JSON",
    "steps": [
      "模拟一个完整的工具调用组（assistant 含 content='思考过程...' 及一个 tool_call），工具返回 {'result': '操作成功', 'summary': '简要结果'}",
      "触发 C-stage 工具轮摘要生成",
      "从 turn_cache 获取该工具轮的 l1_text",
      "解析 JSON 并检查 tool_name, tool_args, thought_process, result_summary, error, implicit_knowledge, next_action_hint 七个字段"
    ],
    "expected": "tool_name='test_tool'；tool_args 为 JSON 对象（非空）；thought_process='思考过程...'；result_summary 非空；error=null；implicit_knowledge=[]（单工具调用）；next_action_hint=''",
    "cleanup": "engine.wait_for_pending()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-017",
    "req": "REQ-TOOL-C003",
    "title": "工具调用失败时 L1 正确标记 error 且 result_summary 为失败",
    "preconditions": "mock 工具返回包含 error 字段的响应",
    "steps": [
      "工具返回 {'error': 'timeout'}",
      "调用 ToolSummarizer.summarize_group 生成摘要",
      "检查返回的 L1 JSON 的 error 和 result_summary 字段"
    ],
    "expected": "L1.error='timeout'；L1.result_summary='失败'；其他字段仍正确填充（tool_name 等）",
    "cleanup": "无",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-018",
    "req": "REQ-TOOL-C004",
    "title": "工具轮 L0 截断至 100 字符且格式正确",
    "preconditions": "mock 工具返回长结果，tool_name='long_tool'，result_summary 为超过 100 字符的字符串",
    "steps": [
      "调用 ToolSummarizer 生成 L0",
      "检查 L0 长度和内容格式"
    ],
    "expected": "L0 长度 ≤ 100；格式为 '[long_tool]: <result_summary 截断>'；若 tool_name+result_summary 本身就超过 100，则整体截断至 100",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-019",
    "req": "REQ-TOOL-C005",
    "title": "字段优先级处理：VIP 保留，P0 截断，P1 仅名称，P2 丢弃",
    "preconditions": "配置字段优先级 YAML（或使用默认 Profile），工具返回包含各级别字段",
    "steps": [
      "工具返回 JSON：{'vip_field': '重要内容'*50, 'p0_field': '中等内容'*50, 'p1_field': '次要内容', 'p2_field': '冗余信息'}",
      "调用 ToolSummarizer 生成摘要",
      "检查 L1 JSON 中各字段的处理结果"
    ],
    "expected": "VIP 字段内容完整保留（不截断）；P0 字段被截断（保留头尾，总长 ≤120 字符）；P1 字段仅显示字段名，不显示内容；P2 字段完全不出现",
    "cleanup": "恢复默认 Profile 配置",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-020",
    "req": "REQ-TOOL-C006",
    "title": "工具轮摘要存入 turn_cache 并更新 AssemblyCache 工具轮字典",
    "preconditions": "ca_engine fixture，已有历史对话轮数据",
    "steps": [
      "触发一个工具轮摘要生成并完成 C-stage",
      "通过 store 按复合主键 (session_id, turn_index, 'tool', tool_sub_index) 查询记录",
      "调用 cache.get_tool_snapshot_data() 获取工具轮数据"
    ],
    "expected": "store 中存在该工具轮记录，turn_type='tool'，tool_sub_index≥1，l1_text 和 l0_text 非空，l2_text 为工具组消息的 JSON 序列化；cache 返回的 dict 中包含该复合键对应的 L1/L0",
    "cleanup": "engine.wait_for_pending()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-021",
    "req": "REQ-TOOL-C008",
    "title": "工具返回无法解析 JSON 时生成降级摘要",
    "preconditions": "mock 工具返回非法的 JSON 字符串（如截断的响应）",
    "steps": [
      "工具返回 '{\"result\": \"incomplete'（缺少右括号）",
      "调用 ToolSummarizer 生成摘要",
      "检查 L1 的 result_summary 和 error 字段"
    ],
    "expected": "result_summary='无法解析'；error 包含原始异常信息（如 JSONDecodeError 详情）；不包含 _c_error 标记；tool_sub_index 仍正常分配",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Exception",
    "status": "ready"
  },
  {
    "id": "TC-C-022",
    "req": "REQ-TOOL-P001",
    "title": "C-stage 预选：以对话 L1 为 Query 在工具轮索引中 BM25 检索",
    "preconditions": "历史中已有 10 条工具轮摘要（BM25 索引已构建），当前对话 L1 的 core_change 文本与其中 2 条工具轮的 L0 高度相关",
    "steps": [
      "完成 C-stage 对话轮摘要生成",
      "检查 ca_engine._pre_upgraded_tool_turns 集合的内容"
    ],
    "expected": "_pre_upgraded_tool_turns 非空；集合大小 ≤ CA_TOOL_PRE_UPGRADE_COUNT（默认 3）；内容为 (turn_index, sub_index) 复合键",
    "cleanup": "engine.wait_for_pending()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-023",
    "req": "REQ-C-ERROR",
    "title": "LLM 降级时对话轮 _assemble_status 置为 1",
    "preconditions": "mock _call_llm_for_l1 返回以'核心摘要：无有效增量'开头的降级文本",
    "steps": [
      "触发 C-stage 对话轮生成",
      "从 turn_cache 查询该对话轮的 _assemble_status 和 l1_text"
    ],
    "expected": "_assemble_status=1（待补全）；l1_text 包含完整的五节降级摘要（core_change 为'无有效增量'等）",
    "cleanup": "engine.wait_for_pending()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-024",
    "req": "REQ-C-ERROR",
    "title": "LLM 正常时对话轮 _assemble_status 为 0",
    "preconditions": "mock _call_llm_for_l1 返回正常 OODA 文本（非降级特征）",
    "steps": [
      "触发 C-stage 对话轮生成",
      "从 turn_cache 查询 _assemble_status"
    ],
    "expected": "_assemble_status=0；l1_text 包含有效的 core_change",
    "cleanup": "engine.wait_for_pending()",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-025",
    "req": "REQ-TOOL-C007",
    "title": "无条件摘要生成：组合关闭关键开关后工具轮摘要仍写入",
    "preconditions": "设置 CA_DEBUG=0, CA_DEDUP_ENABLED=0；mock 工具调用正常返回",
    "steps": [
      "触发一轮含工具调用的对话",
      "等待 C-stage 完成",
      "查询 turn_cache 中 turn_type='tool' 的记录"
    ],
    "expected": "工具轮记录存在，l1_text 和 l0_text 非空；摘要生成不受任何外部开关影响",
    "cleanup": "恢复默认配置",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-C-026",
    "req": "REQ-TOOL-C005",
    "title": "硬编码默认优先级回退及未知字段丢弃验证",
    "preconditions": "不设置 CA_TOOL_FIELD_PRIORITY_PROFILE；工具返回包含已知字段（error, result）和未知字段 xyz_unknown_field",
    "steps": [
      "调用 ToolSummarizer 生成摘要",
      "检查 L1 JSON 内容"
    ],
    "expected": "error 字段被 VIP 保留；result 被 P0 截断；xyz_unknown_field 不出现（P2 丢弃）；默认优先级行为正确",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-020",
    "req": "REQ-TOOL-P002",
    "title": "A-stage Tail 保护：工具轮在 Tail 窗口内保留原文",
    "preconditions": "构造 messages 使得某工具组（含 tool_calls 和 role=tool 响应）位于 PROTECT_TAIL_TOKENS 保护区内",
    "steps": [
      "调用 assemble",
      "在返回的消息列表中定位该工具组",
      "检查工具组内消息的 content 是否保持原文"
    ],
    "expected": "工具组内所有消息（assistant 的 tool_calls 和后续 tool 响应）未被摘要替换，content 与原始输入一致",
    "cleanup": "engine.store.close()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-021",
    "req": "REQ-TOOL-P003",
    "title": "预选工具轮在 Head/Middle 区域至少以 L1 替换（兜底保障）",
    "preconditions": "C-stage 已预选某工具轮（_pre_upgraded_tool_turns 非空），该工具轮位于非 Tail 区域，且 A-stage 预算极度紧张（budget=1）",
    "steps": [
      "调用 assemble",
      "在返回消息中定位该预选工具轮对应的消息",
      "检查其 content 格式"
    ],
    "expected": "消息 content 格式为 '[~/turn_index.sub_index] <L1 摘要内容>'；即使预算不足，该预选工具轮也未降级为 L0 或丢弃",
    "cleanup": "engine.store.close()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-022",
    "req": "REQ-TOOL-P004",
    "title": "预算不足时先降级工具轮后降级对话轮",
    "preconditions": "C-stage 预选了 1 个对话轮和 1 个工具轮，但可用升级预算仅够升级 1 个",
    "steps": [
      "调用 assemble",
      "检查最终哪些轮次被升级（消息中出现 '[~/...]' 摘要标记）"
    ],
    "expected": "对话轮被升级（使用 L1 替换），工具轮被降级（未升级）；即对话轮优先于工具轮保留",
    "cleanup": "engine.store.close()",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-023",
    "req": "REQ-TOOL-P005",
    "title": "同类型内部仅按 RRF 得分排序",
    "preconditions": "3 个工具轮候选：A (RRF=0.8, 节省 Token=200), B (RRF=0.9, 节省 Token=50), C (RRF=0.7, 节省 Token=500)",
    "steps": [
      "执行工具轮检索与升级排序",
      "记录升级顺序"
    ],
    "expected": "升级顺序为 B → A → C（按 RRF 降序）；Token 节省量不改变顺序",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-024",
    "req": "REQ-TOOL-P006",
    "title": "工具轮升级数不超过 CA_TOOL_MAX_UPGRADE_K",
    "preconditions": "设置 CA_TOOL_MAX_UPGRADE_K=2；历史工具轮候选有 5 个，预算充足",
    "steps": [
      "执行 assemble",
      "统计返回消息中工具轮升级标记 '[~/...]' 的数量"
    ],
    "expected": "工具轮升级数量 ≤ 2",
    "cleanup": "恢复默认配置 CA_TOOL_MAX_UPGRADE_K=3",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-025",
    "req": "REQ-FUNC-ASTAGE-003",
    "title": "预算计算精确化：系统消息、Head 工具组、Tail 正确扣除",
    "preconditions": "构造 messages：系统消息 50 Token，Head 对话轮 100 Token，Head 工具组 200 Token，Tail 100 Token；context_length=1000",
    "steps": [
      "调用 assemble 或通过 _available_budget 间接验证（利用 CA_DEBUG 日志或最终升级结果推断）",
      "验证预算计算是否符合公式：1000*0.95 - 50(系统) - (100+200)(Head) - 100(Tail) = 500"
    ],
    "expected": "预算为正，工具组 Token 被整体计入；若预算为负则检索跳过",
    "cleanup": "无",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-026",
    "req": "REQ-FUNC-ASTAGE-003",
    "title": "多 tool_calls 工具组整体 Token 正确计入 Head",
    "preconditions": "构造 messages 含一个工具组（assistant 有 3 个 tool_calls + 3 个 tool 响应），该工具组在 Head 区域；系统消息 50 Token；context_length=2000",
    "steps": [
      "执行 assemble，确保最终升级数量符合预算约束",
      "若 CA_DEBUG=1，通过日志验证预算值（若无日志，通过升级数量推断）"
    ],
    "expected": "3 个 tool 响应的 Token 被分别计算并累加；工具组整体占用 = sum(3 tool 响应) + tool_calls 消息 Token；预算计算无遗漏",
    "cleanup": "无",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-A-027",
    "req": "REQ-FUNC-ASTAGE-008",
    "title": "硬截断时工具组整体保留或整体移除，并插入正确提示",
    "preconditions": "缓存为空，消息超 95% 窗口，包含一个工具组（assistant 含 tool_calls + 2 个 tool 响应）",
    "steps": [
      "调用 assemble",
      "检查返回消息中该工具组的处理"
    ],
    "expected": "工具组内所有消息要么全部保留，要么全部移除；若移除则在原位插入一条 role='assistant'、content='[工具调用结果因上下文截断已被省略]' 的消息",
    "cleanup": "engine.store.close()",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-001",
    "req": "REQ-L-STAGE-001",
    "title": "L-stage 异步补全缺失的对话轮 L1",
    "preconditions": "turn_cache 中存在一条对话轮记录，_assemble_status=1，l2_text='User: 你好\\nAssistant: 你好'",
    "steps": [
      "通过 BackfillThread 的 start_event 触发一次补全扫描",
      "等待补全线程处理完成（检查 _assemble_status 变化或超时 10 秒）",
      "从 turn_cache 重新查询该记录"
    ],
    "expected": "_assemble_status 变为 0；l1_text 更新为正常摘要（非降级文本）；backfill_attempts 为补全期间的尝试次数（成功则为 ≤1）",
    "cleanup": "停止补全线程",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-002",
    "req": "REQ-L-STAGE-002",
    "title": "L-stage 异步补全缺失的工具轮 L1",
    "preconditions": "turn_cache 中存在一条工具轮记录（turn_type='tool'），_assemble_status=1，l2_text 包含原始工具消息的 JSON 序列化",
    "steps": [
      "触发补全",
      "查询记录更新状态"
    ],
    "expected": "工具轮 L1 被 ToolSummarizer 重新生成（非空，可解析为 JSON）；_assemble_status=0",
    "cleanup": "停止补全线程",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-003",
    "req": "REQ-L-STAGE-005",
    "title": "补全失败 3 次后标记为永久失败",
    "preconditions": "mock _call_llm_for_l1 始终抛出异常；turn_cache 中有 _assemble_status=1 的对话轮记录",
    "steps": [
      "触发补全",
      "等待补全线程连续处理 3 次",
      "查询该记录的 _assemble_status 和 backfill_attempts"
    ],
    "expected": "_assemble_status=2（永久失败）；backfill_attempts=3；日志中包含 WARNING",
    "cleanup": "移除 mock，停止补全线程",
    "priority": "High",
    "type": "Exception",
    "status": "ready"
  },
  {
    "id": "TC-L-004",
    "req": "REQ-L-STAGE-006",
    "title": "补全线程定时自检：每 60 秒主动扫描缺失记录",
    "preconditions": "补全线程已启动；手动向 turn_cache 插入一条 _assemble_status=1 的记录（绕过 C-stage 事件）",
    "steps": [
      "不触发任何 C-stage 事件",
      "等待最多 60 秒",
      "查询该记录的 _assemble_status"
    ],
    "expected": "60 秒内该记录被补全线程发现并修复，_assemble_status 变为 0",
    "cleanup": "停止补全线程",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-005",
    "req": "REQ-L-STAGE-007",
    "title": "预选等待超时后放弃工具轮预选",
    "preconditions": "设置 CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT=2 秒；对话轮 L1 缺失触发预选等待；mock 补全线程延迟 5 秒",
    "steps": [
      "C-stage 完成后触发预选逻辑（对话轮 L1 缺失 → 等待补全）",
      "等待 3 秒",
      "检查 _pre_upgraded_tool_turns 集合",
      "验证后续 A-stage 仍正常执行"
    ],
    "expected": "_pre_upgraded_tool_turns 为空（预选被跳过）；A-stage 正常完成（仅使用对话轮拣选）；日志中有超时提示",
    "cleanup": "恢复默认配置，停止补全线程",
    "priority": "High",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-006",
    "req": "REQ-L-STAGE-001, REQ-L-STAGE-002",
    "title": "补全成功后更新 AssemblyCache 并触发快照重建",
    "preconditions": "存在 _assemble_status=1 的对话轮记录；补全线程运行中",
    "steps": [
      "触发补全并等待成功",
      "调用 cache.get_snapshot_data() 获取更新后的 L1/L0 文本",
      "检查快照中对应 turn_index 的 L1 是否已更新为非降级摘要"
    ],
    "expected": "cache 中该 turn_index 的 L1 文本已更新（与 turn_cache 一致）；快照版本已刷新",
    "cleanup": "停止补全线程",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-L-007",
    "req": "REQ-L-STAGE-001",
    "title": "补全对话轮后触发工具轮拆分与摘要生成",
    "preconditions": "turn_cache 中存在一条对话轮记录，_assemble_status=1，其 l2_text 包含完整的工具调用序列（user、assistant with tool_calls、tool 响应）；补全线程运行中",
    "steps": [
      "触发补全并等待对话轮补全成功",
      "查询 turn_cache 中是否有新生成的工具轮记录（turn_type='tool'，相同 turn_index 但 tool_sub_index≥1）"
    ],
    "expected": "补全后，存在对应的工具轮记录，l1_text 非空；工具拆分由 _maybe_trigger_tool_summarization 触发",
    "cleanup": "停止补全线程",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-007",
    "req": "REQ-FUNC-DEDUP-005",
    "title": "去重开关非法值时强制回退为启用并告警",
    "preconditions": "通过环境变量设置 CA_DEDUP_ENABLED=INVALID",
    "steps": [
      "调用 Config.reload()",
      "读取 Config.DEDUP_ENABLED 的值",
      "检查日志中是否包含 WARNING 级别的告警信息"
    ],
    "expected": "Config.DEDUP_ENABLED=True（强制启用）；日志中包含类似 'Invalid value for CA_DEDUP_ENABLED' 的 WARNING 消息",
    "cleanup": "恢复环境变量 CA_DEDUP_ENABLED=1",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-008",
    "req": "新增配置项",
    "title": "新增工具轮相关配置项默认值校验",
    "preconditions": "不设置任何与工具轮相关的环境变量",
    "steps": [
      "调用 Config.reload()",
      "依次读取 CA_TOOL_PRE_UPGRADE_COUNT, CA_TOOL_MAX_UPGRADE_K, CA_TOOL_PRE_UPGRADE_WINDOW, CA_BACKFILL_DIALOGUE_RATE, CA_BACKFILL_TOOL_RATE, CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT, CA_SHUTDOWN_TIMEOUT, CA_BM25_HIT_THRESHOLD"
    ],
    "expected": "默认值依次为 3, 3, 50, 2, 5, 30, 5, 5",
    "cleanup": "无",
    "priority": "Low",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-009a",
    "req": "新增配置项",
    "title": "配置 validate() 对超时类下边界钳位",
    "preconditions": "设置 CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT=0（越界）",
    "steps": [
      "调用 Config.reload() 和 Config.validate()",
      "检查 Config.TOOL_PRE_UPGRADE_WAIT_TIMEOUT 的实际值"
    ],
    "expected": "被钳位为合法最小值（如 1）",
    "cleanup": "恢复环境变量",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-009b",
    "req": "新增配置项",
    "title": "配置 validate() 对阈值类下边界钳位",
    "preconditions": "设置 CA_BM25_HIT_THRESHOLD=0（越界）",
    "steps": [
      "调用 Config.reload() 和 Config.validate()",
      "检查 Config.BM25_HIT_THRESHOLD 的实际值"
    ],
    "expected": "被钳位为合法最小值（如 1）",
    "cleanup": "恢复环境变量",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-009c",
    "req": "新增配置项",
    "title": "配置 validate() 对超时类上边界钳位",
    "preconditions": "设置 CA_LLM_TIMEOUT=500（假设上限 300）",
    "steps": [
      "调用 Config.reload() 和 Config.validate()",
      "检查 Config.LLM_TIMEOUT 的实际值"
    ],
    "expected": "被钳位为合法最大值（如 300）",
    "cleanup": "恢复环境变量",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-009d",
    "req": "新增配置项",
    "title": "配置 validate() 对阈值类上边界钳位",
    "preconditions": "设置 CA_BM25_HIT_THRESHOLD=2000（假设上限 1000）",
    "steps": [
      "调用 Config.reload() 和 Config.validate()",
      "检查 Config.BM25_HIT_THRESHOLD 的实际值"
    ],
    "expected": "被钳位为合法最大值（如 1000）",
    "cleanup": "恢复环境变量",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-010",
    "req": "新增配置项",
    "title": "CA_BM25_HIT_THRESHOLD 影响检索通道分配比例",
    "preconditions": "设置 CA_BM25_HIT_THRESHOLD=3；BM25 命中数=4，向量候选充足，总名额=8",
    "steps": [
      "执行检索（Retriever.retrieve 或 retrieve_tools）",
      "检查最终 BM25 候选数和向量候选数"
    ],
    "expected": "BM25 候选数 = min(6, 8) = 6，向量候选数 = 2，即 BM25:Vec ≈ 6:2；符合 _dynamic_allocation 规则",
    "cleanup": "恢复默认配置",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-011a",
    "req": "REQ-TOOL-C005",
    "title": "自定义 YAML Profile 加载成功并应用",
    "preconditions": "提供一个有效的 YAML Profile 文件，设置某工具字段优先级",
    "steps": [
      "设置 CA_TOOL_FIELD_PRIORITY_PROFILE 指向该文件",
      "Config.reload()",
      "触发该工具的摘要生成",
      "检查字段处理结果"
    ],
    "expected": "自定义 Profile 中的字段优先级生效；未定义的字段回退到 default 规则",
    "cleanup": "恢复环境变量，删除临时 Profile 文件",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CF-011b",
    "req": "REQ-TOOL-C005",
    "title": "YAML Profile 语法错误时回退默认并警告",
    "preconditions": "提供一个语法错误的 YAML 文件",
    "steps": [
      "设置 CA_TOOL_FIELD_PRIORITY_PROFILE 指向该文件",
      "Config.reload()",
      "触发工具摘要生成",
      "检查日志和实际优先级行为"
    ],
    "expected": "回退到硬编码默认优先级；日志包含 WARNING 指出 Profile 加载失败",
    "cleanup": "恢复环境变量，删除临时文件",
    "priority": "Medium",
    "type": "Exception",
    "status": "ready"
  },
  {
    "id": "TC-STORE-011",
    "req": "REQ-TOOL-C006",
    "title": "新增列（turn_type, tool_sub_index, _assemble_status, l2_text, backfill_attempts）写入验证",
    "preconditions": "ca_engine fixture，schema 已升级",
    "steps": [
      "写入一条对话轮记录和一条工具轮记录",
      "查询并验证所有新增列"
    ],
    "expected": "对话轮：turn_type='dialogue', tool_sub_index=0, _assemble_status=0, l2_text 非空；工具轮：turn_type='tool', tool_sub_index=1, l2_text 为工具组 JSON，backfill_attempts=0",
    "cleanup": "engine.store.close()",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-STATS-001",
    "req": "REQ-OBS-002",
    "title": "AssembleStats 包含工具轮相关统计字段",
    "preconditions": "ca_engine fixture，执行完整的 A-stage 流程（含工具轮升级）",
    "steps": [
      "调用 assemble",
      "获取 AssembleStats 实例",
      "检查 tool_upgrade_count, tool_pre_upgrade_count 字段"
    ],
    "expected": "两个字段存在且 ≥ 0；类名为 AssembleStats（非 CompressStats）",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Functional",
    "status": "ready"
  },
  {
    "id": "TC-CONC-005",
    "req": "REQ-REL-005",
    "title": "引擎销毁时等待 C-stage 和 L-stage 完成后再释放资源",
    "preconditions": "ca_engine 实例，补全线程已启动，mock C-stage 阻塞 3 秒",
    "steps": [
      "启动阻塞的 C-stage 任务",
      "立即调用 engine.destroy()",
      "记录 destroy() 返回时间",
      "检查 store.close() 和 embed_client.close() 被调用时机"
    ],
    "expected": "destroy() 阻塞至少 3 秒；store.close() 在 C-stage 完成后调用；补全线程收到停止信号并退出；无死锁",
    "cleanup": "无",
    "priority": "High",
    "type": "Concurrency",
    "status": "ready"
  },
  {
    "id": "TC-CONC-006",
    "req": "设计文档 6.线程安全设计",
    "title": "L-stage 补全与 A-stage 读取的并发一致性（快照隔离）",
    "preconditions": "补全线程即将更新一条对话轮记录，A-stage 已获取旧快照",
    "steps": [
      "A-stage 调用 get_bm25_snapshot() 获取快照 v1",
      "补全线程修改同一条记录并触发快照重建（生成 v2）",
      "A-stage 使用 v1 完成 assemble",
      "验证 A-stage 输出中的摘要内容"
    ],
    "expected": "A-stage 输出仍为 v1 中的旧摘要；无 RuntimeError；下一次 assemble 使用 v2",
    "cleanup": "停止补全线程",
    "priority": "High",
    "type": "Concurrency",
    "status": "ready"
  },
  {
    "id": "TC-CONC-007",
    "req": "设计文档 6.线程安全设计",
    "title": "_build_final_messages_v4 执行期间快照被补全线程更新",
    "preconditions": "A-stage 正在遍历工具组组装消息，补全线程同时更新缓存并触发快照重建",
    "steps": [
      "在 _build_final_messages_v4 中通过环境变量或 hook 注入短暂延迟",
      "A-stage 开始组装后，补全线程完成补全并触发快照重建",
      "A-stage 继续完成组装",
      "检查是否抛出异常，并验证数据一致性"
    ],
    "expected": "A-stage 成功完成，未出现字典修改异常；输出使用旧快照数据；数据一致",
    "cleanup": "移除注入点，停止补全线程",
    "priority": "High",
    "type": "Concurrency",
    "status": "ready",
    "note": "需侵入式 hook，测试代码中通过环境变量控制的延迟注入，低优先级"
  },
  {
    "id": "TC-PERF-002",
    "req": "REQ-PERF-004",
    "title": "工具轮规则摘要生成耗时 p95 < 10ms",
    "preconditions": "mock 工具返回正常 JSON；基准环境：历史 ≤100 轮，总 Token ≤50k，本地 NVMe/SSD",
    "steps": [
      "循环调用 ToolSummarizer.summarize_group 100 次",
      "计算 p95 耗时"
    ],
    "expected": "p95 < 10ms",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Performance",
    "status": "ready"
  },
  {
    "id": "TC-PERF-003",
    "req": "REQ-PERF-005",
    "title": "C-stage 预选检索耗时 p95 < 100ms",
    "preconditions": "历史工具轮约 50 条（BM25 已构建）；基准环境同上",
    "steps": [
      "以对话 L1 的 core_change 文本为 Query，调用 _pre_upgrade_tools 100 次",
      "计算 p95"
    ],
    "expected": "p95 < 100ms",
    "cleanup": "无",
    "priority": "Medium",
    "type": "Performance",
    "status": "ready"
  },
  {
    "id": "TC-PERF-004",
    "req": "REQ-PERF-006, REQ-PERF-007",
    "title": "补全耗时验证：对话轮 < LLM 超时，工具轮 < 50ms",
    "preconditions": "mock LLM 延迟 100ms；工具轮补全使用规则引擎",
    "steps": [
      "触发一条对话轮补全，测量耗时",
      "触发一条工具轮补全，测量耗时"
    ],
    "expected": "对话轮补全耗时 < CA_LLM_TIMEOUT（默认 120s）；工具轮补全耗时 < 50ms",
    "cleanup": "停止补全线程",
    "priority": "Low",
    "type": "Performance",
    "status": "ready"
  }
]
```

## 4. 测试策略
1. **合并执行**：将以上46个用例追加至原有`ContextAssembler_testcases_v4.3.4.json`，合并后共146个用例。运行`generate_tests.py`重新生成全部测试文件。
2. **Mock依赖**：C‑stage工具轮、L‑stage补全等模块大量使用`unittest.mock`模拟LLM、工具返回及补全线程事件。
3. **并发控制**：使用`threading.Event`精确控制C/A/L三阶段的时序，确保竞态条件可复现。
4. **性能基线**：所有性能用例基于SRS定义的基准环境（历史≤100轮、Token≤50k、本地SSD），硬件信息由`conftest.py`自动采集。
5. **兼容性处理**：
   - 全库将`CompressStats`替换为`AssembleStats`。
   - `turn_cache`主键及新列需更新store相关mock。
   - `TC-CF-005`预期需与新去重Fail‑Safe策略对齐（本补充集已通过`TC-CF-007`覆盖）。

## 5. 需求覆盖总结
| 需求 ID | 用例数 | 说明 |
|---------|--------|------|
| REQ-TOOL-C001~C008 | 12 | 工具轮识别、摘要、错误处理、字段优先级、存储、无条件生成、容错 |
| REQ-C-ERROR | 2 | 降级标识正常/异常 |
| REQ-TOOL-P001~P006 | 8 | 预选、Tail保护、兜底L1、降级顺序、RRF排序、升级上限 |
| REQ-L-STAGE-001~007 | 7 | 对话/工具轮补全、失败重试、定时扫描、超时放弃、缓存更新 |
| REQ-FUNC-ASTAGE-003(增强) | 2 | 预算精确计算（系统消息、工具组整体） |
| REQ-FUNC-ASTAGE-008 | 1 | 硬截断工具组完整性 |
| REQ-FUNC-DEDUP-005(修正) | 1 | 非法值强制启用去重 |
| 新增配置项 | 7 | 默认值、双边界钳位、BM25阈值分配、Profile加载与回退 |
| REQ-REL-005 | 1 | 销毁顺序 |
| 线程安全 | 2 | 快照隔离、组装期间竞争 |
| REQ-OBS-002(变更) | 1 | AssembleStats重命名及新字段 |
| REQ-PERF-004~007 | 3 | 工具轮摘要、预选检索、补全耗时 |

**总计46个用例，完全覆盖SRS v4.4.0所有新增需求。**

## 6. 文档状态
本文档为最终审定版，所有用例均经多轮自查与修正，与代码实现严格一致。可直接作为测试开发输入。

