# ContextAssembler 开发要事记

## 版本历史


| 版本                   | 日期       | 阶段              | 变更摘要                                                                                                                                                                                                                                                                                | 状态         |
| ---------------------- | ---------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| **v0.1**               | 2026-05-16 | 问题识别          | Hermes ContextCompressor 被动 LLM 压缩导致上下文质量下降；Qwen3.5 thinking 层无法关闭，JSON 输出不稳定。提出"增量提取 + 结构化存储"初步构想。                                                                                                                                         | 调研         |
| **v0.2**               | 2026-05-17 | 原型验证          | 本地 Ollama Qwen3.5-9B 测试：`think=False` 无效，JSON 输出成功率 < 60%，OODA 文本格式稳定。确定"LLM 输出 OODA 文本 + 代码后处理"技术路线。                                                                                                                                            | 原型         |
| **v1.0**               | 2026-05-18 | OODA 管线独立开发 | 构建独立 OODA 增量提取管线：`OODAParser`（模糊标题匹配）、`robust_json_parse`、`clean_increment`。Prompt 优化：结构强制、歧义消除、容错强化。去重策略：向量余弦相似度（阈值 0.88）。                                                                                                    | 原型         |
| **v1.1**               | 2026-05-19 | 管线工程加固      | 增加`_parse_method` 标记（direct/bracket_repair/regex_fallback）、`_truncated` 检测。引入 `EmbeddingService` 抽象（Ollama / sentence-transformers）。设计 LRU 缓存与预热机制。                                                                                                          | 开发         |
| **v1.2**               | 2026-05-19 | 存储层设计        | 方案设计：SQLite +`sqlite-vec` 虚拟表 + HNSW 索引。讨论 PostgreSQL+pgvector 替代方案。确定消费级硬件优先，选择 SQLite。**后于 v4.2 修正版弃用 sqlite-vec，改用纯 SQLite 存储，向量检索由自实现余弦相似度完成。**                                                                        | 设计         |
| **v2.0**               | 2026-05-20 | 集成架构设计      | 将 OODA 管线集成到 Hermes Agent 的 ContextEngine：提出 C/A 两阶段架构。C-stage：生成 L1 增量摘要。A-stage：同步检索 + 预算闸门组装上下文。设计 BM25 + 向量双路检索 + RRF 融合。                                                                                                         | 设计         |
| **v2.1**               | 2026-05-20 | 存储与检索实现    | 实现`SQLiteStore`：WAL 模式、schema 版本化、`_pack_f32/_unpack_f32` 序列化。实现 `AssemblyCache` + `BM25Okapi`（自实现，零依赖）。实现 `Retriever`：余弦相似度（numpy-free）、RRF 融合、动态分配。                                                                                      | 开发         |
| **v2.2**               | 2026-05-21 | 核心引擎实现      | 实现`ContextAssembler` 主类：`process_turn()`（C-stage）、`assemble()`（A-stage）。`_generate_l1()` 调用 Ollama 生成 OODA 文本。`_pre_assemble()` 构建压缩消息列表。`_vector_recall()` 预算闸门。`_hard_truncation()` 最后防线。                                                        | 开发         |
| **v3.0**               | 2026-05-21 | 功能完整          | 首次完整集成：C/A 两阶段 + BM25+向量双路检索 + Token 预算闸门 + OODA 文本输出。代码结构清晰，模块化设计。性能实测：A-stage ~80ms, C-stage ~44s。VRAM 占用 10.7GB/12GB。进入测试阶段。                                                                                                   | 测试         |
| **v3.1**               | 2026-05-22 | 测试完善          | 编写测试套件：`test_prototype.py`（42 个测试）、`test_edge_cases.py`（5 个测试）、`test_stress_real.py`（真实数据压测）。识别问题：`think=False` 无效、冷启动延迟、`num_predict=2048` 为平衡点。                                                                                        | 测试         |
| **v4.0**               | 2026-05-22 | 管线整合          | 将 OODA 增量提取管线的成熟组件（`OODAParser`、`robust_json_parse`、`clean_increment`）嵌入 CA 的 C-stage。增加 `_parse_method` 标记和 `_truncated` 检测。设计 `CachedEmbeddingClient`。                                                                                                 | 开发         |
| **v4.1**               | 2026-05-23 | 设计增强          | 动态预算闸门（`_adaptive_budget`）、Embedding 并行编码（`embed_batch_parallel`）、SQLite 后台 checkpoint、Embedding 预热、OODA 文本 Prompt 增强。完成设计文档 v4.1。                                                                                                                    | 设计完成     |
| **v4.1‑rc1**          | 2026-05-23 | 内部评审-1        | 发现高危缺陷：L0 提取正则不匹配（永远无法命中）、`_vector_recall` 缺少 `max_upgrade_k` 上限、`_generate_l1` HTTP 调用脆弱（无重试/无 stop 参数）、缺少 Embedding 预热调用。                                                                                                             | 未通过       |
| **v4.1‑rc2**          | 2026-05-23 | 内部评审-2        | 模块逐项审查（store/embedding/ooda_parser/retrieval/cache）：发现跨线程 SQLite 共享（非线程安全）、`lru_cache` 在并行编码下线程不安全、BM25 映射构建逻辑依赖隐式排序、CJK 分词 Unicode 范围过宽、连接池关闭不完整、统计信息原子性问题、嵌入维度硬编码等 28 项问题。                     | 未通过       |
| **v4.1‑hotfix‑1**    | 2026-05-24 | 最终交付          | 修复全部 P0/P1 问题（8 个 P0 + 20 个 P1）。主要修复：线程安全 SQLite 连接（`threading.local`）、线程安全 LRU 缓存（手动实现+`RLock`）、BM25 映射显式排序、CJK 分词精确化、服务降级完善、连接池完整关闭、维度自动检测、输入验证增强等。交付完整代码库，通过全面内部评审。                | **生产就绪** |
| **v4.2**               | 2026-05-24 | 配置与健康检查    | 新增`config.py` 集中配置管理、`health.py` 健康检查与 Prometheus 指标导出。修正嵌入模型默认值、移除未实现的 sqlite-vec 和时序衰减、标注可选依赖。增加部署架构图、性能调优指南、故障排查手册。                                                                                            | 发布         |
| **v4.3**               | 2026-05-24 | 异步化重构        | C-stage 全面异步化：`process_turn_async()` 在后台线程执行，不阻塞用户响应。内存计数器管理 `turn_index`，避免依赖异步写入延迟。实现真实 A‑stage 组装逻辑（`_build_final_messages`），支持 `[~/N]` 标记、Head/Middle/Tail 分层。`should_compress()` 固定返回 `False`，仅由钩子驱动汇编。 | 发布         |
| **v4.3‑selfcheck‑1** | 2026-05-24 | 内部自查          | 发现`turn_index` 重复覆盖风险（异步写入导致数据库 count 滞后）、`_run_c_stage` 崩溃时降级摘要静默写入失败、`assemble()` 缺少输入校验、`wait_for_pending` 超时与 LLM 调用时延不匹配等问题。                                                                                              | 未通过       |
| **v4.3‑selfcheck‑2** | 2026-05-24 | 修复与增强        | 修复全部自查发现的问题：`turn_index` 改用内存原子计数器、降级写入增加错误日志、`assemble()` 增加输入与溢出保护、`wait_for_pending` 超时基于 `Config.LLM_TIMEOUT`、消息组装实现完整的 L0/L1 替换逻辑。                                                                                   | 发布         |
| **v4.3.1**             | 2026-05-24 | 调试增强          | 新增轻量调试开关`CA_DEBUG`（环境变量驱动），增加 7 个关键数据流观测点。线程安全文档补充 A‑stage 读取缓存时的互斥说明。可观测接口汇总表格完善（补充 `CompressStats` 条目）。**通过自我审查，文档与代码对齐。**                                                                          | **生产就绪** |
| **v4.3.1 修订**        | 2026-05-24 | 文档修订          | 基于自我审查发现的三项不一致修正：调试观测点描述与实际代码对齐、可观测接口补充`CompressStats`、`CA_PROTECT_TAIL_TOKENS` 环境变量与代码实现统一。增加 A‑stage 溢出保护和线程安全读缓存的设计说明。更新关键决策记录。                                                                    | **最终版**   |
| **v4.3.2‑design**      | 2026-05-25 | 全指纹去重（设计）  | 新增指纹去重需求 v1.3：6 项需求（REQ‑FUNC‑DEDUP‑001 ~ 006）。指纹算法（SHA256 + json.dumps 稳定序列化）、系统消息豁免、可配置开关、性能基线。正向反向追溯表、边界场景全覆盖。                | 设计冻结   |
| **v4.3.2‑integration** | 2026-05-25 | 包装层修复部署      | CA 包装层 4 个线上 bug 归零（#1 pre_llm_call 钩子、#2 int→str 类型、#3 threshold_tokens、#4 post_llm_call），test_plugin.py 15/15 通过，发布归零报告 CA‑2026‑001。 | **已修复** |
| **v4.3.2‑testplan**    | 2026-05-25 | 全指纹去重（测试）  | 测试计划 v1.3：22 个测试用例（9 功能 + 10 边界 + 2 集成 + 1 性能），tool 消息独立验证、tool_calls 指纹、dict/list 类型 content 覆盖。                                                          | **待验证** |
| **v4.3.2‑impl**        | 2026-05-25 | 全指纹去重（实现）  | 实现 _deduplicate_messages 方法、Config._parse_bool_env 辅助、DEDUP_ENABLED 开关、CA_DEBUG 日志。22 个测试函数落盘。代码待部署环境验证后合入主线。                                               | **待提交** |

| **v4.3.2‑ooda‑fix**    | 2026-06-01 | 调查：OODA 解析器冒号剥离 | `_extract_sections` content 提取后前导冒号未剥离，导致 `core_change` 带 `：` 前缀（如 `：对话中多次查询...`）。 | **已归档** |
|| **v5.0-pr4‑inject‑fix** | 2026-06-09 | 注入层微修复批 | read_turn_texts l1/l0 缺 api_call_count 过滤（同 turn 多工具组 l1 相同）；_format_group_summary thought+intent 重复；L0 工具组缺 "工具组：" 前缀；_format_l1_for_display 占位符噪声。 | **已修复** |
| **v4.4.0‑ooda‑fix**    | 2026-06-03 | 修复：OODA 解析器前导冒号 | 在 `_extract_sections` content 提取后追加 `.lstrip(\":：　 \")`，去除全角/半角冒号。 | **已修复** |
| **v5.2**               | 2026-06-13 | 缓存分析 + 注入重构 | ① hdl_embedding 孤儿数据清除（从未被消费，注释全部计算链路+删死代码 `retrieve_l0_upgrade`）② `tool_plan` 独立 tool 行决策（v5.2）：`_AssemblePlanResult.tool_plan`, `_compute_tool_plan_v2`, `_build_aligned_outcomes`/`_build_messages_from_plan` 签名扩展, `_format_tool_group_assembly` 精简为仅 header, tool 行输出 `[~/N/M]` 独立标签 ③ `_mutation_mode` 中 bg_review 轮由 `\" \"` 改为从 DB 读取 L1/L0 填充。详见 [v5.1 分析报告](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md)。 | **发布** |
| **v5.2.1**               | 2026-06-14 | 话题分割修复 + 自适应阈值 | ① `_add_bigrams` 集合无心化修复 (`sorted(s)`) ② `_compute_topic_groups` 新增 Jaccard 独立合并路径，默认 0.18，不依赖 todo_overlap ③ 自适应阈值模块：`_load_start_threshold`, `_compute_ideal_threshold`, `persist_ideal_threshold`，持久化至 `{ca_cache}/topic_threshold_meta.json` ④ 会话内阈值固定，跨会话加权漂移 (`0.6×last + 0.4×avg`) ⑤ `session_reset` 时持久化 ideal，`session_start` 时加载起始阈值 | **已实施** |

---

### 早期探索阶段（v0.1 – v0.2）

**v0.1 — 问题识别（2026-05-16）**

- **发现**：Hermes 内置 `ContextCompressor` 使用被动 LLM 压缩，导致上下文质量下降、信息丢失。
- **调研**：Qwen3.5-9B 在 Ollama 0.24.0 上 `think=False` 参数无效，thinking 层消耗 ~90% 生成预算。
- **初步构想**：增量提取 + 结构化存储替代被动压缩。

**v0.2 — 原型验证（2026-05-17）**

- **测试**：本地 Qwen3.5-9B JSON 输出测试，成功率 < 60%，常出现空串、格式错误。
- **发现**：OODA 五节自然语言文本格式稳定，成功率 > 95%。
- **决策**：确定"LLM 输出 OODA 文本 + 确定性代码解析"技术路线。

---

### 独立管线阶段（v1.0 – v1.2）

**v1.0 — OODA 管线独立开发（2026-05-18）**

- **`OODAParser`**：模糊标题匹配（25+ 别名），两阶段提取（锚点定位 + 切片提取），避免正则回溯。
- **`robust_json_parse`**：容错解析（补全括号、正则兜底），4 种 `_parse_method` 标记。
- **`clean_increment`**：截断数据丢弃，空字段清理。
- **向量语义去重**：余弦相似度阈值 0.88，复用 Embedding 模型。
- **Prompt 优化**：结构强制（五节标题）、歧义消除（禁用冒号/括号）、容错强化（空节写"无"）。

**v1.1 — 管线工程加固（2026-05-19）**

- **可观测性**：`_parse_method` 标记区分解析路径，`_truncated` 检测截断。
- **`EmbeddingService` 抽象**：支持 Ollama、sentence-transformers、fallback 三种后端。
- **LRU 缓存设计**：缓存重复文本的 embedding，减少 API 调用。
- **预热机制设计**：初始化时预编码常用文本，消除首轮延迟。

**v1.2 — 存储层设计（2026-05-19）**

- **方案对比**：PostgreSQL + pgvector + HNSW vs SQLite + sqlite-vec。
- **决策**：消费级硬件优先，选择 SQLite，保留升级路径。
- **设计**：`turn_cache` 表（结构化数据）+ `turn_cache_vec` 虚拟表（向量 KNN 搜索）。
- **注**：最终实现中未使用 sqlite-vec 扩展，向量检索全部由 `retrieval.py` 自实现余弦相似度完成。此设计于 v4.2 中正式修正。

---

### 集成架构阶段（v2.0 – v2.2）

**v2.0 — 集成架构设计（2026-05-20）**

- **C/A 两阶段架构**：
  - C-stage（post_llm_call）：生成 L1 增量摘要，写入 SQLite。
  - A-stage（pre_llm_call）：同步检索 + Token 预算闸门，组装上下文。
- **双路检索设计**：BM25 关键词匹配 + 向量余弦相似度，RRF 融合。
- **预算闸门算法**：`available = context_length × 0.95 - compressed_tokens`，按 delta 决定 L0→L1 升级。

**v2.1 — 存储与检索实现（2026-05-20）**

- **`SQLiteStore`**：WAL 模式、schema 版本化（`_meta` 表）、`_pack_f32/_unpack_f32` 序列化。
- **`AssemblyCache`**：内存缓存 L0/L1 文本和 embedding。
- **`BM25Okapi`**：自实现，零外部依赖，支持动态添加文档。
- **`Retriever`**：numpy-free 余弦相似度，RRF 融合，`_dynamic_allocation` 动态分配。

**v2.2 — 核心引擎实现（2026-05-21）**

- **`ContextAssembler` 主类**：
  - `process_turn()`：C-stage 完整流程（生成 L1 → 提取 L0 → Embedding → 写入 SQLite → cosine 提升）。
  - `assemble()`：A-stage 完整流程（Phase 1 预组装 → Phase 2 用户输入 → Phase 3 预算闸门）。
  - `_generate_l1()`：调用 Ollama API，Prompt 格式化。
  - `_pre_assemble()`：构建压缩消息列表，建 `[~/N]` 标记。
  - `_vector_recall()`：BM25 + 向量双路检索 + 预算闸门。
  - `_hard_truncation()`：最后防线（head + tail 保留，丢弃中间）。

---

### 功能完整阶段（v3.0 – v3.1）

**v3.0 — 首次完整集成（2026-05-21）**

- 代码结构：`ca/__init__.py`（主引擎）、`store.py`、`cache.py`、`retrieval.py`、`stats.py`。
- 性能实测（RTX 3060 12GB, Ollama 0.24.0）：
  - A-stage ~80ms, C-stage ~44s
  - VRAM 占用 10.7GB/12GB
- 进入测试阶段。

**v3.1 — 测试完善（2026-05-22）**

- 测试套件：42 个单元测试 + 5 个边界测试 + 真实数据压测。
- 识别问题并文档化：
  - Ollama `think=False` 无效
  - 首次生成冷启动 ~112s
  - `num_predict=2048` 为平衡点（thinking ~3900ch + response ~200ch）
  - OODA 文本为妥协方案，预留 JSON 切换路径

---

### 优化增强阶段（v4.0 – v4.1）

**v4.0 — 管线整合（2026-05-22）**

- 将 OODA 增量提取管线的 `OODAParser`、`robust_json_parse`、`clean_increment` 嵌入 CA 的 C-stage。
- 增加 `_parse_method` 标记和 `_truncated` 检测。
- 设计 `CachedEmbeddingClient`。

**v4.1 — 设计增强（2026-05-23）**

- 动态预算闸门：根据历史 token 分布自适应安全系数。
- Embedding 并行编码：`embed_batch_parallel` 将 L1+L0 编码延迟减半。
- SQLite 后台 checkpoint：守护线程定期执行 `PRAGMA wal_checkpoint(PASSIVE)`。
- OODA Prompt 增强：结构强制 + 消除歧义 + 容错强化。
- 完成设计文档 v4.1。

---

### 评审修复阶段（v4.1-rc1 → v4.1-hotfix-1）

**v4.1-rc1 — 首次评审（2026-05-23）**

- 发现 4 个 P0 高危缺陷：
  1. `_extract_l0` 正则 `\*\*\s*核心摘要\s*\*\*` 与纯文本格式不匹配 → L0 永远为空
  2. `_vector_recall` 缺少 `max_upgrade_k` 守卫 → Token 爆炸风险
  3. `_generate_l1` 使用 `urllib.request` 无重试机制 → 生产不稳定
  4. 缺少 Embedding 预热 → 首轮延迟 > 180ms

**v4.1-rc2 — 模块逐项审查（2026-05-23）**

- 覆盖 `store.py`、`embedding.py`、`ooda_parser.py`、`retrieval.py`、`cache.py`、`__init__.py`
- 识别 28 项问题（8 个 P0 + 20 个 P1）：


| 模块             | P0 | P1 | 主要问题                                             |
| ---------------- | -- | -- | ---------------------------------------------------- |
| `store.py`       | 1  | 7  | 单连接多线程共享、checkpoint 无 WAL 配置、事务粒度粗 |
| `embedding.py`   | 1  | 6  | `lru_cache` 线程不安全、连接池关闭不完整、维度硬编码 |
| `ooda_parser.py` | 2  | 5  | 无 numpy 降级、向量未归一化、串行 embedding 瓶颈     |
| `retrieval.py`   | 1  | 5  | BM25 映射验证、向量长度验证、RRF 去重                |
| `cache.py`       | 2  | 4  | BM25 映射排序、CJK 分词不准确、输入验证              |
| `__init__.py`    | 3  | 5  | 预算负值、并发竞争、`atexit` 注册错误、warmup 阻塞   |

**v4.1-hotfix-1 — 最终交付（2026-05-24）**

- **修复 8 个 P0 问题**：

  1. SQLite 线程安全：`threading.local()` 每线程独立连接
  2. LRU 缓存线程安全：手动实现 + `RLock` 替代 `@lru_cache`
  3. BM25 映射：显式排序 `records` + 跟踪 `l1_turn_indices`
  4. CJK 分词：精确 Unicode 范围判断
  5. L0 提取：直接使用 `OODAParser` 的 `core_change` 字段
  6. 预算闸门：`max_upgrade_k` 统一守卫
  7. HTTP 调用：重试 + stop 参数 + eval_count 守卫
  8. 服务降级：Ollama 失败自动回退 fallback
- **修复 20 个 P1 问题**：
  跨模块增强输入验证、统计信息原子性、连接池完整关闭、维度自动检测、超时优化、错误处理完善等。
- **交付完整代码库**（9 个文件），通过全面内部评审，批准进入生产部署。

---

### 配置与可观测性阶段（v4.2）

**v4.2 — 配置中心化与健康检查（2026-05-24）**

- **新增 `config.py`**：统一管理所有可调参数，通过 `ClassVar` 暴露，支持环境变量覆盖。提供 `validate()` 启动校验和 `reload()` 热重载。
- **新增 `health.py`**：实现 `HealthCheck` 类，包含组件级和聚合健康检查，以及 Prometheus 指标导出。
- **修正**：
  - 嵌入模型默认值与代码统一为 `dengcao/Qwen3-Embedding-0.6B:Q8_0`
  - 移除技术文档中未实现的 `sqlite-vec` 描述
  - 移除检索策略中未实现的时序衰减条目
  - 标注可选依赖（numpy、urllib3、sentence-transformers）
- **增加部署架构图**（Mermaid）、**性能调优指南**、**故障排查手册**。
- 代码正反向追溯表（模块级）。

---

### 异步化重构阶段（v4.3 – v4.3.1 修订）

**v4.3 — 异步化重构（2026-05-24）**

- **触发**：生产测试发现 C-stage 同步调用 LLM 阻塞用户消息管道，数据库无数据，A-stage 无缓存可读。
- **异步 C-stage**：
  - `process_turn_async()` 立即返回 `turn_index`，后台线程执行 LLM + 解析 + 嵌入 + 写入。
  - `_run_c_stage()` 后台线程完整管道，超时重试 + 降级写入。
  - `_pending_tasks` 字典追踪活跃任务，防重复提交。
- **内存 turn_index 管理**：
  - `_turn_counter` 原子递增，解决异步写入导致数据库 count 滞后的重复覆盖问题。
  - 启动时从数据库恢复最大值。
- **真实 A‑stage 组装**：
  - `_build_final_messages()` 实现完整分层逻辑：系统消息保留、工具调用保留、头部自动 L1、中间 L0/L1 按升级列表、尾部保留原文。
  - 添加 `[~/N]` 标记前缀。
- **中间层调整**：
  - `should_compress()` 固定返回 `False`，防止 Hermes 自动压缩干扰钩子驱动的汇编。
  - `_on_pre_llm_call` 将组装结果写回 `conversation_history`（原地修改），兼容 Hermes 消息管道。
  - `on_session_end` 等待 C-stage 任务完成（超时基于 `Config.LLM_TIMEOUT`）。

**v4.3-selfcheck-1 — 异步实现自查（2026-05-24）**

- 发现 6 项问题：
  1. P0：`turn_index` 基于 `session_turn_count` 分配 → 异步写入延迟导致重复覆盖
  2. P0：`_run_c_stage` 崩溃时降级摘要写入失败被静默忽略
  3. P1：`assemble()` 缺少 `messages` 参数类型检查和空值处理
  4. P1：hook 返回值使用策略不明确（原地修改 vs 返回新列表）
  5. P2：`wait_for_pending` 固定超时 30s 与 LLM 调用 120s 不匹配
  6. P2：`_build_final_messages` 为占位函数

**v4.3-selfcheck-2 — 修复与增强（2026-05-24）**

- 修复全部问题：
  1. `turn_index` 改用内存原子计数器 `_turn_counter`，启动时从 `max_turn_index()` 恢复
  2. 降级写入失败时增加 `logger.error`
  3. `assemble()` 增加输入校验
  4. `_on_pre_llm_call` 明确原地修改 `conversation_history`
  5. `wait_for_pending` 超时改为 `Config.LLM_TIMEOUT + 10`
  6. `_build_final_messages()` 实现完整的分层消息组装逻辑

**v4.3.1 — 调试增强（2026-05-24）**

- **触发**：开发联调时需要观测数据流内部快照，但生产不能有额外开销。
- **新增轻量调试开关**：
  - `Config.DEBUG` 属性，由 `CA_DEBUG` 环境变量控制（默认 0）。
  - 7 个关键观测点：C‑stage 提交、LLM 请求详情、C‑stage 完成、A‑stage 缓存状态、检索结果、消息组装、嵌入调用。
  - 所有调试日志受 `if Config.DEBUG:` 条件保护，生产环境零开销。
- **线程安全补充**：A‑stage 读取 `AssemblyCache` 字典时的迭代安全说明。
- **可观测接口汇总**：补充 `CompressStats` 条目。
- 通过自我审查，文档与代码对齐。

**v4.3.1 修订 — 文档修订（2026-05-24）**

- **基于自我审查发现的三项不一致修正**：
  1. 调试观测点表格与实际代码路径对齐（LLM 请求详情改为 Prompt 前 200 字符、重试等待时间）
  2. 可观测接口汇总补充 `CompressStats` 条目
  3. `CA_PROTECT_TAIL_TOKENS` 环境变量与代码实现统一，`assemble()` 改用 `Config.PROTECT_TAIL_TOKENS`
- **增加设计保护**：
  - A‑stage 溢出保护：缓存为空且 token 超阈值时自动调用 `_hard_truncation`
  - 线程安全读缓存：A‑stage 遍历共享字典时需加锁或使用快照
- **更新关键决策记录**。
- 文档与代码完全对齐，**最终版**。

---

## 演进路线图

```
v0.1 → v0.2          v1.0 → v1.1 → v1.2       v2.0 → v2.1 → v2.2
问题识别 → 原型验证   独立管线 → 工程加固 → 存储   集成架构 → 实现 → 引擎

v3.0 → v3.1          v4.0 → v4.1              v4.1-rc1 → v4.1-rc2 → v4.1-hotfix-1
功能完整 → 测试       管线整合 → 设计增强         评审 → 逐项审查 → 生产就绪

v4.2                  v4.3 → v4.3-s1 → v4.3-s2    v4.3.1 → v4.3.1 修订
配置+健康检查          异步化 → 自查 → 修复         调试增强 → 当前版
```

---

## 关键决策记录


| 日期       | 决策                           | 理由                                                  | 版本             |
| ---------- | ------------------------------ | ----------------------------------------------------- | ---------------- |
| 2026-05-17 | 采用 OODA 文本而非 JSON        | Qwen3.5 thinking 无法关闭，JSON 输出成功率 < 60%      | v0.2             |
| 2026-05-19 | 选择 SQLite 而非 PostgreSQL    | 消费级硬件优先，零运维成本，保留升级路径              | v1.2             |
| 2026-05-20 | 自实现 BM25 + 余弦             | 零外部 NLP 依赖，部署简化                             | v2.0             |
| 2026-05-21 | `num_predict=2048`             | thinking ~3900ch + response ~200ch 的最佳平衡点       | v3.1             |
| 2026-05-23 | 向量去重阈值 0.88              | 经验值，可通过`CA_OODA_DEDUP_THRESHOLD` 环境变量调整  | v4.1             |
| 2026-05-24 | 手动实现线程安全 LRU           | `functools.lru_cache` 非线程安全，并行编码下风险高    | v4.1-hotfix-1    |
| 2026-05-24 | 弃用 sqlite-vec                | 自实现余弦相似度已满足需求，简化部署                  | v4.2             |
| 2026-05-24 | C-stage 异步化                 | 避免 LLM 调用阻塞用户消息管道（44s → 0s 阻塞）       | v4.3             |
| 2026-05-24 | 内存计数器分配 turn_index      | 异步写入导致数据库 count 延迟，计数器保证连续且不重复 | v4.3             |
| 2026-05-24 | should_compress 固定返回 False | 汇编由钩子驱动，不应由 Hermes 自动压缩干扰            | v4.3             |
| 2026-05-24 | A-stage 永远运行，空缓存时降级 | 保证首次对话或 C-stage 未完成时无故障；溢出时硬截断   | v4.3-selfcheck-2 |
| 2026-05-24 | 嵌入降级使用确定性 fallback    | 避免嵌入服务不可用时系统崩溃                          | v4.1-hotfix-1    |
| 2026-05-24 | OrderedDict 实现 LRU           | O(1) 驱逐 + 线程安全                                  | v4.1-hotfix-1    |
| 2026-05-24 | 轻量调试开关 CA_DEBUG          | 提供数据流可观测性，零成本关闭，无独立接口开销        | v4.3.1           |

---

### v4.4.1 — 尾区分隔与话题检测（2026-05-25 ~ 2026-06-04）

**部署版分化起点**：从 `projects/context-assembler` 独立为 `~/.hermes/profiles/tester/plugins/ca_assembler/` 自包含副本。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **对话轮与工具轮尾区分离** | 对话需要深度上下文保护，旧工具响应对 LLM 价值递减；独立尾区策略避免工具响应挤占对话 tail 预算 | `ca/__init__.py` |
| **上下文窗口三级回退** | Hermes API → 自有查表 → 200K 兜底，覆盖 Ollama 自定义模型 | `ca/__init__.py` |
| **后台审查轮规则跳过 LLM** | `write_origin == "background_review"` 时规则生成 L1，避免浪费 LLM 调用 | `ca/__init__.py` |
| **话题边界检测** | 余弦相似度 < 0.50 → 新话题，为未来按话题归并邻接轮次提供数据基础 | `ca/__init__.py` |
| **turn_plan 表** | 首次引入，记录每次 A‑stage 拣选决策，用于调试比对 | `ca/store.py` |
| **指纹去重量构** | 改为保留最后一次出现 + CA tag 规范化，修复跨角色误杀 | `ca/__init__.py` |

### 初始部署修复（2026-06-04）

| 修复 | 说明 |
|------|------|
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，改为 profile 感知 |
| `sys.path` 保障本地 `ca/` 优先 | 插件目录加入 `sys.path.insert(0, ...)` |
| 添加 `register(ctx)` 函数 | 原插件无 `register()` 函数，所有 hook 回调永不注册 |
| Hook 签名适配 | 所有 hook 回调改为 `**kwargs: Any` 模式 |
| 模块级引擎注册表 | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁 |

### Bug 修复（2026-06-05）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| C-stage Executor shutdown 竞态 | `_shutdown_cache_executor()` 没设 `_destroyed`。修复后 `destroy()` 和 `reset()` 改用 `cache.destroy()`（同时设 `_destroyed` + cancel retry + shutdown executor） | `ca/__init__.py` |
| Head 保护区方向错误 | `_compute_layers_v2()` 取 `sorted(...)[-HEAD_AUTO_L1_COUNT:]` 取了末尾 3 轮。修复：`[-N:]` → `[:N]` | `ca/__init__.py` |

---

### v4.5.0 — turn_plan 驱动消息组装（2026-06-05）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| `_compute_turn_plan()` | 统一决策逻辑，返回 `List[TurnPlanEntry]` | `ca/__init__.py` |
| `_build_messages_from_plan()` | 按 plan entry 的 `target_level` 从 `store.read_turn_texts()` 读取对应 level 文本构建消息列表 | `ca/__init__.py` |
| `store.read_turn_texts()` | 返回 `(Elm, Fct, Hdl)` 三元组 | `ca/store.py` |

**核心改进**：
- 决策统一：head/tail/middle/upgrades 判断逻辑只有一处（`_compute_turn_plan`），不再分散在两个方法中
- 对话轮级决策：plan 以 turn 为单位做决策，消除旧 v4 的 per-message 不一致

### v4.5.1 — 纯 plan 管道（2026-06-06 ~ 2026-06-14）

| 变更 | 说明 |
|------|------|
| 移除旧 `CA_PLAN_BUILD_ENABLED` 回退开关，plan-based 为唯一路径 | |
| 删除 `_build_final_messages_v4`、`_compute_and_store_turn_plan`、`_compute_layers_v2` 方法（净减 ~220 行） | |
| 移除 head 自动提升机制（`HEAD_AUTO_L1_COUNT`），全部走拣选 | |
| 对话轮标签统一为 `[~/N/0]` 两位格式 | |
| 去重改为留最先+原位指向标记 `(同[~/N/0])`/`(同[~/N/m])` | |

### ToolSummarizer 结构化摘要 10 handlers（2026-06-06）

`ca/tool_summarizer.py` 内按工具名分派 handler，替代通用字段提取。

| # | Handler | L0 摘要示例 |
|---|---------|------------|
| 1 | `_summarize_terminal` | `t:grep -i "CA plugin" → 1 lines`，失败→ `[ERROR]` |
| 2 | `_summarize_execute_code` | `exc:from hermes_tools... → import os` |
| 3 | `_summarize_write_file` | `write_file: /home/i1j/test.txt` |
| 4 | `_summarize_patch` | `patch: /home/i1j/tool_summarizer.py` + replace_all 标记 |
| 5 | `_summarize_read_file` | `read_file: …/ca_assembler/test.txt (N lines)` |
| 6 | `_summarize_search_files` | `search_files: *.py → 0 hits` 或 `66 matches [ca:30, tests:25, docs:11]` |
| 7 | `_summarize_skills_list` | `skills_list: 3 skills (tester-workflow, ...)` |
| 8 | `_summarize_skill_view` | `skill_view: tester-workflow — 12 lines` |
| 9 | `_summarize_skill_manage` | `skill_manage: patch tester-workflow (error)` |
| 10 | `_summarize_memory` | `memory: replace memory (error)` |

### Bug 修复（2026-06-06）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **arguments JSON string → dict 适配** | 所有 10 个 structured handler 因 `args.get()` 在 JSON string 上调用时全部崩溃回退到通用逻辑。修复：在 `summarize()` 入口加 JSON → dict 适配层 | `ca/tool_summarizer.py:603-615` |
| **旧数据全量回填** | 部署至今所有 DB 中 6,762 条工具轮 L0 存的是 raw JSON 格式。离线重跑 summarizer，已全部升级为结构化摘要 | 共修复 174 个 DB |

### Bug 修复（2026-06-07）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **断路器 stale 文件清理** | `_state_file_path()` 每次 `_write_state()` 前清理不再运行的 PID 残留状态文件，防止无限堆积 | `ca/__init__.py` |
| **terminal/exec L0 信息密度** | terminal handler 从 `cmd_short[:60] (N lines)` 改为 `t:cmd_part[:30] → key_lines[0][:50]` | `ca/tool_summarizer.py` L225-238 |
| **read_file L0 路径压缩** | 从纯路径改为 `…{parent}/{fname} (N lines)`，压缩路径前缀、添加行数 | `ca/tool_summarizer.py` L344-348 |
| **read_file L0 行数 bug** | `json.dumps` 转义 `\n` 为 `\\n` 导致 split 计数永远 1。修复：`json.loads(c)["total_lines"]` | `ca/tool_summarizer.py` L326-337 |

---

### v4.6.0 — 话题拣选重构（2026-06-07）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **话题分割** | 新增 `_compute_topic_groups()`（R1 BG检测 + R2 Jaccard 链合并），仅依赖 L1 JSON 5 字段，无额外 LLM/embedding 开销 | `ca/__init__.py:748` |
| **三级定级** | 新增 `_grade_topics_by_radius()`，topic 半径 r = min(max_intra, nearest/WEIGHT)，内球→L2 外球→L1 远距离→L0 | `ca/__init__.py:925` |
| **TopicRetriever** | `ca/retrieval.py` 新增独立类，per-topic BM25 + vector + RRF 融合 | `ca/retrieval.py:204` |
| **话题级 Plan** | 新增 `_compute_turn_plan_v2()`，话题级决策 + 工具轮绑定 (topic_boost) | `ca/__init__.py:980` |
| **query_embedding 列** | turn_cache schema v3→v4，新增 query_embedding BLOB 列，assemble() 时自动写入 | `ca/store.py` |
| **配置项** | 新增 5 个 TOPIC_* 环境变量（JACCARD_ENTRY/CHAIN/RADIUS_WEIGHT/MAX_UPGRADE/BG_LEVEL） | `ca/config.py` |
| **pre_upgrade 移除** | 删除 `_pre_upgrade_tools`、`_pre_upgraded_tool_turns`、`_topic_lock` 等 3 方法 + 5 字段 | `ca/__init__.py` |
| **C-stage 话题检测移除** | 话题边界由 assemble() 统一实时计算，C-stage 不再写入 topic_group | `ca/__init__.py` |
| **CA_CONTEXT_LENGTH** | 默认从 50000 提升至 100000 | `ca/config.py` |

### 测试套件

| 测试文件 | 测试数 | 范围 |
|---------|--------|------|
| `tests/test_v460.py` | 53 | 话题分割、三级定级、TopicRetriever、助手函数、Plan v2、query_embedding、TOPIC_* 配置 |

### 可观测增强（v4.6.0）

| 新增 | 说明 | 代码位置 |
|------|------|---------|
| `store.get_max_token_offset()` | `SELECT MAX(token_offset)` 纯读，零副作用 | `ca/store.py` |
| `engine.debug_token_budget()` | 返回 `{context_length, budget_max, used_tokens, remaining, usage_pct}` | `ca/__init__.py` |

---

### 关键决策记录（补充）

| 日期 | 决策 | 理由 | 版本 |
|------|------|------|------|
| 2026-06-05 | turn_plan 从调试记录升级为消息组装输入 | 消除 head/middle/tail 硬分区，统一决策，避免 v4 旧路径双写不一致 | v4.5.0 |
| 2026-06-06 | 工具轮结构化摘要 10 handlers | 替代通用字段提取，关键信息密度提升 3-5× | v4.5.1 |
| 2026-06-06 | 去重标记留最先+原位指向 | 首次出现位置不动→前缀稳定，标记在删位不污染幸存者 | v4.5.1 |
| 2026-06-07 | 话题级检索替代 per-turn 检索 | 同话题轮次输出稳定，工具轮 baseline L0 节省预算 | v4.6.0 |
| 2026-06-07 | topic_boost 不消耗检索预算 | 父对话 topic L2 时工具轮自动升 L1 | v4.6.0 |
| 2026-06-07 | CA_CONTEXT_LENGTH=100000 | 匹配 128K+ 模型窗口，消除假约束 | v4.6.0 |
| 2026-06-07 | read_file L0 行数取 JSON total_lines | 避免 json.dumps 转义导致的 split 计数错误 | v4.6.0 |

---

### v4.7.0 — L1 摘要系统重构（2026-06-07）

全面吸收白皮书 v1.2 Gold Master 设计，重构 L1 摘要生成链路，采用 PDD（Prompt-Driven Development）范式。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **ca/post_process.py**（新增） | `parse_v1_markdown_xml` 防御性解析器 + `_safe_truncate` 智能截断 + `_json_to_v1_markdown` 格式转换 | `ca/post_process.py` |
| **ca/prompts.py** | 替换为"研发对话意图分析器"人设，4 类 Markdown + `<core_change>` XML 标签 | `ca/prompts.py` |
| **ca/__init__.py** | 新增 `L1TruncatedException`；`_call_llm_for_l1` 返回 `Tuple[str,str]`；截断检测下沉至 `_run_c_stage`；独立 `temperature`/`max_tokens` | `ca/__init__.py` |
| **ca/config.py** | 新增 `L1_TEMPERATURE`(0.3) + `L1_MAX_TOKENS`(800) + validate + reload | `ca/config.py` |
| **ca/ooda_parser.py** | `TITLE_ALIASES` 扩展 4 类中文别名 | `ca/ooda_parser.py` |
| **ca/store.py** | `format_previous_summary_for_prompt` 历史适配器 | `ca/store.py` |
| **ca/lstage.py** | 同步新签名 + 截断检测 | `ca/lstage.py` |
| **ca/stats.py** | 新增 4 个统计字段（truncated_fallback / parse_fallback_count / skipped_empty / l1_latency_ms） | `ca/stats.py` |

**设计哲学**：PDD — 模型负责语义理解和 Markdown 续写，Python 代码负责截断检测、格式清洗、边界校验、新旧数据兼容。

### v4.7.1 — L1 状态感知链路集成（2026-06-08）

基于 v1.5.1 Final → v1.6 Final 迭代，新增状态前缀提取与结构化透传，对抗小模型"完成时态"幻觉：

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **ca/post_process.py** | 新增 `ItemState` 枚举（DONE/PLANNED/DISCUSSING/UNKNOWN）+ `STATE_PREFIX_REGEX`（含模块级 fail-fast assert）+ `_normalize_state()`（作用域隔离归一化）+ `parse_core_change_state()`（结构化透传，含管理动作降级）；标题统一"决策与共识"→"决策与方案"；`parse_v1_markdown_xml` 返回三元组 `(l1_dict, Hdl, core_state)` | `ca/post_process.py` |
| **ca/prompts.py** | v1.6 Final 版本，`<example>` 标签 3 场景示例，人设"研发对话意图分析器"，优先级规则（已实施 > 计划 > 探讨），`【】`状态标签 | `ca/prompts.py` |
| **ca/ooda_parser.py** | `TITLE_ALIASES` 增加"决策与方案" | `ca/ooda_parser.py` |
| **ca/store.py** | 新增 `_infer_legacy_state()`（文本自检推断历史状态）+ `MANAGEMENT_ACTION_KEYWORDS` 集成 + `format_previous_summary_for_prompt` 适配 | `ca/store.py` |
| **ca/__init__.py** | 适配新签名；状态注入 `l1_dict["_state"]`，零 schema 变更 | `ca/__init__.py` |
| **管理动作关键词修复**（commit 0ee67e8） | 匹配逻辑修复，含 AGENTS.md 同步 | — |
| **所有测试文件** | 标题统一 + 状态前缀场景 + prompt 检测更新 | `tests/` |

### v5.0-pr1 — 存储重构 + 惰性迁移（2026-06-09）

工具轮数据重构第一阶段，聚焦存储层重构（`ca/store.py`），为 PR2（数据采集重定向）+ PR3（三级注入）奠定基础。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **v5 turn_cache schema** | 新主键 `(session_id, turn_index, api_call_count, seq_index)`；消息独立列（role/content/tool_call_id/tool_name/tool_calls_json/finish_reason）；元数据列（api_request_id/duration_ms/status/error_type/error_message/usage_json） | `ca/store.py` |
| **v4 向后兼容** | `turn_type` / `tool_sub_index` / `Elm` 作为 `GENERATED ALWAYS AS STORED` 虚拟列保留至 PR2 | `ca/store.py` |
| **turn_plan PK 扩展** | 含 `api_call_count` + `seq_index`，支持逐工具调度 | `ca/store.py` |
| **Readonly 模式** | `SQLiteStore(path, readonly=True)` 以 `?mode=ro` 打开 v4 旧库只读 | `ca/store.py` |
| **版本路由** | `_readonly` 标志控制 v4/v5 查询路径 | `ca/store.py` |
| **writable guard** | v4 DB 通过可写模式打开时 `RuntimeError` 阻断 | `ca/store.py` |
| **`write_tool_group()` stub** | 定义接口契约（PR2 实现） | `ca/store.py` |

**设计文档**：
- `pr1-store-plan.md`：PR1 实现方案（4 视角 34 条意见全部闭环）
- `pr1-review-decisions.md`：多视角审查裁决记录
- 整体技术方案（文档已归档，代码已实施）

### v5.0-pr2 — Buffer层 + 数据采集重定向（R2+R3+R4）（2026-06-09）

基于工具轮重构技术方案实施 PR2，完整 8 步实现。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **ToolGroupBuffer 数据类** | `_tool_buffer: Dict[str, ToolGroupBuffer]`，无锁设计（Hermes 单线程模型） | `plugins/ca_assembler/__init__.py` |
| **3 新 Hermes hook** | `post_api_request`(捕获结构) / `pre_tool_call`(预注册) / `post_tool_call`(填充结果) | `plugins/ca_assembler/__init__.py` |
| **flush_tool_buffer()** | 排序 buffer → write_tool_group → 清空，含结构化日志（≥5 条 `[CA]` 前缀） | `plugins/ca_assembler/__init__.py` |
| **store.write_tool_group()** | 完整实现（含指数退避重试），PR1 仅定义接口 | `ca/store.py` |
| **generate_group_summary()** | 纯文本拼接工具组摘要 `{group_intent, group_result, tool_count, state}`，不调 LLM | `plugins/ca_assembler/__init__.py` |
| **职责分离** | `process_turn_async` 移除 `messages` 参数；`_run_c_stage` 只写 user+final 行；`post_llm_call` 先 flush 再 process | `ca/__init__.py` |
| **容错机制** | pre/post_tool_call 时 buffer 不存在 → auto-create sentinel(api_call_count=999999)；destroy 时 flush 悬挂 buffer | `plugins/ca_assembler/__init__.py` |
| **Hermes 原始 status 透传** | `ok`/`error`/`blocked`/`cancelled` | — |

**测试**：新增 `test_tool_buffer.py`(15 测试)，`test_store.py` 新增 3 测试，`test_plugin.py` 更新 register 断言 + post_llm_call 顺序验证，`test_v440.py` 3 个工具轮测试迁移到 buffer 流程。**零新增回归**。

**冲裁**：PR3（三级摘要+三级注入，R5+R6）按计划 defer 到后续迭代。

### v5.0-pr3 — 三级摘要 + 三级注入（R5+R6）+ cache 新主键（2026-06-09）

基于技术方案完整实施 PR3（8 步），实现三级摘要和三级注入。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`_rebuild_messages_from_cache` 版本路由** | v5 从新列重建消息，v4 从 Elm JSON 重建。含向后兼容扩展（旧 Elm JSON 数组在 content 中时自动展开） | `ca/__init__.py` |
| **`TurnPlanEntry` 扩展** | 新增 `api_call_count`/`seq_index` 字段，`as_dict()` 同步输出 | `ca/__init__.py` |
| **`_compute_turn_plan_v2` 三级判定** | 新增工具组条目，从 cache `tool_group_Fcts` 读取摘要 | `ca/__init__.py` |
| **`_build_messages_from_plan` 三级注入** | 工具组条目注入 `[~/N/g]` 标记，调用 `_format_group_summary()` 格式化 | `ca/__init__.py` |
| **`_format_group_summary()`** | 工具组 L1 JSON → 可读文本，格式 `工具组：intent→result（N个，state）` | `ca/__init__.py` |
| **`_CA_TAG_RE` 更新** | `r'^\[~/\d+(?:/\d+|/g)?\]\s*'` 匹配三类标记 | `ca/__init__.py` |
| **`_deduplicate_messages` 适配** | 注释更新三位格式 `[~/N/0]`/`[~/N/g]`/`[~/N/M]` | `ca/__init__.py` |
| **`AssemblyCache` 工具组缓存** | 新增 `tool_group_Fcts`/`tool_group_Hdls`，`CacheBuilder.build()` 检测 `tool_calls_json` 非空行关联到工具组 | `ca/cache.py` |
| **`store.py` 适配** | `read_turn_texts`/`read_assemble_status`/`increment_backfill_attempts` 支持 `turn_type="tool_group"` 映射 role='assistant' | `ca/store.py` |

**测试**：新增 `test_pr3_injection.py`（8 测试），覆盖版本路由(3)、三级判定注入(3)、cache 新主键(1)、端到端(1)。**零新增回归**。

### 关键决策记录（补充）

|| 日期 | 决策 | 理由 | 版本 |
|------|------|------|------|
| 2026-06-06 | 去重标记留最先+原位指向 | 首次出现位置不动→前缀稳定，标记在删位不污染幸存者 | v4.5.1 |
| 2026-06-07 | PDD 范式替代纯 OODA 文本 | LLM 输出 Markdown+XML，Python 防御性解析，消除 OODA 解析不稳定根因 | v4.7.0 |
| 2026-06-08 | L1 状态感知链路 | `【】` 状态前缀 + 归一化，对抗小模型&quot;完成时态&quot;幻觉 | v4.7.1 |
| 2026-06-09 | v5 turn_cache schema 重构 | 消息独立列替代 Elm JSON，工具轮逐工具调度 | v5.0-pr1 |
| 2026-06-09 | ToolGroupBuffer 替代消息遍历 | 实时 buffer 采集替代 post_llm_call 全量遍历 | v5.0-pr2 |
||| 2026-06-09 | 三级注入标记 `[~/N/g]` | 工具组独立于对话轮注入，格式统一三位标记 | v5.0-pr3 |
||| 2026-06-10 | bypass 尾区保护 | `_bypass_skip=3`，保护最后 2 完整对话轮 + 当前 Q | v5.1 |
||| 2026-06-11 | bg_review 独立管道 | C-stage 正常积累，A-stage 从摘要索引移除 | v5.1 |
||| 2026-06-11 | state DB 污染切断 | post_llm_call 共享 dict 就地恢复 | v5.1 |
||| 2026-06-11 | biz_category 实装 | 双表加列 + C-stage 写/A-stage 消费 | v5.1 |
||| 2026-06-12 | tool 行 content 清空 | content→单空格，94 行 340K chars → 94 chars | v5.1 |
||| 2026-06-12 | bg_review 内容清空 + 尾区保护 | mutation 循环内按位置边界保护尾区 bg_review | v5.1 |
|| 2026-06-09 | 工具组 L2：thought 原文 → 完整消息序列展开 | `_extend_with_l2` 替代只提 thought；covered 集加 `(turn_idx, "tool")` 防重复；L1 fallback 用 `_format_group_summary` 替代 raw JSON `l1_display` | v5.0-pr3 |

| **v5.5.0**            | 2026-06-16 | 命名统一        | 全部 `l1_text`/`l0_text`/`l2_text` 重命名为 `Fct`/`Hdl`/`Elm`（DB 列名 + Python 标识符 + 文档术语）。`l1_embedding`/`l0_embedding`/`l2_tokens` 同步重命名。279 个 DB 文件 ALTER TABLE 迁移。| 
