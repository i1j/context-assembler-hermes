# ContextAssembler 开发要事记

## 演进路线图

```
v0.1 → v0.2          v1.0 → v1.1 → v1.2       v2.0 → v2.1 → v2.2
问题识别 → 原型验证   独立管线 → 工程加固 → 存储   集成架构 → 实现 → 引擎

v3.0 → v3.1          v4.0 → v4.1              v4.1-rc1 → v4.1-rc2 → v4.1-hotfix-1
功能完整 → 测试       管线整合 → 设计增强         评审 → 逐项审查 → 生产就绪

v4.2                  v4.3 → v4.3-s1 → v4.3-s2    v4.3.1 → v4.3.1 修订
配置+健康检查          异步化 → 自查 → 修复         调试增强 → 当前版

v4.4.1 → v4.5.0 → v4.5.1    v4.6.0 → v4.7.0 → v4.7.1
尾区分隔 → plan驱动 → 纯plan   话题拣选 → L1 PDD → 状态感知

v5.0-pr1→pr2→pr3→pr4    v5.1 → v5.2 → v5.2.1 → v5.3.0
存储重构 → buffer → 三级注入  注入重构 → 缓存分析 → 话题阈值 → 多对标签

v5.5.0                      v5.10 → v6.0.0
命名统一+话题分割+A-stage重构  CE shell → 方向B conv_history重建

v6.0.1 → v6.0.2 → v6.0.3 → v6.0.4 → v6.0.5
Fct告警 → 首轮Fct → 空字段过滤 → HEAVY_FIELDS → CE停用
```
## 版本历史

| 版本 | 日期 | 阶段 | 变更摘要 | 状态 |
| --- | --- | --- | --- | --- |
| **v0.1** | 2026-05-16 | 问题识别 | Hermes ContextCompressor 被动 LLM 压缩导致上下文质量下降；Qwen3.5 thinking 层无法关闭，JSON 输出不稳定。提出"增量提取 + 结构化存储"初步构想。 | 调研 |
| **v0.2** | 2026-05-17 | 原型验证 | 本地 Ollama Qwen3.5-9B 测试：`think=False` 无效，JSON 输出成功率 < 60%，OODA 文本格式稳定。确定"LLM 输出 OODA 文本 + 代码后处理"技术路线。 | 原型 |
| **v1.0** | 2026-05-18 | OODA 管线独立开发 | 构建独立 OODA 增量提取管线：`OODAParser`（模糊标题匹配）、`robust_json_parse`、`clean_increment`。Prompt 优化：结构强制、歧义消除、容错强化。去重策略：向量余弦相似度（阈值 0.88）。 | 原型 |
| **v1.1** | 2026-05-19 | 管线工程加固 | 增加 `_parse_method` 标记（direct/bracket_repair/regex_fallback）、`_truncated` 检测。引入 `EmbeddingService` 抽象（Ollama / sentence-transformers）。设计 LRU 缓存与预热机制。 | 开发 |
| **v1.2** | 2026-05-19 | 存储层设计 | 方案设计：SQLite + sqlite-vec 虚拟表 + HNSW 索引。讨论 PostgreSQL + pgvector 替代方案。确定消费级硬件优先，选择 SQLite。**后于 v4.2 修正版弃用 sqlite-vec，改用纯 SQLite 存储，向量检索由自实现余弦相似度完成。** | 设计 |
| **v2.0** | 2026-05-20 | 集成架构设计 | 将 OODA 管线集成到 Hermes Agent 的 ContextEngine：提出 C/A 两阶段架构。C-stage：生成 L1 增量摘要。A-stage：同步检索 + 预算闸门组装上下文。设计 BM25 + 向量双路检索 + RRF 融合。 | 设计 |
| **v2.1** | 2026-05-20 | 存储与检索实现 | 实现 `SQLiteStore`：WAL 模式、schema 版本化、`_pack_f32/_unpack_f32` 序列化。实现 `AssemblyCache` + `BM25Okapi`（自实现，零依赖）。实现 `Retriever`：余弦相似度（numpy-free）、RRF 融合、动态分配。 | 开发 |
| **v2.2** | 2026-05-21 | 核心引擎实现 | 实现 `ContextAssembler` 主类：`process_turn()`（C-stage）、`assemble()`（A-stage）。`_generate_l1()` 调用 Ollama 生成 OODA 文本。`_pre_assemble()` 构建压缩消息列表。`_vector_recall()` 预算闸门。`_hard_truncation()` 最后防线。 | 开发 |
| **v3.0** | 2026-05-21 | 功能完整 | 首次完整集成：C/A 两阶段 + BM25+向量双路检索 + Token 预算闸门 + OODA 文本输出。性能实测：A-stage ~80ms, C-stage ~44s。VRAM 占用 10.7GB/12GB。进入测试阶段。 | 测试 |
| **v3.1** | 2026-05-22 | 测试完善 | 编写测试套件：`test_prototype.py`（42 测试）、`test_edge_cases.py`（5 测试）、`test_stress_real.py`（真实数据压测）。识别：`think=False` 无效、冷启动延迟、`num_predict=2048` 为平衡点。 | 测试 |
| **v4.0** | 2026-05-22 | 管线整合 | 将 OODA 管线成熟组件（`OODAParser`、`robust_json_parse`、`clean_increment`）嵌入 CA C-stage。增加 `_parse_method` 标记和 `_truncated` 检测。设计 `CachedEmbeddingClient`。 | 开发 |
| **v4.1** | 2026-05-23 | 设计增强 | 动态预算闸门（`_adaptive_budget`）、Embedding 并行编码（`embed_batch_parallel`）、SQLite 后台 checkpoint、Embedding 预热、OODA Prompt 增强。完成设计文档 v4.1。 | 设计完成 |
| **v4.1-rc1** | 2026-05-23 | 内部评审-1 | 发现高危缺陷：Elm 提取正则不匹配（永远无法命中）、`_vector_recall` 缺少 `max_upgrade_k` 上限、`_generate_l1` HTTP 调用脆弱（无重试/无 stop）、缺少 Embedding 预热。 | 未通过 |
| **v4.1-rc2** | 2026-05-23 | 内部评审-2 | 模块逐项审查（store/embedding/ooda_parser/retrieval/cache）：发现 28 项问题（8 P0 + 20 P1），含跨线程 SQLite 共享、`lru_cache` 线程不安全、BM25 映射隐式排序依赖、CJK 分词 Unicode 范围过宽、连接池关闭不完整等。 | 未通过 |
| **v4.1-hotfix-1** | 2026-05-24 | 最终交付 | 修复 8 个 P0 + 20 个 P1。线程安全 SQLite 连接（`threading.local`）、LRU 缓存手动实现+`RLock`、BM25 显式排序、CJK 分词精确化、服务降级完善、连接池完整关闭、维度自动检测、输入验证增强。交付完整代码库，通过全面内部评审。 | **生产就绪** |
| **v4.2** | 2026-05-24 | 配置与健康检查 | 新增 `config.py` 集中配置管理、`health.py` 健康检查与 Prometheus 指标导出。修正嵌入模型默认值、移除未实现的 sqlite-vec 和时序衰减、标注可选依赖。增加部署架构图、性能调优指南、故障排查手册。 | 发布 |
| **v4.3** | 2026-05-24 | 异步化重构 | C-stage 全面异步化：`process_turn_async()` 后台线程执行，内存计数器管理 `turn_index`。真实 A-stage 组装逻辑（`_build_final_messages`），支持 `[~/N]` 标记、Head/Middle/Tail 分层。`should_compress()` 固定返回 `False`。 | 发布 |
| **v4.3-selfcheck-1** | 2026-05-24 | 内部自查 | 发现 `turn_index` 重复覆盖风险（异步写入 DB count 滞后）、`_run_c_stage` 崩溃降级静默失败、`assemble()` 缺少输入校验、`wait_for_pending` 超时与 LLM 时延不匹配。 | 未通过 |
| **v4.3-selfcheck-2** | 2026-05-24 | 修复与增强 | 修复全部问题：`turn_index` 改用原子计数器、降级写入增加错误日志、`assemble()` 输入与溢出保护、`wait_for_pending` 超时基于 `Config.LLM_TIMEOUT`、消息组装完整分层逻辑。 | 发布 |
| **v4.3.1** | 2026-05-24 | 调试增强 | 新增轻量调试开关 `CA_DEBUG`（环境变量驱动），7 个关键数据流观测点。线程安全补充说明。可观测接口汇总补充 `CompressStats`。**通过自我审查，文档与代码对齐。** | **生产就绪** |
| **v4.3.1 修订** | 2026-05-24 | 文档修订 | 三项不一致修正：调试观测点与实际代码对齐、可观测接口补充 `CompressStats`、`CA_PROTECT_TAIL_TOKENS` 与代码统一。增加 A-stage 溢出保护和线程安全读缓存设计说明。 | **最终版** |
| **v4.3.2-design** | 2026-05-25 | 全指纹去重（设计） | 6 项需求（REQ-FUNC-DEDUP-001 ~ 006）：指纹算法（SHA256 + json.dumps）、系统消息豁免、可配置开关、性能基线。正向反向追溯表、边界场景全覆盖。 | 设计冻结 |
| **v4.3.2-integration** | 2026-05-25 | 包装层修复部署 | CA 包装层 4 个线上 bug 归零（#1 pre_llm_call 钩子、#2 int→str 类型、#3 threshold_tokens、#4 post_llm_call），test_plugin.py 15/15 通过。 | **已修复** |
| **v4.3.2-testplan** | 2026-05-25 | 全指纹去重（测试） | 22 个测试用例（9 功能 + 10 边界 + 2 集成 + 1 性能），tool 消息独立验证、tool_calls 指纹、dict/list 类型 content 覆盖。 | **待验证** |
| **v4.3.2-impl** | 2026-05-25 | 全指纹去重（实现） | 实现 `_deduplicate_messages`、`Config._parse_bool_env`、DEDUP_ENABLED、CA_DEBUG 日志。22 个测试函数落盘。 | **待提交** |
| **v4.3.2-ooda-fix** | 2026-06-01 | 调查：OODA 解析器冒号剥离 | `_extract_sections` content 提取后前导冒号未剥离，导致 `core_change` 带全角冒号前缀。 | **已归档** |
| **v5.0-pr4-inject-fix** | 2026-06-09 | 注入层微修复批 | read_turn_texts l1/l0 缺 api_call_count 过滤（同 turn 多工具组 l1 相同）；_format_group_summary thought+intent 重复；L0 工具组缺"工具组："前缀；_format_l1_for_display 占位符噪声。 | **已修复** |
| **v4.4.0-ooda-fix** | 2026-06-03 | 修复：OODA 解析器前导冒号 | 在 `_extract_sections` content 提取后追加 `.lstrip(":：　 ")`，去除全角/半角冒号。 | **已修复** |
| **v5.1** | 2026-06-09 | Replace 模式注入重构 + bypass 统一 | 三模式（Replace/Append/Off）+ bypass_turns 统一 + 20K 尾区保护 + biz_category 双向嵌入 + state DB 污染切断 + tool 行 content 清空 + bg_review 尾区保护。详见下方详细章节。 | **已发布** |
| **v5.2** | 2026-06-13 | 缓存分析 + 注入重构 | ① hdl_embedding 孤儿数据清除 ② tool_plan 独立 tool 行决策 + `[~/N/M]` 标签 ③ bg_review 轮从 DB 读数替代空格占位。详见 [v5.1 分析报告](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md)。 | **发布** |
| **v5.2.1** | 2026-06-14 | 话题分割修复 + 自适应阈值 | ① `_add_bigrams` 集合排序稳定修复 ② Jaccard 独立合并路径（默认 0.18） ③ 自适应阈值模块 + `topic_threshold_meta.json` 持久化 ④ 跨会话加权漂移（0.6×last + 0.4×avg）。 | **已实施** |
| **v5.3.0** | 2026-06-17 | Fct 提示词多对标签重构 | FCT_GENERATION_PROMPT 从「单一 stage_tag 合并多状态」改为「每对 stage_tag/core_change 仅含单一状态」。PAIR_PATTERN 多对解析，全链路适配。242 测试通过。 | **已实施** |
| **v5.5.0** | 2026-06-15 | 命名统一 + 话题分割重构 | Elm/Fct/Hdl 重命名 + 话题分割 + A-stage 角色队列匹配 + E-stage write-on-receive + turn_stream 新表 + F-stage Phase 1-4 + 死代码清理。详见下方详细章节。 | **已发布** |
| **v5.10** | 2026-06-22 | CE route shell 基线 | v6.0 CE 路线实现前的代码基线。实现 `CAContextEngine` 单例注册 + CE ABC 接口。后继 v6 方向 B 改用 turn_stream DB 重建 conv_history。 | **基线** |
| **v6.0.0** | 2026-06-22 | CE 路线 + 方向 B conv_history 重建 | 从 turn_stream DB 重建 conv_history，替代 mutation。方向 A（CE route shell）→ 方向 B（_build_conv_history_v6）。CE 管线全面停用（v6.0.5）。详见下方详细章节。 | **已发布** |
| **v6.0.1** | 2026-06-28 | Fct=NULL 告警 | A-stage 新增 Fct=NULL 告警，消除静默 Elm 回退。冗余赋值清理。测试 xfail→pass。 | **已实装** |
| **v6.0.2** | 2026-06-28 | 首轮 Fct 修复 + 截断 fallback | 首轮 Fct band-aid 移除、截断优先 PAIR_PATTERN 提取 XML 对、Tool Fct 代码体剥离、L1_MAX_TOKENS 800→2048。 | **已实装** |
| **v6.0.3** | 2026-06-28 | Tool Fct 空字段过滤 | `_select_content` 对 tool Fct 空字段剔除，单位 Token 互信息最大化（~650 tok/会话）。result_summary 去命令冗余。 | **已实装** |
| **v6.0.4** | 2026-06-28 | HEAVY_FIELDS 审计修复 | skill_manage 补旧/新/content、memory 条目＋handler、5 handler 漏接 _clean_tool_args 补全、terminal command[:100] 保留。 | **已实装** |
| **v6.0.5** | 2026-06-28 | CE 管线全面停用 | should_compress → False, compress → no-op。方向 A 死代码删除。CE 管线双写漏洞根除。 | **已实装** |
| **v6.1** | 2026-07-29 | 话题摘要 v4-v7 + wiki merge 演进 | 本地话题摘要取代 OV Memory Provider；consumable 空洞保护；merge Jaccard→centroid（v5.19 动态阈值 wiki_meta）；L4 空闲精炼；确认性豁免 ≤5；Jaccard 0.95 近精确去重。详见 28-topic-summarization-v4.md。 | **已实装** |
| **v6.2** | 2026-07-31 | 测试适配收尾 + 注入预算 v8 | 话题摘要 v8：注入预算 2000 + 迭代提炼（3 轮上限 + hdl 兜底质量过滤）。v5 mutation 测试清理：删除 test_a_stage.py（_simple_mutation_mode_v5/_incremental_mutation/快照/增量缓存 2026-06-28 已废弃），替代 test_build_conv_history_v6.py；test_a_stage_topic_aware 仅保留真实 mgr 集成；test_plugin 适配 v6.2 本地召回（_ov_find_ca_topics → query_wiki_by_semantics）；debug dump 受 DEBUG_MODE 控制（修复无条件写 /tmp）。 | **已实装** |
| **v6.3** | 2026-07-31 | 话题分割 Jaccard 输入归一化（TP-001 缺陷族根治） | Fct JSON 公共键名（core_change/changes/stage_tag/_assemble_status）与元数据值跨话题恒定，把无关话题 Jaccard 抬过 ENTRY 阈值（"数据库设计"vs"Python优化" 原始 JSON j=0.0714 ≥ 0.04 → 虚假延续；真实 DB 完整形状 j≈0.12）。新增 `_extract_fct_semantic_text`：`_extract_turn_fct` 返回前剥离 JSON 键名 + 跳过元数据键与非字符串值（仅剥离键名不够：`_assemble_status:0` 的数字仍共享，j≈0.074）。test_a_stage_topic_aware 移除强制分割短语拐杖，改为 Jaccard 自然分割回归验证（反证：回退修复后该测试失败）。 | **已实装** |
| **v6.4** | 2026-07-31 | 话题块 centroid JSON 键名污染修复（TP-001 缺陷族同源，embedding 路径） | `_compute_centroids`（topic_manager.py）直接 `embed(row[5])` 原始 Fct JSON——公共键名 token 抬高无关话题 centroid 相似度 → 半径定级失真。改为 `embed(_extract_fct_semantic_text(fct_text))`，与 Jaccard 路径一致。TDD：新增 `test_embed_input_strips_json_keys`（捕获 embed 输入断言无键名/花括号/元数据），topic_manager 108 passed。**审计结论**：5 条 embedding 路径中仅此 1 条未剥键名（Jaccard/Jaccard 摘要/wiki merge/查询向量均已纯文本）；strand 规范=只 embed 语义文本。 | **已实装** |
| **v6.4.1** | 2026-07-31 | **Strand 重构 + Bug 1/2 修复（话题摘要单元 topic → strand）** | **Bug 1**：`parse_summary_response` 丢弃 ooda_groups/strands → 86% 话题 OODA 全落"其他"；透传修复。**Bug 2**：`_apply_hdl_fallback` 超预算清空内容 → 有内容话题被误 skip；改为仅降级 title 保留内容。**Strand 重构**：摘要单元从话题块 → 事务级 strand（4B 识别 2-6 个，宁多勿少 + turns 元数据），`_assemble_summary` 支持 strands 分支 + `_fallback_single_strand` 兜底；store schema 三表重构（strand_summaries/wiki_strand_map/topic_wiki.source_strands，旧表 topic_summaries/wiki_topic_map 不向前兼容，旧数据重建丢弃）；TOPIC_SUMMARY_MAX_CHARS 2000→4000（多 strand ~3174 实测）；prompt 输出格式 ooda_groups → strands。TDD 31 单测 + 10 store 测试。 | **已实装** |
| **v6.4.2** | 2026-08-01 | **摘要 num_predict 独立配置（架构债修复）** | 重跑验证发现 46% 话题走代码 fallback——`call_llm_for_summary` 复用 F-stage 的 `L1_MAX_TOKENS=2048`，多 strand 输出（实测 ~2538 字符）被 `stop=length` 截断 → JSON parse 失败。新增 `TOPIC_SUMMARY_MAX_TOKENS=4096` 独立配置，摘要/标题链路改用（f_stage 不受影响）。修复后批量重跑 19 个 ms* 会话：OODA 覆盖率 65%→**100%**，ooda_other_only 37→**0**，skip 率 21%→**10.9%**，孤 strand=0，单轮话题正确拆分 5-7 strand。全量 488 passed。 | **已实装** |
| **v6.4.3** | 2026-08-01 | **strand hdl 质量修复（hdl 命名规范 + P0/P1 缺陷）** | **hdl 命名规范**：`TOPIC_SUMMARIZE_PROMPT`/`REFINE_SUMMARY_PROMPT` 增加 Fct 风格规范（基于 ooda 总结 + ✅正例/❌反例 + 禁止代码符号名）。全量重跑验证：hdl 纯 ASCII 79%（130/164）→**0%**（145/145 全中文语义名），TDD `TestPromptHdlNamingRule` 5 用例。**P0-1**：`changes = llm_result.get("changes") or fallback_changes`——4B 返回空列表时不再吞掉代码提取（strand 35 ooda 全空根因）。**P0-2**：`_build_hdl` 超长截断 ≤30 chars（`_HDL_MAX_LEN` + `_truncate_hdl`）。**P1-1**：4B 退化输出（无 strands 且无 ooda_groups）重试一次。**P1-2**：`_fallback_single_strand` hdl 超长降级为首条 change 摘要。定向修复 4 个问题 topic（strand 35: 123ch+ooda空 → 5 条健康 strand）。全量 549 passed。 | **已实装** |
| **v6.5.4** | 2026-08-06 | **wiki 全量重建链路适配 themes（修复 topic_wiki 表不存在断裂）** | **背景**：v6.5 schema 迁移（topic_wiki → themes）只同步了增量链路（graphify_sync.py），全量重建链路漏迁移——`scripts/wiki_to_graph.py` 主查询读已废弃的 topic_wiki 表 → 运行时 `no such table` 崩溃，主图知识节点（旧图 427 个）无法重建，verification 第 5 项缺失。**修复**：① wiki_to_graph.py `build_wiki_subgraph` 主查询 topic_wiki → themes，节点 id `theme_{id}` 对齐增量命名（幂等）；② `store.build_wiki_associations` 读 themes + 写入 `wiki_associations.theme_id`（旧库 entry_id 列由 `_migrate_wiki_associations_column` 自动 RENAME COLUMN）；③ refinement.py 7 处 SQL + upsert_wiki_entry 适配 themes（停用开关不变）；④ evaluate_pipeline.py 适配 themes/theme_strand_map；⑤ 清理 store.py 18 个死函数（topic_wiki/wiki_strand_map/topic_summaries 残留，共 764 行）。**验证**：全量重建 912 知识节点（410 themes + 502 strand）+ 134,029 条关联，幂等；pytest 783 passed。 | 已修复 |
| **v6.5** | 2026-08-01 | **Wiki Theme 生成重构（参考 strand 链路，三级信息导向）** | **背景**：wiki 层无 4B 参与——overview 恒空（117/117）、title=strand hdl 直继承、merge 纯规则字符去重。**决策（用户 M1-M9）**：theme = 切换话题时提供给云端大模型的背景参考；注入 = 当前详细状态（title+overview+OODA 四组+key_facts+open_items，互信息量优先级裁剪 P0-P7，单 theme 预算 2000）；时间线 = 二级信息（仅 overview 条目，按主题块追加，不设上限，历史追溯靠 theme_strand_map→strand）；归并 = 单向否决（0.70 余弦候选 + 4B 否决，merge 窄 / strand 宽 → 收敛，例外留精炼轮）；schema 彻底重构（themes/theme_strand_map 替代 topic_wiki/wiki_strand_map，不向前兼容）。**实现**：`ca/theme.py`（prompt 双式 create/merge + 决策 + 两段式 `run_theme_merge` + 代码兜底）；`_run_topic_summarize` 尾部同步归并（M2）；`_run_wiki_merge` 移除；`_format_wiki_carryover` 重构为当前详细状态+优先级裁剪；reprocess Step 3 适配；refinement 停用（schema 连锁，待下次会话适配 themes）。TDD 全量 603 passed。**数据验证（真实 4B 重跑 148 strands，failed=0）**：overview 0%→97.8%、ooda 100%、孤 strand=0、title 0 ASCII、跨 session 真语义合并 5 例、注入召回 3 themes≈2.2K chars。 | **已实装** |
| **v6.5.1** | 2026-08-01 | **create 缺 overview 兜底（P2，对齐 strand P1-1 模式）** | 评估发现 3/138（2.2%）theme 的 4B create 返回完整 ooda 但 overview 为空 → 注入时 [当前状态] 段缺失。修复：`run_theme_merge` create 分支检测「overview 空但有 ooda」→ 重试一次（温度抖动防御）；重试仍缺 → `_pad_overview_from_ooda` 代码拼接（决策与方案 + 现象与问题，≤3 条分号连接）。4B 完全失败（None）不重试（仍走 hdl fallback）。TDD 4 用例（重试成功/重试失败拼接/正常不重试/None 不重试），全量 608 passed。 | **已实装** |
| **v6.4** | 2026-07-31 | 话题摘要质量审计：定位 2 个缺陷（已修复 → v6.4.1） | **Bug 1**：`parse_summary_response`（topic_summary.py:375-389）只提取 changes/key_facts/title/consumable，**丢弃 4B 输出的 ooda_groups**（实测 4B 原始响应含该键）→ `_assemble_summary` 永远走 `_fallback_ooda_groups` → 43 completed 中 37 个（86%）OODA 全落"其他"。**Bug 2**：`_apply_hdl_fallback`（topic_summary.py:719-726）超预算清空 changes/key_facts → 调用方 hollow 判定误 skip（重跑中 4 个有内容话题被误杀，含 ms718ecxpes9v7:6 的 Elm/Fct 字段差异讨论）。修复计划：parse 透传 + hdl fallback 仅降级 title。→ **已随 v6.4.1 修复**。 | **已修复** |
| **v6.4** | 2026-07-31 | Strand 多事务摘要重构（已实现 → v6.4.1） | 以 strand（事务/工作线）取代话题块作为摘要单元：一个话题块内 4B 识别 2-6 个 strand，各有 hdl/turns/独立 OODA 四组；wiki 重构（strand 为 source 颗粒，不向前兼容）；A-stage 沿用话题块 grade 不动；宁多勿少 + 识别失败降级单 strand。详见 35-strand-multi-affair-summarization.md + `.hermes/plans/2026-07-31_ca-strand-refactor.md`。→ **已随 v6.4.1 实装**。 | **已实装** |

---

### v0.1 — 问题识别（2026-05-16）

| 变更 | 说明 |
|------|------|
| **发现** | Hermes 内置 `ContextCompressor` 使用被动 LLM 压缩，导致上下文质量下降、信息丢失 |
| **调研** | Qwen3.5-9B 在 Ollama 0.24.0 上 `think=False` 参数无效，thinking 层消耗 ~90% 生成预算 |
| **初步构想** | 增量提取 + 结构化存储替代被动压缩 |

### v0.2 — 原型验证（2026-05-17）

| 变更 | 说明 |
|------|------|
| **测试** | 本地 Qwen3.5-9B JSON 输出测试，成功率 < 60%，常出现空串、格式错误 |
| **发现** | OODA 五节自然语言文本格式稳定，成功率 > 95% |
| **决策** | 确定"LLM 输出 OODA 文本 + 确定性代码解析"技术路线 |

---

### v1.0 — OODA 管线独立开发（2026-05-18）

| 变更 | 说明 |
|------|------|
| **`OODAParser`** | 模糊标题匹配（25+ 别名），两阶段提取（锚点定位 + 切片提取），避免正则回溯 |
| **`robust_json_parse`** | 容错解析（补全括号、正则兜底），4 种 `_parse_method` 标记 |
| **`clean_increment`** | 截断数据丢弃，空字段清理 |
| **向量语义去重** | 余弦相似度阈值 0.88，复用 Embedding 模型 |
| **Prompt 优化** | 结构强制（五节标题）、歧义消除（禁用冒号/括号）、容错强化（空节写"无"） |

### v1.1 — 管线工程加固（2026-05-19）

| 变更 | 说明 |
|------|------|
| **可观测性** | `_parse_method` 标记区分解析路径，`_truncated` 检测截断 |
| **`EmbeddingService` 抽象** | 支持 Ollama、sentence-transformers、fallback 三种后端 |
| **LRU 缓存设计** | 缓存重复文本的 embedding，减少 API 调用 |
| **预热机制设计** | 初始化时预编码常用文本，消除首轮延迟 |

### v1.2 — 存储层设计（2026-05-19）

| 变更 | 说明 |
|------|------|
| **方案对比** | PostgreSQL + pgvector + HNSW vs SQLite + sqlite-vec |
| **决策** | 消费级硬件优先，选择 SQLite，保留升级路径 |
| **设计** | `turn_cache` 表（结构化数据）+ `turn_cache_vec` 虚拟表（向量 KNN 搜索） |
| **注** | 最终实现未使用 sqlite-vec 扩展，向量检索由 `retrieval.py` 自实现余弦相似度完成。v4.2 正式修正 |

---

### v2.0 — 集成架构设计（2026-05-20）

| 变更 | 说明 |
|------|------|
| **C/A 两阶段架构** | C-stage（post_llm_call）：生成 Elm 增量摘要；A-stage（pre_llm_call）：同步检索 + Token 预算闸门 |
| **双路检索设计** | BM25 关键词匹配 + 向量余弦相似度，RRF 融合 |
| **预算闸门算法** | `available = context_length × 0.95 - compressed_tokens`，按 delta 决定 Elm→Fct 升级 |

### v2.1 — 存储与检索实现（2026-05-20）

| 变更 | 说明 |
|------|------|
| **`SQLiteStore`** | WAL 模式、schema 版本化（`_meta` 表）、`_pack_f32/_unpack_f32` 序列化 |
| **`AssemblyCache`** | 内存缓存 Elm/Fct 文本和 embedding |
| **`BM25Okapi`** | 自实现，零外部依赖，支持动态添加文档 |
| **`Retriever`** | numpy-free 余弦相似度，RRF 融合，`_dynamic_allocation` 动态分配 |

### v2.2 — 核心引擎实现（2026-05-21）

| 变更 | 说明 |
|------|------|
| **`ContextAssembler` 主类** | `process_turn()`（C-stage）+ `assemble()`（A-stage） |
| **`_generate_l1()`** | 调用 Ollama API，Prompt 格式化 |
| **`_pre_assemble()`** | 构建压缩消息列表，建 `[~/N]` 标记 |
| **`_vector_recall()`** | BM25 + 向量双路检索 + 预算闸门 |
| **`_hard_truncation()`** | 最后防线（head + tail 保留，丢弃中间） |

---

### v3.0 — 首次完整集成（2026-05-21）

| 变更 | 说明 |
|------|------|
| **代码结构** | `ca/__init__.py`（主引擎）、`store.py`、`cache.py`、`retrieval.py`、`stats.py` |
| **性能实测** | RTX 3060 12GB, Ollama 0.24.0：A-stage ~80ms, C-stage ~44s, VRAM 10.7GB/12GB |
| **状态** | 进入测试阶段 |

### v3.1 — 测试完善（2026-05-22）

| 变更 | 说明 |
|------|------|
| **测试套件** | 42 个单元测试 + 5 个边界测试 + 真实数据压测 |
| **识别问题** | Ollama `think=False` 无效、首轮冷启动 ~112s、`num_predict=2048` 为平衡点、OODA 文本为妥协方案 |

---

### v4.0 — 管线整合（2026-05-22）

| 变更 | 说明 |
|------|------|
| **管线嵌入** | OODA 管线成熟组件（`OODAParser`、`robust_json_parse`、`clean_increment`）嵌入 CA C-stage |
| **新增** | `_parse_method` 标记和 `_truncated` 检测 |
| **设计** | `CachedEmbeddingClient` |

### v4.1 — 设计增强（2026-05-23）

| 变更 | 说明 |
|------|------|
| **动态预算闸门** | 根据历史 token 分布自适应安全系数 |
| **Embedding 并行编码** | `embed_batch_parallel` 将 Elm+Fct 编码延迟减半 |
| **SQLite 后台 checkpoint** | 守护线程定期执行 `PRAGMA wal_checkpoint(PASSIVE)` |
| **OODA Prompt 增强** | 结构强制 + 消除歧义 + 容错强化 |
| **文档** | 完成设计文档 v4.1 |

---

### v4.1-rc1 — 首次评审（2026-05-23）

发现 4 个 P0 高危缺陷：

| # | 缺陷 | 影响 |
|---|------|------|
| 1 | `_extract_l0` 正则 `\*\*\s*核心摘要\s*\*\*` 与纯文本格式不匹配 | Elm 永远为空 |
| 2 | `_vector_recall` 缺少 `max_upgrade_k` 守卫 | Token 爆炸风险 |
| 3 | `_generate_l1` 使用 `urllib.request` 无重试机制 | 生产不稳定 |
| 4 | 缺少 Embedding 预热 | 首轮延迟 > 180ms |

### v4.1-rc2 — 模块逐项审查（2026-05-23）

覆盖 `store.py`、`embedding.py`、`ooda_parser.py`、`retrieval.py`、`cache.py`、`__init__.py`。识别 28 项问题（8 P0 + 20 P1）：

| 模块 | P0 | P1 | 主要问题 |
| --- | -- | -- | --- |
| `store.py` | 1 | 7 | 单连接多线程共享、checkpoint 无 WAL 配置、事务粒度粗 |
| `embedding.py` | 1 | 6 | `lru_cache` 线程不安全、连接池关闭不完整、维度硬编码 |
| `ooda_parser.py` | 2 | 5 | 无 numpy 降级、向量未归一化、串行 embedding 瓶颈 |
| `retrieval.py` | 1 | 5 | BM25 映射验证、向量长度验证、RRF 去重 |
| `cache.py` | 2 | 4 | BM25 映射排序、CJK 分词不准确、输入验证 |
| `__init__.py` | 3 | 5 | 预算负值、并发竞争、`atexit` 注册错误、warmup 阻塞 |

### v4.1-hotfix-1 — 最终交付（2026-05-24）

修复 8 个 P0 问题：

| # | 问题 | 修复 |
|---|------|------|
| 1 | SQLite 线程安全 | `threading.local()` 每线程独立连接 |
| 2 | LRU 缓存线程安全 | 手动实现 + `RLock` 替代 `@lru_cache` |
| 3 | BM25 映射 | 显式排序 `records` + 跟踪 `l1_turn_indices` |
| 4 | CJK 分词 | 精确 Unicode 范围判断 |
| 5 | Elm 提取 | 直接使用 `OODAParser` 的 `core_change` 字段 |
| 6 | 预算闸门 | `max_upgrade_k` 统一守卫 |
| 7 | HTTP 调用 | 重试 + stop 参数 + eval_count 守卫 |
| 8 | 服务降级 | Ollama 失败自动回退 fallback |

修复 20 个 P1 问题：跨模块增强输入验证、统计信息原子性、连接池完整关闭、维度自动检测、超时优化、错误处理完善等。**交付完整代码库（9 个文件），通过全面内部评审，批准进入生产部署。**

---

### v4.2 — 配置中心化与健康检查（2026-05-24）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **新增 `config.py`** | 统一管理所有可调参数，通过 `ClassVar` 暴露，支持环境变量覆盖 | `ca/config.py` |
| **新增 `health.py`** | `HealthCheck` 类 + 组件级/聚合健康检查 + Prometheus 指标导出 | `ca/health.py` |
| **修正** | 嵌入模型默认值统一、移除未实现的 sqlite-vec 描述、移除未实现的时序衰减条目、标注可选依赖 | 多个文件 |
| **文档** | 部署架构图（Mermaid）、性能调优指南、故障排查手册 | `docs/` |

---

### v4.3 — 异步化重构（2026-05-24）

| 变更 | 说明 |
|------|------|
| **触发** | 生产测试发现 C-stage 同步调用 LLM 阻塞用户消息管道，数据库无数据，A-stage 无缓存可读 |
| **异步 C-stage** | `process_turn_async()` 立即返回 `turn_index`，后台线程执行 LLM + 解析 + 嵌入 + 写入。`_run_c_stage()` 后台线程完整管道，超时重试 + 降级写入。`_pending_tasks` 字典追踪活跃任务 |
| **内存 turn_index 管理** | `_turn_counter` 原子递增，启动时从数据库恢复最大值 |
| **真实 A-stage 组装** | `_build_final_messages()` 实现完整分层逻辑：系统消息保留、工具调用保留、头部自动 Fct、中间 Elm/Fct 按升级列表、尾部保留原文。添加 `[~/N]` 标记前缀 |
| **中间层调整** | `should_compress()` 固定返回 `False`；`_on_pre_llm_call` 原地修改 `conversation_history`；`on_session_end` 等待 C-stage 任务完成 |

### v4.3-selfcheck-1 — 异步实现自查（2026-05-24）

| P级 | 问题 | 说明 |
|-----|------|------|
| P0 | turn_index 分配竞态 | 基于 `session_turn_count` 分配 → 异步写入延迟导致重复覆盖 |
| P0 | 降级写入静默失败 | `_run_c_stage` 崩溃时降级摘要写入失败被忽略 |
| P1 | assemble() 缺少校验 | `messages` 参数无类型检查和空值处理 |
| P1 | hook 返回值策略不明 | 原地修改 vs 返回新列表 |
| P2 | wait_for_pending 固定超时 | 30s 与 LLM 调用 120s 不匹配 |
| P2 | _build_final_messages 为占位函数 | 未实现真实组装逻辑 |

### v4.3-selfcheck-2 — 修复与增强（2026-05-24）

| # | 问题 | 修复 |
|---|------|------|
| 1 | turn_index 重复覆盖 | 改用原子计数器 `_turn_counter`，启动时从 `max_turn_index()` 恢复 |
| 2 | 降级写入静默 | 增加 `logger.error` |
| 3 | assemble 无输入校验 | 增加输入校验 |
| 4 | hook 返回策略 | 明确原地修改 `conversation_history` |
| 5 | 超时不匹配 | `wait_for_pending` 改为 `Config.LLM_TIMEOUT + 10` |
| 6 | 占位函数 | `_build_final_messages()` 实现完整分层消息组装 |

### v4.3.1 — 调试增强（2026-05-24）

| 变更 | 说明 |
|------|------|
| **触发** | 联调时需要观测数据流内部快照，但生产不能有额外开销 |
| **新增调试开关** | `Config.DEBUG`（`CA_DEBUG` 环境变量）；7 个关键观测点；`if Config.DEBUG:` 保护，生产零开销 |
| **线程安全补充** | A-stage 读取 `AssemblyCache` 字典时的迭代安全说明 |
| **可观测接口汇总** | 补充 `CompressStats` 条目 |
| **审查结果** | 通过自我审查，文档与代码对齐 |

### v4.3.1 修订 — 文档修订（2026-05-24）

| 变更 | 说明 |
|------|------|
| **不一致 1** | 调试观测点描述与实际代码对齐（LLM 请求详情→Prompt 前 200 字、重试等待时间） |
| **不一致 2** | 可观测接口补充 `CompressStats` |
| **不一致 3** | `CA_PROTECT_TAIL_TOKENS` 环境变量与代码统一，`assemble()` 改用 `Config.PROTECT_TAIL_TOKENS` |
| **设计保护** | A-stage 溢出保护（缓存为空 + token 超阈值→`_hard_truncation`）；线程安全读缓存（加锁或快照） |
| **结论** | 文档与代码完全对齐，**最终版** |

---

### v4.4.1 — 尾区分隔与话题检测（2026-05-25 ~ 2026-06-04）

> 部署版分化起点：从 `projects/context-assembler` 独立为 `~/.hermes/profiles/tester/plugins/ca_assembler/` 自包含副本。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **对话轮与工具轮尾区分离** | 对话需要深度上下文保护，旧工具响应对 LLM 价值递减；独立尾区策略避免工具响应挤占对话 tail 预算 | `ca/__init__.py` |
| **上下文窗口三级回退** | Hermes API → 自有查表 → 200K 兜底，覆盖 Ollama 自定义模型 | `ca/__init__.py` |
| **后台审查轮规则跳过 LLM** | `write_origin == "background_review"` 时规则生成 L1，避免浪费 LLM 调用 | `ca/__init__.py` |
| **话题边界检测** | 余弦相似度 < 0.50 → 新话题，为未来按话题归并邻接轮次提供数据基础 | `ca/__init__.py` |
| **turn_plan 表** | 首次引入，记录每次 A‑stage 拣选决策，用于调试比对 | `ca/store.py` |
| **指纹去重量构** | 改为保留最后一次出现 + CA tag 规范化，修复跨角色误杀 | `ca/__init__.py` |

### v4.4.2 — 初始部署修复（2026-06-04）

| 修复 | 说明 |
|------|------|
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，改为 profile 感知 |
| `sys.path` 保障本地 `ca/` 优先 | 插件目录加入 `sys.path.insert(0, ...)` |
| 添加 `register(ctx)` 函数 | 原插件无 `register()`，所有 hook 回调永不注册 |
| Hook 签名适配 | 所有 hook 回调改为 `**kwargs: Any` 模式 |
| 模块级引擎注册表 | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁 |

### v4.4.3 — Bug 修复（2026-06-05）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| C-stage Executor shutdown 竞态 | `_shutdown_cache_executor()` 没设 `_destroyed`。修复：`destroy()`/`reset()` 改用 `cache.destroy()` | `ca/__init__.py` |
| Head 保护区方向错误 | `_compute_layers_v2()` 取 `sorted(...)[-N:]` 取了末尾 3 轮。修复：`[-N:]` → `[:N]` | `ca/__init__.py` |

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

**ToolSummarizer 结构化摘要 10 handlers（2026-06-06）**

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

### v4.5.2 — ToolSummarizer JSON 适配（2026-06-06）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **arguments JSON string → dict 适配** | 所有 10 个 structured handler 因 `args.get()` 在 JSON string 上调用时全部崩溃回退到通用逻辑。修复：在 `summarize()` 入口加 JSON → dict 适配层 | `ca/tool_summarizer.py` |
| **旧数据全量回填** | 部署至今所有 DB 中 6,762 条工具轮 Elm 存的是 raw JSON 格式。离线重跑 summarizer，已全部升级为结构化摘要 | 共修复 174 个 DB |

### v4.6.1 — ToolSummarizer 信息密度优化（2026-06-07）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| **断路器 stale 文件清理** | `_state_file_path()` 每次 `_write_state()` 前清理不再运行的 PID 残留状态文件，防止无限堆积 | `ca/__init__.py` |
| **terminal/exec Fct 信息密度** | terminal handler 从 `cmd_short[:60] (N lines)` 改为 `t:cmd_part[:30] → key_lines[0][:50]` | `ca/tool_summarizer.py` |
| **read_file Fct 路径压缩** | 从纯路径改为 `…{parent}/{fname} (N lines)`，压缩路径前缀、添加行数 | `ca/tool_summarizer.py` |
| **read_file Fct 行数 bug** | `json.dumps` 转义 `\n` 为 `\\n` 导致 split 计数永远 1。修复：`json.loads(c)["total_lines"]` | `ca/tool_summarizer.py` |

---

### v4.6.0 — 话题拣选重构（2026-06-07）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **话题分割** | 新增 `_compute_topic_groups()`（R1 BG检测 + R2 Jaccard 链合并），仅依赖 Fct JSON 5 字段，无额外 LLM/embedding 开销 | `ca/__init__.py` |
| **三级定级** | 新增 `_grade_topics_by_radius()`，topic 半径 r = min(max_intra, nearest/WEIGHT)，内球→Hdl 外球→Fct 远距离→Elm | `ca/__init__.py` |
| **TopicRetriever** | `ca/retrieval.py` 新增独立类，per-topic BM25 + vector + RRF 融合 | `ca/retrieval.py` |
| **话题级 Plan** | 新增 `_compute_turn_plan_v2()`，话题级决策 + 工具轮绑定 (topic_boost) | `ca/__init__.py` |
| **query_embedding 列** | turn_cache schema v3→v4，新增 query_embedding BLOB 列，assemble() 时自动写入 | `ca/store.py` |
| **配置项** | 新增 5 个 TOPIC_* 环境变量（JACCARD_ENTRY/CHAIN/RADIUS_WEIGHT/MAX_UPGRADE/BG_LEVEL） | `ca/config.py` |
| **pre_upgrade 移除** | 删除 `_pre_upgrade_tools`、`_pre_upgraded_tool_turns`、`_topic_lock` 等 3 方法 + 5 字段 | `ca/__init__.py` |
| **C-stage 话题检测移除** | 话题边界由 assemble() 统一实时计算，C-stage 不再写入 topic_group | `ca/__init__.py` |
| **CA_CONTEXT_LENGTH** | 默认从 50000 提升至 100000 | `ca/config.py` |

**测试**：`tests/test_v460.py`（53 测试），覆盖话题分割、三级定级、TopicRetriever、Plan v2、query_embedding、TOPIC_* 配置

**可观测增强**：

| 新增 | 说明 | 代码位置 |
|------|------|---------|
| `store.get_max_token_offset()` | `SELECT MAX(token_offset)` 纯读，零副作用 | `ca/store.py` |
| `engine.debug_token_budget()` | 返回 `{context_length, budget_max, used_tokens, remaining, usage_pct}` | `ca/__init__.py` |

---

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

---

### v5.1 — Replace 模式注入重构 + bypass 统一 + 尾区保护（2026-06-09 ~ 2026-06-12）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **三模式注入** | Replace（替换原文）/ Append（追加）/ Off（不注入），`INJECTION_MODE` 配置 | `ca/__init__.py` |
| **行类型表** | 不同角色行使用不同注入策略 | `ca/__init__.py` |
| **bypass_turns 统一** | 指定 turn 跳过注入，`_bypass_skip=3` 保护最后 2 完整对话轮 + 当前 Q | `ca/__init__.py` |
| **20K 尾区保护** | 保护最后 ~20K tokens 的原始对话内容不被替换 | `ca/__init__.py` |
| **biz_category 双向嵌入** | DDL 加列 + C-stage 写 + A-stage 消费 + 连带脆弱点修复 | `ca/store.py`, `ca/__init__.py` |
| **state DB 污染切断** | `post_llm_call` 共享 dict 就地恢复，防止 mutation 污染 Hermes 消息 | `ca/__init__.py` |
| **tool 行 content 清空** | content→单空格，94 行 340K chars → 94 chars | `ca/__init__.py` |
| **bg_review 内容清空 + 尾区保护** | mutation 循环内按位置边界保护尾区 bg_review | `ca/__init__.py` |
| **语义摘要格式化优化** | `_format_group_summary` 精简、handler 字段提取修复 | `ca/__init__.py`, `tool_summarizer.py` |
| **调试文档沉淀** | debug-20260609g 等分析报告归档 | `docs/debug/` |

---

### v5.2 — 缓存分析 + 注入重构（2026-06-13）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **hdl_embedding 孤儿数据清除** | 从未被消费，注释全部计算链路 + 删除死代码 `retrieve_l0_upgrade` | `ca/__init__.py`, `ca/retrieval.py` |
| **tool_plan 独立 tool 行决策** | `_AssemblePlanResult.tool_plan` + `_compute_tool_plan_v2` + `_build_aligned_outcomes`/`_build_messages_from_plan` 签名扩展 + `_format_tool_group_assembly` 精简为仅 header + tool 行输出 `[~/N/M]` 独立标签 | `ca/__init__.py` |
| **bg_review 轮从 DB 读数** | `_mutation_mode` 中 bg_review 轮由 `" "` 改为从 DB 读取 L1/L0 填充 | `ca/__init__.py` |

详见 [v5.1 分析报告](docs/analysis/ca-v5.1-cache-analysis-and-injection-refactor.md)。

---

### v5.2.1 — 话题分割修复 + 自适应阈值（2026-06-14）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`_add_bigrams` 集合无心化修复** | `sorted(s)` 保证集合迭代顺序稳定 | `ca/__init__.py` |
| **Jaccard 独立合并路径** | `_compute_topic_groups` 新增，默认 0.18，不依赖 todo_overlap | `ca/__init__.py` |
| **自适应阈值模块** | `_load_start_threshold` / `_compute_ideal_threshold` / `persist_ideal_threshold`，持久化至 `{ca_cache}/topic_threshold_meta.json` | `ca/__init__.py` |
| **会话内阈值固定** | 同一会话内阈值不变 | `ca/__init__.py` |
| **跨会话加权漂移** | `0.6×last + 0.4×avg` 权重 | `ca/__init__.py` |
| **session 生命周期** | `session_reset` 时持久化 ideal，`session_start` 时加载起始阈值 | `ca/__init__.py` |

---

### v5.3.0 — Fct 提示词多对标签重构（2026-06-17）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`FCT_GENERATION_PROMPT` 多对输出** | 从「单个 `<stage_tag>` 合并多状态（`【已实施/计划】`）」改为「每对 `<stage_tag>`/`<core_change>` 仅含单一状态，多个事项=多对标签」 | `ca/prompts.py` |
| **`PAIR_PATTERN` 多对解析** | 正则适配多对 XML 标签解析 | `ca/post_process.py` |
| **`changes` 列表存储** | 全链路适配 `clean_increment`/`_extract_l0`/`_json_to_v1_markdown` | `ca/` 多个文件 |
| **测试** | 242 测试全部通过 | `tests/` |

---

### v5.5.0 — 命名统一 + 话题分割 + A-stage 角色队列匹配重构（2026-06-15 ~ 2026-06-18）

**术语命名统一**（commit `aff6aaf`）：
- `L2`/`L1`/`L0` → `Elm`/`Fct`/`Hdl`
- `C-stage` → `F-stage`
- 全代码库重命名 + 279 DB ALTER TABLE 迁移
- 旧 buffer/conv_encoding/assemble 路径死代码清理

**F-stage 重构（Phase 1-4）**：
| Phase | 变更 | 说明 |
|-------|------|------|
| 1 | 从 DB 读 Elm | 不再依赖函数参数，`f_stage.py` 直接读取 `turn_stream` 的 Elm 列 |
| 2 | stage_tag 独立 | `FCT_GENERATION_PROMPT` 中 `stage_tag` 与 `core_change` 解耦 |
| 3 | thought 截断修正 | fin 行 `stop_reason=length` 时检测截断并降级 |
| 4 | 多对标签重构 | 同 v5.3.0，每 `stage_tag`/`core_change` 对仅含单一状态 |

**E-stage write-on-receive + turn_stream 新表**（commit `5432463`）：
| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **turn_stream 表** | 主键 `(session_id, turn, seq)`，每行 = 一条消息切片 | `ca/store.py` |
| **5 Hook 各写其责** | pre_llm_call 写 user(seq=0)、post_api_request 写 thought+tool 占位、post_tool_call 回填 tool、post_llm_call 写 fin 行 | `plugins/ca_assembler/__init__.py` |
| **Fct/Hdl 列** | F-stage 异步回写，tool summarizer 同步写 per-tool Fct | `ca/store.py` |
| **WAL 模式** | `PRAGMA journal_mode=WAL` | `ca/store.py` |

**A-stage 角色队列匹配**（commit `a79bde4`）：
- 替代旧逐行 seq 对齐，基于 row role 队列匹配
- `get_turn_ca_rows()` 替代 `read_role_v5()`（删除）
- 增量缓存 `_A_stable_cache` 配合角色队列

**话题分割（v5.5 核心）**：
| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`TopicGradeManager`** | 新增独立话题管理类，含 `_assign_topic()` / `detect()` | `ca/topic_manager.py` |
| **Jaccard + 强制短语 + 水位压力** | CJK 重叠、预定义分隔短语、Token 驱动 Jaccard 柔性扣减 | `ca/topic_manager.py` |
| **grade_on_switch** | 话题切换时打包旧话题 OV 提交 | `ca/topic_manager.py` |
| **`CA_CONTEXT_LENGTH`** | 默认 50000 | `ca/config.py` |
| **LLM think 默认关闭** | `think=False` + `_PLACEHOLDERS` 过滤占位符 | `ca/` |
| **clean_increment 占位符过滤** | bypass 不参与话题检测 | `ca/ooda_parser.py` |
| **OpenViking 话题自动提交** | `4bd0190` — CA-OV topic submit | `ca/` |

**Bug 修复**：
- `_call_llm_for_fct` 错标 `@staticmethod` → F-stage 全部降级
- F-stage fallback 复制 user Elm 替代硬编码占位符
- `fct_text` 残留字段清理
- `generate_group_summary` 截断无句尾标点长文本

---

### v5.10 — CE route shell 基线（2026-06-22）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`CAContextEngine` 单例** | 实现 `ContextEngine` ABC，`ctx.register_context_engine(\"ca_assembler\", _ce_engine)` | `plugins/ca_assembler/__init__.py` |
| **should_compress → True** | 触发 Hermes compress_context 管线 | `plugins/ca_assembler/__init__.py` |
| **compress()** | 从 turn_stream DB 构建 conv_history → 原地拷贝回 messages | `plugins/ca_assembler/__init__.py` |
| **CE 协议测试** | 8 项协议测试（cache impact verified） | `tests/unit/test_ce_shell.py` |
| **基线快照** | 后继 v6 方向 B 改用 `_build_conv_history_v6` 替代 CE compress | `ca/` |

此版本为 v6.0 方向 A（CE route shell）的实现，后继被方向 B（纯 DB 重建，不经过 CE 管线）替代。

---

### v6.0.0 — CE 路线 + 方向 B conv_history 重建（2026-06-22 ~ 2026-06-27）

**方向 A（CE route shell）**：`CAContextEngine` 注册到 Hermes CE 管线，`compress()` 从 turn_stream DB 构建 conv_history 后原地拷贝回 Hermes 消息列表（2026-06-22）。

**方向 B（_build_conv_history_v6，2026-06-27 ~ 2026-06-28）**：从 CA 自有 turn_stream DB 重建 conv_history，**完全不接触 Hermes 消息列表**。

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`_build_conv_history_v6()`** | 纯 DB 读取函数，从 turn_stream 读取全量 → 三区模型逐 turn 构造 OpenAI 格式消息列表 → 替换 Hermes conv_history | `ca/a_stage.py` |
| **三区模型 + 行类型降级** | 远区(FAR)→Hdl、中区(REL)→Fct、近区(ACT)→Elm；thought/tool 行比 user/fin 降一级；FAR thought/tool 删除；尾区全 Elm | `ca/a_stage.py` |
| **CE 管线全面停用** | `should_compress()` → False, `compress()` → no-op（v6.0.5） | `plugins/ca_assembler/__init__.py` |
| **QA 修复** | `from_topic_grade()` FAR→None 修正、`_incremental_mutation` Step 3 thought/fin 改用 next-msg 替代 tool_calls、冗余 tool_calls 后向查找清理 | `ca/a_stage.py`, `ca/grade.py` |
| **移除方向 A 死代码** | `pre_llm_call_v5()` 类方法（hook 注册的是独立函数 `_on_pre_llm_call_v5`） | `plugins/ca_assembler/__init__.py` |
| **fix: bg 跳过导致 post hooks 写入 stale turn** | bg_review 轮跳过时正确维护 `_turn_counter` | `plugins/ca_assembler/__init__.py` |
| **测试** | 18 项 `_build_conv_history_v6` 单元测试 | `tests/unit/test_a_stage.py` |
| **wiki 文档同步** | AGENTS.md + decisions/31-ce-shell-registration.md + architecture/04-a-stage-role-match.md 更新为 CE 已停用 | `docs/wiki/` |

**架构对比**：

| 维度 | 方向 A（CE route shell，已废） | 方向 B（当前，v6.0） |
|------|-------------------------------|---------------------|
| 数据源 | turn_stream DB → CE compress → 原地拷贝 messages | turn_stream DB → `_build_conv_history_v6` → 新列表 |
| 写 state.db | CE compress 的 append() 产生新 dict → identity 不匹配 → 双写风险 | 不接触 Hermes 消息列表，零 state.db 写入 |
| 恢复成本 | 需要 `_full_backup` / `_saved_history_snapshot` 来回退 | 无备份需要，每次重建重新计算 |
| 增量缓存 | `_A_stable_cache` + Fct pending 防护增加复杂度 | 无需增量缓存，无 Fct pending 防护 |
| CE 管线依赖 | 依赖 should_compress → compress_context → archive_and_compact | 完全独立，CE 管线仅返回 False/no-op |

---

### v6.0.1 — Fct=NULL 告警 + 冗余赋值清理（2026-06-28）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **Fct=NULL 告警** | A-stage 新增 4+2 处 `logger.warning`：当 grade=FCT 但摘要为空时写告警，消除静默 Elm 回退 | `ca/a_stage.py` |
| **冗余赋值清理** | 移除 `test_a_stage.py`/`test_a_stage_topic_aware.py` 中 `plugin._A_stable_cache = None`（v6.0 方向 B 重构后从未被读取） | `tests/` |
| **测试 xfail→pass** | `test_rel_fct_null_falls_back_to_elm_with_warning` 移除 xfail 标记 | `tests/` |

---

### v6.0.2 — 首轮 Fct 修复 + 截断 fallback 改进（2026-06-28）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **首轮 Fct band-aid 移除** | 删除 f_stage.py 首轮跳过 LLM 分支；首轮 LLM 收到完整空历史摘要，正常走生成路径 | `ca/f_stage.py` |
| **截断 fallback 改进** | ① PAIR_PATTERN.findall 提取已完成 XML 对 ② parse_v1_markdown_xml 提取叙事段 ③ partial[:500] 兜底 | `ca/post_process.py` |
| **Tool Fct 代码体剥离** | `_clean_tool_args` 新增 `terminal`→`command` + 通用 `code` 兜底（覆盖 MCP 变体），消除 4.5K-8.5K tool Fct 膨胀 | `ca/tool_summarizer.py` |
| **L1_MAX_TOKENS 800→2048** | 减少 F-stage LLM 截断频率 | `ca/config.py` |

---

### v6.0.3 — Tool Fct 空字段过滤（2026-06-28）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **Tool Fct 空字段过滤** | `_select_content` 对 tool Fct 做空字段(null/[]/""/0)剔除 — 单位 Token 互信息最大化。ACT 轮 32 工具行节省 ~650 tok/会话 | `ca/a_stage.py` |
| **result_summary 去命令** | terminal `_summarize_terminal` 有 key_lines 时不附带 `[cmd_short]` — 命令已隐含在 thought 中 | `ca/tool_summarizer.py` |

---

### v6.0.4 — HEAVY_FIELDS 审计修复 + memory handler 新增（2026-06-28）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`skill_manage` HEAVY_FIELDS 补全** | 补 `old_string/new_string/content` — 修复 2962B tool Fct outlier | `ca/tool_summarizer.py` |
| **`memory` HEAVY_FIELDS 新增** | 新增 `memory` 条目 + handler 接线，清除 `content/old_text/old_string` | `ca/tool_summarizer.py` |
| **5 handler 漏接 `_clean_tool_args`** | `search_files/skills_list/skill_view/todo` 补调用 — 0 处裸 `tool_args: args` 残留 | `ca/tool_summarizer.py` |
| **terminal `command[:100]` 保留** | 替代全删，命令是身份标识 | `ca/tool_summarizer.py` |

---

### v6.0.5 — CE 管线全面停用 + 双写漏洞消除（2026-06-28）

| 变更 | 说明 | 代码位置 |
|------|------|---------|
| **`should_compress()` → False** | 不触发 Hermes compress_context | `plugins/ca_assembler/__init__.py` |
| **`compress()` → no-op** | 返回 messages 不变 | `plugins/ca_assembler/__init__.py` |
| **删除方向 A 死代码** | 移除 `pre_llm_call_v5()` 类方法 | `plugins/ca_assembler/__init__.py` |
| **双写漏洞消除** | 旧 should_compress→True + _last_compress_aborted flag 导致 compress_context 先调 compress() 做原地 mutation 再 abort→history_ids=set()→state.db 重复行。CE 停用后根除 | `plugins/ca_assembler/__init__.py` |
| **wiki 文档同步** | AGENTS.md + 31-ce-shell-registration.md + 04-a-stage-role-match.md CE 已停用 | `docs/wiki/` |

### 关键决策记录

每项关键决策的完整分析（问题→备选方案→理由→得失）在 wiki 决策树中归档。

→ [决策索引](docs/wiki/decisions/)（32 份决策文档，覆盖 v0.x ~ v6.1）

---

