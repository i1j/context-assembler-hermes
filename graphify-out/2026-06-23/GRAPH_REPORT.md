# Graph Report - .  (2026-06-23)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 573 nodes · 766 edges · 60 communities (53 shown, 7 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 26 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1f5df04d`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Topic Manager Unit Tests|Topic Manager Unit Tests]]
- [[_COMMUNITY_F-stage Async Summary|F-stage Async Summary]]
- [[_COMMUNITY_Three-Tier Grading Plugin|Three-Tier Grading Plugin]]
- [[_COMMUNITY_Architecture Decisions Index|Architecture Decisions Index]]
- [[_COMMUNITY_Topic Segmentation|Topic Segmentation]]
- [[_COMMUNITY_Tokenization Tests|Tokenization Tests]]
- [[_COMMUNITY_CA Tester Profile|CA Tester Profile]]
- [[_COMMUNITY_Tool Summarizer|Tool Summarizer]]
- [[_COMMUNITY_Test Strategy & Environment|Test Strategy & Environment]]
- [[_COMMUNITY_Test Configuration & Markers|Test Configuration & Markers]]
- [[_COMMUNITY_Wiki & Documentation Index|Wiki & Documentation Index]]
- [[_COMMUNITY_A-stage Role Match|A-stage Role Match]]
- [[_COMMUNITY_Fingerprint Dedup|Fingerprint Dedup]]
- [[_COMMUNITY_Topic Grade Switch|Topic Grade Switch]]
- [[_COMMUNITY_Backfill Tool Summaries|Backfill Tool Summaries]]
- [[_COMMUNITY_Incidents Review|Incidents Review]]
- [[_COMMUNITY_Rejected Approaches|Rejected Approaches]]
- [[_COMMUNITY_Rejected Approaches|Rejected Approaches]]
- [[_COMMUNITY_CE Shell Registration|CE Shell Registration]]
- [[_COMMUNITY_E-stage Write Protocol|E-stage Write Protocol]]
- [[_COMMUNITY_CA-OV Topic Submit|CA-OV Topic Submit]]
- [[_COMMUNITY_CA Test System Overview|CA Test System Overview]]
- [[_COMMUNITY_Storage Model|Storage Model]]
- [[_COMMUNITY_ContextEngine Shell|ContextEngine Shell]]
- [[_COMMUNITY_Tail Protection|Tail Protection]]
- [[_COMMUNITY_Test Fixtures Embed & LLM|Test Fixtures Embed & LLM]]
- [[_COMMUNITY_Incremental Cache|Incremental Cache]]
- [[_COMMUNITY_Naming Convention|Naming Convention]]
- [[_COMMUNITY_Test Strategy|Test Strategy]]
- [[_COMMUNITY_Dual Retrieval & RRF|Dual Retrieval & RRF]]
- [[_COMMUNITY_Topic Grade Mapping|Topic Grade Mapping]]
- [[_COMMUNITY_Engine & Hardware Fixtures|Engine & Hardware Fixtures]]
- [[_COMMUNITY_Design Philosophy|Design Philosophy]]
- [[_COMMUNITY_SQLite WAL Storage|SQLite WAL Storage]]
- [[_COMMUNITY_Architecture & Decisions Wiki|Architecture & Decisions Wiki]]
- [[_COMMUNITY_Tool Summarizer Rules|Tool Summarizer Rules]]
- [[_COMMUNITY_Dual Retrieval RRF|Dual Retrieval RRF]]
- [[_COMMUNITY_E-stage Write on Receive|E-stage Write on Receive]]
- [[_COMMUNITY_F-stage Async Summary|F-stage Async Summary]]
- [[_COMMUNITY_FCT Format Changes|FCT Format Changes]]
- [[_COMMUNITY_Topic Grade Manager|Topic Grade Manager]]
- [[_COMMUNITY_Incremental Cache|Incremental Cache]]
- [[_COMMUNITY_Tool Group Format|Tool Group Format]]
- [[_COMMUNITY_Embedding Multi-Backend|Embedding Multi-Backend]]
- [[_COMMUNITY_Assembly Cache Singleton|Assembly Cache Singleton]]
- [[_COMMUNITY_Three-Zone Model|Three-Zone Model]]
- [[_COMMUNITY_LLM Fallback Chain|LLM Fallback Chain]]
- [[_COMMUNITY_Config System|Config System]]
- [[_COMMUNITY_Topic Picking v46|Topic Picking v46]]
- [[_COMMUNITY_L1 Summary PDD|L1 Summary PDD]]
- [[_COMMUNITY_Schema v5 Rewrite|Schema v5 Rewrite]]
- [[_COMMUNITY_Stage Terminology Unification|Stage Terminology Unification]]
- [[_COMMUNITY_Tail Protection|Tail Protection]]
- [[_COMMUNITY_Background Review Sync|Background Review Sync]]
- [[_COMMUNITY_CA-OV Topic Submit|CA-OV Topic Submit]]
- [[_COMMUNITY_CA Document Index|CA Document Index]]
- [[_COMMUNITY_Budget Gate|Budget Gate]]
- [[_COMMUNITY_C-stage Async|C-stage Async]]
- [[_COMMUNITY_Plugin Responsibility|Plugin Responsibility]]
- [[_COMMUNITY_Injection Refactor|Injection Refactor]]

## God Nodes (most connected - your core abstractions)
1. `TopicGradeManager` - 44 edges
2. `tests/conftest.py` - 30 edges
3. `FStageMixin` - 25 edges
4. `CA (ContextAssembler) — Tester Profile 部署` - 20 edges
5. `EStageMixin` - 20 edges
6. `ToolSummarizer` - 17 edges
7. `tests/unit/test_topic_manager.py` - 16 edges
8. `CAContextAssemblerPlugin` - 15 edges
9. `tests/unit/test_tool_summarizer.py` - 14 edges
10. `ContextAssembler` - 13 edges

## Surprising Connections (you probably didn't know these)
- `Architecture 04: A-stage Role Queue Match` --references--> `AStageMixin`  [EXTRACTED]
  docs/wiki/architecture/04-a-stage-role-match.md → ca/a_stage.py
- `Architecture 06: Tail Protection` --references--> `AStageMixin`  [EXTRACTED]
  docs/wiki/architecture/06-tail-protection.md → ca/a_stage.py
- `Architecture 05: Incremental Cache` --references--> `AssemblyCache`  [EXTRACTED]
  docs/wiki/architecture/05-incremental-cache.md → ca/cache.py
- `Architecture 05: Incremental Cache` --references--> `CacheBuilder`  [EXTRACTED]
  docs/wiki/architecture/05-incremental-cache.md → ca/cache.py
- `Architecture 06: Tail Protection` --references--> `Config`  [EXTRACTED]
  docs/wiki/architecture/06-tail-protection.md → ca/config.py

## Import Cycles
- None detected.

## Communities (60 total, 7 thin omitted)

### Community 0 - "Topic Manager Unit Tests"
Cohesion: 0.10
Nodes (38): _grade_topics_by_radius, Topic Centroid via Embedding, FCT Priority Extraction (user→fin→any), Forced Split Phrases for Topic Segmentation, Radius-Based Topic Grading, Water Pressure Token Budget (CA_TOPIC_PEAK_TOKEN), _compute_centroid, TestApplyWaterPressure (+30 more)

### Community 1 - "F-stage Async Summary"
Cohesion: 0.06
Nodes (43): 优点, 决策, 备选方案, 实现要点, 数据验证, 生成内容, 约束 / 已知问题, 选定方案 (+35 more)

### Community 2 - "Three-Tier Grading Plugin"
Cohesion: 0.20
Nodes (14): CAContextAssemblerPlugin, register, Elm-Fct-Hdl Three-Tier Grading, TopicGradeManager, ASSEMBLE_OK, ASSEMBLE_PENDING_BACKFILL, ASSEMBLE_PERMANENT_FAILURE, Config (+6 more)

### Community 3 - "Architecture Decisions Index"
Cohesion: 0.33
Nodes (15): Architecture 01: Storage Model, Architecture 02: E-stage Write Protocol, Architecture 03: F-stage Async Summary, Architecture 04: A-stage Role Queue Match, Architecture 05: Incremental Cache, Architecture 06: Tail Protection, Architecture 07: bg_review Sync Write, Architecture 08: Topic Segmentation (+7 more)

### Community 4 - "Topic Segmentation"
Cohesion: 0.09
Nodes (27): 优点, 决策, 备选方案, 数据验证, 约束 / 已知问题, 选定方案, 问题, A-stage Role Queue Matching (+19 more)

### Community 5 - "Tokenization Tests"
Cohesion: 0.67
Nodes (4): tokenise, TestIsCjkChar, TestTokenise, tests/unit/test_cache.py

### Community 6 - "CA Tester Profile"
Cohesion: 0.04
Nodes (48): CA (ContextAssembler) — Tester Profile 部署, compress() 行为（CE 路径）, ContextEngine 壳（v6.0）, DB 路径, Detect：增量话题分割, Fct pending 防护（P0）, Fct 摘要生成架构, Get Turn Grade：等级查询 (+40 more)

### Community 7 - "Tool Summarizer"
Cohesion: 0.19
Nodes (17): Rule-Engine Tool Summarizer (No LLM), DEFAULT_PRIORITY, ToolSummarizer, TestGenerateGroupSummary, TestInit, TestMemoryHandler, TestPatchHandler, TestReadFileHandler (+9 more)

### Community 8 - "Test Strategy & Environment"
Cohesion: 0.40
Nodes (4): DOA Self-Defense Test, Hermetic Test Environment, 3-Stage Layered Test Strategy, Tests INDEX

### Community 9 - "Test Configuration & Markers"
Cohesion: 0.15
Nodes (12): sys.path.insert (ca import path), pytest marker: critical, pytest marker: elm, pytest marker: fct, pytest marker: high, pytest marker: linux_only, pytest marker: llm_return, pytest marker: low (+4 more)

### Community 10 - "Wiki & Documentation Index"
Cohesion: 0.18
Nodes (9): CA Wiki README, architecture/ — 空间维度：当前系统组件, CA 技术方案, decisions/ — 时间维度：决策树, 与 Git 的关联, 双维度文档, 文件结构, 新旧对照 (+1 more)

### Community 11 - "A-stage Role Match"
Cohesion: 0.22
Nodes (8): 优点, 决策, 备选方案, 实现要点, 数据验证, 约束 / 已知问题, 选定方案, 问题

### Community 13 - "Topic Grade Switch"
Cohesion: 0.25
Nodes (7): 优点, 决策, 备选方案, 数据验证, 约束 / 已知问题, 选定方案, 问题

### Community 14 - "Backfill Tool Summaries"
Cohesion: 0.50
Nodes (4): backfill_db(), is_raw_json_l0(), SQLiteStore, turn_stream

### Community 25 - "Rejected Approaches"
Cohesion: 0.22
Nodes (6): 决策, 已拒绝方案列表, 拒绝原则, 问题, 触发条件, 选定

### Community 26 - "CE Shell Registration"
Cohesion: 0.18
Nodes (10): CE-004: ContextEngine 壳注册, 三个约束条件, 代码位置, 关键设计, 决策, 守卫机制, 对缓存的影响, 执行顺序 (+2 more)

### Community 29 - "E-stage Write Protocol"
Cohesion: 0.07
Nodes (31): 优点, 决策, 备选方案, 实现要点, 数据验证, 约束 / 已知问题, 选定方案, 问题 (+23 more)

### Community 30 - "CA-OV Topic Submit"
Cohesion: 0.20
Nodes (9): 优点, 决策, 变更记录, 变更记录, 备选方案, 数据验证, 约束 / 已知问题, 选定方案 (+1 more)

### Community 34 - "CA Test System Overview"
Cohesion: 0.20
Nodes (9): 1. 测试哲学, 2. 跑法, 3. 分层, 4. 决策点 ↔ 测试文件 对照矩阵, 5. 测试缺口登记, 6. 覆盖统计, 7. 相关文档, CA 测试体系总览 (+1 more)

### Community 35 - "Storage Model"
Cohesion: 0.20
Nodes (9): 优点, 决策, 备选方案, 实现要点, 数据验证, 约束 / 已知问题, 选定方案, 问题 (+1 more)

### Community 37 - "ContextEngine Shell"
Cohesion: 0.24
Nodes (10): CAContextEngine, is_available, Circuit Breaker Pattern, Copy-on-Write Cache with BM25, Write-on-Flush Persistence, TestCachePreservation, TestCEMutationRestore, TestCEProtocol (+2 more)

### Community 38 - "Tail Protection"
Cohesion: 0.09
Nodes (25): 优点, 决策, 备选方案, 实现要点, 数据验证, 约束 / 已知问题, 选定方案, 问题 (+17 more)

### Community 41 - "Test Fixtures Embed & LLM"
Cohesion: 0.22
Nodes (8): _mock_embed (fixture, autouse), _mock_llm (fixture, autouse), v5_store (fixture), v5_turn_stream (fixture), _LLM_MOCK_RESPONSE, tests/fixtures/__init__.py, tests/fixtures/fixtures_mock.py, tests/fixtures/fixtures_store.py

### Community 56 - "Incremental Cache"
Cohesion: 0.17
Nodes (12): 优点, 决策, 备选方案, 数据验证, 约束 / 已知问题, 选定方案, 问题, AssemblyCache (+4 more)

### Community 61 - "Naming Convention"
Cohesion: 0.25
Nodes (7): 优点, 决策, 备选方案, 数据验证, 约束 / 已知问题, 选定方案, 问题

### Community 62 - "Test Strategy"
Cohesion: 0.25
Nodes (7): 优点, 决策, 备选方案, 数据验证, 约束 / 已知问题, 选定方案, 问题

### Community 63 - "Dual Retrieval & RRF"
Cohesion: 0.24
Nodes (11): Dynamic BM25/Vector Candidate Allocation, RRF Fusion Retrieval, BM25Okapi, Retriever, _rrf_fuse, TopicRetriever, TestBM25Okapi, TestCosineSimilarity (+3 more)

### Community 64 - "Topic Grade Mapping"
Cohesion: 0.33
Nodes (7): TopicGrade→Grade Mapping (ACT→FCT, REL→HDL, FAR→ELM), ACT-REL-FAR Topic Grading, TopicGrade, TestFromTopicGrade, TestGradeConstantsInvariant, TestTopicGrade, tests/unit/test_grade.py

### Community 65 - "Engine & Hardware Fixtures"
Cohesion: 0.29
Nodes (5): ca_engine (fixture), engine (fixture), fd_checker (fixture), hardware_info (fixture), tests/fixtures/fixtures_engine.py

### Community 66 - "Design Philosophy"
Cohesion: 0.33
Nodes (5): 之前 vs 之后, 备选方案, 影响, 触发条件, 选定

### Community 67 - "SQLite WAL Storage"
Cohesion: 0.33
Nodes (5): 之前 vs 之后, 备选方案, 影响, 触发条件, 选定

### Community 81 - "Architecture & Decisions Wiki"
Cohesion: 0.33
Nodes (6): architecture（14 页 — 空间维度：当前系统组件）, CA 技术方案 Wiki, decisions（31 页 — 时间维度：决策树）, 关系图, 历史文档, 索引

### Community 82 - "Tool Summarizer Rules"
Cohesion: 0.40
Nodes (4): 备选方案, 影响, 触发条件, 选定

### Community 83 - "Dual Retrieval RRF"
Cohesion: 0.40
Nodes (4): 备选方案, 影响, 触发条件, 选定

### Community 84 - "E-stage Write on Receive"
Cohesion: 0.40
Nodes (4): 之前 vs 之后, 备选方案, 触发条件, 选定

### Community 85 - "F-stage Async Summary"
Cohesion: 0.40
Nodes (4): 之前 vs 之后, 备选方案, 触发条件, 选定

### Community 86 - "FCT Format Changes"
Cohesion: 0.40
Nodes (4): 之前 vs 之后, 备选方案, 触发条件, 选定

### Community 87 - "Topic Grade Manager"
Cohesion: 0.40
Nodes (4): 之前 vs 之后, 备选方案, 触发条件, 选定

### Community 88 - "Incremental Cache"
Cohesion: 0.40
Nodes (4): 之前 vs 之后, 备选方案, 触发条件, 选定

### Community 89 - "Tool Group Format"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 90 - "Embedding Multi-Backend"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 91 - "Assembly Cache Singleton"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 92 - "Three-Zone Model"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 93 - "LLM Fallback Chain"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 94 - "Config System"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 95 - "Topic Picking v46"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定

### Community 96 - "L1 Summary PDD"
Cohesion: 0.50
Nodes (3): 影响, 触发条件, 选定（PDD 驱动）

### Community 97 - "Schema v5 Rewrite"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 98 - "Stage Terminology Unification"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 99 - "Tail Protection"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 100 - "Background Review Sync"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 101 - "CA-OV Topic Submit"
Cohesion: 0.50
Nodes (3): 备选方案, 触发条件, 选定

### Community 141 - "CA Document Index"
Cohesion: 0.50
Nodes (3): CA (Context Assembler) 项目文档索引, 测试 & 调试, 设计文档

## Knowledge Gaps
- **308 isolated node(s):** `部署快照`, `注册接口`, ``_on_session_start` — 引擎初始化`, ``_on_session_end` — 资源清理`, ``_on_session_reset` — 重置` (+303 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TopicGradeManager` connect `Topic Manager Unit Tests` to `Topic Grade Mapping`, `Three-Tier Grading Plugin`, `Architecture Decisions Index`, `Topic Segmentation`, `Tail Protection`, `A-stage Role Match`, `Topic Grade Switch`, `Backfill Tool Summaries`, `CA-OV Topic Submit`?**
  _High betweenness centrality (0.198) - this node is a cross-community bridge._
- **Why does `CA (ContextAssembler) — Tester Profile 部署` connect `CA Tester Profile` to `Wiki & Documentation Index`?**
  _High betweenness centrality (0.140) - this node is a cross-community bridge._
- **Why does `tests/conftest.py` connect `Test Configuration & Markers` to `Topic Grade Mapping`, `Engine & Hardware Fixtures`, `F-stage Async Summary`, `Topic Manager Unit Tests`, `Tokenization Tests`, `Tail Protection`, `ContextEngine Shell`, `Tool Summarizer`, `Test Fixtures Embed & LLM`, `E-stage Write Protocol`, `Dual Retrieval & RRF`?**
  _High betweenness centrality (0.125) - this node is a cross-community bridge._
- **What connects `部署快照`, `注册接口`, ``_on_session_start` — 引擎初始化` to the rest of the system?**
  _308 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Topic Manager Unit Tests` be split into smaller, more focused modules?**
  _Cohesion score 0.0953058321479374 - nodes in this community are weakly interconnected._
- **Should `F-stage Async Summary` be split into smaller, more focused modules?**
  _Cohesion score 0.057004830917874394 - nodes in this community are weakly interconnected._
- **Should `Topic Segmentation` be split into smaller, more focused modules?**
  _Cohesion score 0.09259259259259259 - nodes in this community are weakly interconnected._