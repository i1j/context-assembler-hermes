# CA Assembler 知识图谱报告

**图谱概况**: 2210 节点 · 2930 边 · 303 社区

## 🏛️ God Nodes（高连接度节点）

| Node | Degree | Type | Kind |
|------|--------|------|------|
| ContextAssembler | 100 | unknown | — |
| SQLiteStore | 62 | unknown | — |
| ToolSummarizer | 53 | unknown | — |
| v1.0 | 51 | directory | directory |
| __init__.py | 38 | unknown | — |
| CacheBuilder | 37 | unknown | — |
| test_parse_v1.py | 37 | unknown | — |
| .get() | 37 | unknown | — |
| test_store.py | 35 | unknown | — |
| Config | 34 | unknown | — |
| EmbeddingClient | 33 | unknown | — |
| test_c.py | 33 | unknown | — |
| __init__.py | 32 | unknown | — |
| json | 32 | unknown | — |
| TopicRetriever | 29 | unknown | — |
| CAContextAssemblerPlugin | 28 | unknown | — |
| MockAssistantMessage | 28 | unknown | — |
| test_smoke.py | 28 | unknown | — |
| MockToolCall | 26 | unknown | — |
| test_v440.py | 26 | unknown | — |
| test_a.py | 26 | unknown | — |
| AssemblyCache | 26 | unknown | — |
| pytest | 24 | unknown | — |
| TurnPlanEntry | 24 | unknown | — |
| time | 23 | unknown | — |

## 🔗 边关系分布

| 类型 | 边数 |
|------|------|
| calls | 672 |
| contains | 528 |
| rationale_for | 452 |
| method | 393 |
| uses | 193 |
| imports | 173 |
| belongs_to | 120 |
| imports_from | 114 |
| extends | 18 |
| has_field | 17 |
| reads_from_env_var | 10 |
| registers_hook | 8 |
| reads_from | 8 |
| writes_to | 6 |
| invokes_hook | 6 |

## 🏷️ 边类型分布（含动态边）

- **UNKNOWN**: 2482
- **EXTRACTED**: 261
- **DYNAMIC**: 187

## 🏘️ 社区分布（Top 10）

- **社区 5** (159 节点) — 主要类型: unknown
- **社区 159** (155 节点) — 主要类型: unknown
- **社区 8** (128 节点) — 主要类型: unknown
- **社区 169** (111 节点) — 主要类型: unknown
- **社区 163** (107 节点) — 主要类型: unknown
- **社区 284** (97 节点) — 主要类型: concept
- **社区 161** (88 节点) — 主要类型: unknown
- **社区 162** (88 节点) — 主要类型: unknown
- **社区 164** (87 节点) — 主要类型: unknown
- **社区 3** (83 节点) — 主要类型: unknown

## 🔄 动态边（DYNAMIC）

共 **187** 条动态边，捕获运行时调用关系、事件驱动连接、异步数据流。

### 主要动态流

| 数据流 | 边数 |
|--------|------|
| register() → 8 Hermes hooks | 8 |
| _engines session map (get/remove/reset) | 5 |
| _tool_buffer 3-stage (create/fill/flush) | 15 |
| _saved_history_snapshot save/restore | 3 |
| flush_tool_buffer → store → cache | 8 |
| _compute_assemble_plan → topic/retrieval/grading | 8 |
| _mutation_mode / _annotation_mode 分叉 | 4 |
| process_turn_async → _run_c_stage → LLM | 9 |
| Circuit breaker (read/fail/success/cooldown) | 6 |
| Cache rebuild: add_turn → BM25 snapshot | 7 |
| L-stage BackfillThread → LLM → parse → store | 10 |
| SessionManager LRU+TTL eviction | 8 |
| bg_review A-stage 门控 | 3 |
| State DB pollution cycle (fix: snapshot restore) | 5 |
| Bypass decision flow (compute → apply) | 6 |
| Budget computation chain | 4 |
| Topic-based pickling pipeline | 8 |

## 🎯 建议查询问题

1. **register() hook 注册链路**: register() 如何连接到 8 个 hook 回调？运行时数据如何流动？
2. **bypass_turns 数据流**: bypass_turns 如何从 _compute_assemble_plan 传递到 _build_aligned_outcomes？
3. **C-stage 异步管线**: process_turn_async → _run_c_stage → L1 生成的完整数据流
4. **工具组采集管线**: post_api_request → _tool_buffer → flush_tool_buffer → store → cache
5. **会话生命周期**: session_manager.get/remove → ContextAssembler.destroy → 资源释放
6. **断路器模式**: _record_failure → is_available() → 1h冷却机制
7. **mutation 模式注入**: pre_llm_call → _build_aligned_outcomes → 1:1 content 替换 → post_llm_call snapshot 恢复
8. **Cache 异步重建**: add_turn/add_tool_group → _submit_rebuild → BM25 snapshot 原子替换
9. **State DB pollution 修复**: shallow copy → mutation → snapshot restore 的完整链条
10. **bg_review 门控机制**: 运行时 get_current_write_origin() 检查 → A-stage 跳过 → C-stage 写入
