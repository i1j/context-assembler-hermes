# CA 决策树 — 节点索引

> 每个节点 = 一个独立决策。子节点 = 父决策的细化/实现/约束。
> 总 145 个决策节点 + 11 个已知差距项。
> 源文件: `design/decision-points-wiki.md` → 拆分为独立 wiki 页面。
> 
> **双维度 Wiki 入口（互补）：** `../wiki/INDEX.md` — 按组件(14页)+时间(31页)双维度组织，含关系图
>
> **快速导航:** [[C-INDEX]] (C-stage 31) · [[H-INDEX]] (历史 12) · [[GAP-INDEX]] (差距 11, 含已关闭项)

---

## 文档状态约定

| 标记 | 含义 |
|------|------|
| 无标记 | 对应当前 v5.10 代码 |
| ⚠️ 历史存档 / ⚠️ 历史 | 引用 v4.4.0 Baseline A 或 v4.7.0 过渡方案的过时决策记录，保留供溯源 |

## 根节点

- [[R-000]] — 设计哲学
- [[R-001]] — 三阶段架构 (C/A/L)
- [[R-002]] — CA-OV 分层架构 (2026-05-31)

## 三阶段架构

### C-stage

- [[C-001]] C-stage daemon thread 异步化
  - [[C-002]] turn_index 分配
  - [[C-003]] 降级链 (LLM fallback ⚠️ v4.4 历史)
    - [[C-003a]] num_predict 512→24768
    - [[C-003b]] LLM 参数三级回退
    - [[C-003c]] OODA 文本 vs JSON ⚠️ 历史
    - [[C-003d]] OODAParser 别名匹配 ⚠️ 历史
  - [[C-004]] 工具轮规则引擎
    - [[C-005]] 单工具调用拆分 ⚠️ 历史
    - [[C-006]] 三级处理
    - [[C-007]] 四级字段优先级
    - [[C-008]] YAML 配置三段回退
    - [[C-009]] 10 个结构化 Handler
- [[C-010]] A-stage 解耦

### A-stage

- [[C-011]] 三区模型 ⚠️ 历史 — v5.10 topic-aware
  - [[C-011a]] _is_valid_summary fail-close
  - [[C-012]] 尾区保护区 ⚠️ 历史
  - [[C-013]] 尾区保护区 ⚠️ 历史
- [[C-014]] 双路检索 → [[C-014a]] RRF 排序 / [[C-015]] 动态分配 / [[C-016]] 嵌入降级
- [[C-017]] 预算闸门 ⚠️ 历史
- [[C-018]] 工具组组装格式
- [[C-025]] should_compress()=False ⚠️ 历史
- [[C-027]] save/restore 快照 ⚠️ 含缺陷 — `conv_hist[:]=_backup` 破坏 id() 去重 ([[INC-004]])

### L-stage

- [[C-019]] BackfillThread — 已移除，F-stage 替代
  - [[C-020]] 双独立线程 (已移除) / [[C-021]] 限速 (已移除) / [[C-022]] 3 次失败 (已移除) / [[C-023]] 自动拆工具轮 (已移除)

### Plugin 适配

- [[P-001]] 职责分离
  - [[P-002]] 断路器 / [[P-003]] bg_review 跳过 / [[P-004]] 8 hooks / [[P-005]] ToolBuffer / [[P-006]] bg_review 内容清空

## v5.1 注入重构 ⚠️ v5.10 已改用 `_simple_mutation_mode_v5`

- [[V-001]] Replace/Append/Off → [[V-002]] 行类型表 ⚠️ 历史 / [[V-003]] bypass_turns / [[V-004]] _format_tool_group_assembly ⚠️ 历史 / [[V-005]] 工具行清空

## Schema v5 重构

- [[SC-001]] 主键重构 → [[SC-002]] 3 钩子采集 / [[SC-003]] 惰性迁移 / [[SC-004]] query_embedding

## 存储层

- [[S-001]] SQLite + WAL → [[S-001a]] SessionManager / [[S-002]] 重试 ⚠️ 历史 / [[S-003]] schema迁移 ⚠️ 历史 / [[S-004]] WAL checkpoint ⚠️ 历史 / [[S-005]] 无 rollback ⚠️ 历史

## 内存缓存

- [[D-001]] AssemblyCache 单例 → [[D-002]] 冷却 ⚠️ 历史 / [[D-003]] 对话/工具双独立 BM25 索引 / [[D-004]] 快照原子引用替换 / [[D-005]] get_snapshot_data() 线程安全副本 / [[D-006]] 防竞态

## 去重 ⚠️ v4.4 全部历史

- [[DE-001]] 全指纹去重 → [[DE-002]] 规范化 / [[DE-003]] Fail-Safe / [[DE-004]] role 字段

## 检索

- [[RE-001]] BM25 自实现 / [[RE-002]] RRF K=60

## 工具摘要

- [[T-001]] Elm 格式 → [[T-002]] P0 头尾截断

## 嵌入服务

- [[E-001]] 多后端 → [[E-002]] 连接池 / [[E-003]] LRU 不缓存 fallback / [[E-004]] 线程安全

## 配置体系

- [[F-001]] Config 类 → [[F-002]] protect_tail 唯一源 / [[F-003]] _assemble_status 常量 / [[F-004]] CONTEXT_LENGTH

## 特殊

- [[TK-001]] Token 估算 ⚠️ 历史 / [[HC-001]] 硬截断 ⚠️ 历史 / [[TH-001]] 锁策略 / [[SYS-001]] _system_overhead 移除

## 话题拣选 (v4.6.0)

- [[TP-001]] 话题分割 → [[TP-002]] 三级话题定级+替换映射表 / [[TP-003]] TopicRetriever / [[TP-004]] topic_boost / [[TP-005]] 移除预选 / [[TP-006]] 水位压力 / [[TP-007]] 故障安全降级

## CE In-Place 删行 (v6.1)

- [[TP-010]] CE In-Place 删行 — should_compress=True + compress 原地删除 + _full_backup restore + abort sentinel 阻断 archive/rotation

## 死代码清理 (v5.10)

- [[TP-008]] 按设计删除废弃代码 — 清理 v4/v5 混合时代遗留的 turn_cache、turn_plan、旧 mutation 方法

## 远期规划 — 三方向战略决策

- [[TP-009]] 手段与目标 — OV 话题摘要 / CE 接口删行 / 多 OODA 链
  - [[TP-009a]] 方向一: 接入 OV 做话题摘要
  - [[TP-009b]] 方向二: CE 接口实现历史列表替换干行
  - [[TP-009c]] 方向三: 多 OODA 链 + 事实摘要集群

## 替换 Hermes compress engine (CE) ⚠️ 已废弃

- **已由 [[TP-009]] 取代** — 旧 monkey-patch 方案 (`_compress_context` 接管) 改为标准 `ContextEngine` ABC 实现
- [[CE-000]] 接管 Hermes compress engine 管道
  - [[CE-001]] 帧扫描获取 agent 引用
  - [[CE-002]] 空壳 pass-through 架构
  - [[CE-003]] restore-before-write 恢复长表写 state.db
- [[CE-004]] v6.0 CE 壳 — CAContextEngine 实现 ContextEngine ABC，should_compress=False，compress() 手动 /compress 回退（FAR 行删除 + 尾区保护），register() 注册 ctx.register_context_engine()

## Fct 摘要重构 (v4.7.0, PDD)

- [[Fct-001]] PDD → [[Fct-002]] 新 Prompt / [[Fct-003]] 独立配置 (L1_MAX_TOKENS 默认值 v6.0.2 从 800 提升至 2048) / [[Fct-004]] 新防御性解析器 / [[Fct-005]] 截断检测双重校验 / [[Fct-006]] L1TruncatedException / [[Fct-007]] 旧数据适配 / [[Fct-008]] Metrics 4 指标 / [[Fct-009]] _safe_truncate() 降.优先级 / [[Fct-010]] 预编译正则 / [[Fct-011]] MEANINGLESS_CORE 语义短路 / [[Fct-012]] 标签清洗移除

## E-stage 简化版 (v5.0)

- [[ES-001]] 写即落盘替代内存 buffer
- [[ES-002]] turn_stream (turn,seq) 统一坐标
- [[ES-003]] _simple_mutation_mode 跳过 cache 层

## v5.0 重构 (RF — Refactoring)

- [[RF-001]] 术语重命名 Elm/Fct/Hdl, F-stage
- [[RF-002]] F-stage 从 DB 读 Elm
- [[RF-003]] stage_tag 独立 XML 标签
- [[RF-004]] thought 截断修正 — 句子边界

## 历史演化

- [[H-001]] SQLite 选型 / [[H-002]] 弃用 sqlite-vec (v4.2) / [[H-003]] num_predict 演进 / [[H-004]] 去重阈值 0.88
- [[H-005]] C-stage 异步化 (v4.3) / [[H-006]] 内存计数器 turn_index (v4.3) / [[H-007]] CA_DEBUG 开关 (v4.3.1)
- [[H-008]] 10 handlers (v4.5.1) / [[H-009]] 去重保留最先+原位指向 (v4.5.1)
- [[H-010]] turn_plan 驱动 / [[H-011]] state DB 污染切断 (v5.1) / [[H-012]] biz_category 双向嵌入 (v5.1)

## 事故与审查

- [[INC-001]] v4.3.2 14 项修复 / [[INC-002]] 早期三连故障 / [[INC-003]] 286K 篡改事故 / [[INC-004]] state.db 重复写入 — 最终修复: 去掉 _full_backup 写 state.db
- [[CR-001]] 3 项设计偏差 / [[CR-002]] stats 计数器 / [[CR-003]] 测试导入 / [[CR-004]] get_turn_ca_rows 7 列返回修复 / [[CR-005]] _compute_centroids session_id 断链 + Fct 空值保护 / [[CR-006]] FAR thought/tool 替换为 "略" 而非空串 / [[CR-007]] 断路器函数缺失 — TP-008 误删 + 双重 DOA
- **CR-008** (v6.0.2): 首轮 Fct band-aid 移除 — 删 f_stage.py 跳过 LLM 分支，改空 Fct JSON 归一化
- **CR-009** (v6.0.2): 截断 fallback 改进 — PAIR_PATTERN 优先提取 XML 对 + Tool Fct 代码体剥离 (terminal command + MCP code)

## 已知差距

- [[GAP-1]] `_available_budget()` 预算未传入 / [[GAP-2]] v5.10 已替代 — 尾保护区改用「倒数第2个user轮」
- [[GAP-3]] tool_group l2_tokens 列未写入 / [[GAP-4]] write_turns_batch 无显式 rollback
- [[GAP-5]] `_parse_bool_env` 非 fail-safe / [[GAP-6]] 无 `__all__` in stats/health
- [[GAP-7]] 核心模块零直接单元测试 / [[GAP-8]] 空壳测试文件 / [[GAP-9]] 端到端全 mock LLM
- [[GAP-10]] v5.1 `_format_tool_group_assembly` 实现在 tester profile 独立副本

---

> 基线 A (v4.4.0): `~/projects/context-assembler/` — C-011~C-023
> 基线 B (v6.0): `~/.hermes/profiles/tester/plugins/ca_assembler/` — V-001~V-005, P-005, RF-*, ES-*, CE-*, CE-004
> 更新: 2026-06-28 — CR-008, CR-009: 首轮 Fct band-aid 移除 + 截断 fallback 改进 + Tool Fct 代码体剥离; Fct-003: L1_MAX_TOKENS 800→2048
