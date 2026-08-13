# CA 插件重构交接文档（2026-08-13）

> **用途**：新会话启动 CA 重构项目前的自举材料。读完本文件 + AGENTS.md + docs/INDEX.md 即可开工。
> **上一会话已完成**：DSH 迁移调研、CE 壳故障修复、设计符合性审计（§6）、文档修订、gateway 重启恢复。
> **本文件待办**：记录当前状态、待拍板设计问题、推荐工作流（flash 编排 + pro 执行）、禁区清单、验证基线。

---

## 1. 当前状态（2026-08-13 21:55 快照）

| 项 | 状态 |
|---|---|
| CA 插件运行 | ✅ 已修复并恢复。sysadmin gateway PID 8218、tester PID 8701（均重启后无加载失败）；winker 副本已同步待下次启动 |
| 修复内容 | `__init__.py` register() 注释掉 CE 壳注册（旧 2 参调用必抛 TypeError；Hermes commit 22af80bcf 起失败会回滚全部 hooks → 插件停摆） |
| 三副本同步 | tester / sysadmin / winker 三份 `__init__.py` 一致（`ca/` 等引擎代码本就一致）；修复后 register() 只注册 8 个 hooks |
| 测试基线 | 全量 pytest：1008 passed / 1 failed（`test_fact_linking.py::TestProfileFilter::test_foreign_profiles_removed`，既有环境性失败，与修复无关）/ 1 skipped / 1 xfailed |
| 文档修订 | architecture 01/05/08、decisions 06/08/09/15/20/22、docs/INDEX.md、根 INDEX.md、AGENTS.md 均已按代码修订 |
| 审计记录 | `docs/migration-research-dsh.md` §6（设计簇分析 + 15 项决策层差异 + 13 篇架构文档核对 + 修订闭环说明） |

**生效状态说明**：CA 当前以 **hooks 模式**运行（E-stage 写库 / F-stage 异步摘要 / 话题检测 / wiki recall 注入）——这是 08-12 前数月以来的生产实际状态。**A-stage 重建（CE 壳 compress() 路径）未激活**，且从未在生产运行过。

## 2. 待你拍板的设计问题（重构前必须先定，pro 只给分析不给结论）

| # | 问题 | 选项 | 关联材料 |
|---|------|------|---------|
| 1 | **CE 壳去留（设计簇 A/B 分裂）** | A：正式退役（删/停 CE 壳代码，决策 06/20 标记退役，`context.engine: ca_assembler` 配置清理）<br>B：激活（改 1 参注册 + 补三前置：条件式 should_compress、pre_llm_call 模式守卫、前检压缩与 seq 0 写入的轮序处理）<br>C：维持现状（注册暂停、hooks 驱动、内置 compressor 兜底） | §6.1 两簇分析；TP-009 vs 决策 06/20 |
| 2 | **决策 22（on_session_finalize 去重）** | 补实现（register() 补注册该 hook + SQL 清理相邻重复 user 行）or 正式标记"已撤销" | decisions/22 修订注 |
| 3 | **决策 15（SHA256 指纹去重）** | 补实现 or 正式标记"已撤销"（当前 dict key 去重够用） | decisions/15 修订注 |
| 4 | **死代码处置** | 删除 or 保留标注（retrieval.py 双路检索、cache.py 四字典、`scripts/` 遗留、`_A_stable_cache` 残留字段） | architecture 07/08；§6.4 |
| 5 | **should_compress 立场**（若走 B） | 无条件 True（现状）→ 条件式"有旧话题且 token 超阈值"（TP-009 设计） | §6.2 差异 #3 |

## 3. 推荐工作流：flash 编排 + pro 关键执行

```
Step 0  基线冻结：git commit 当前全部改动（含本交接文档）——重构前必有回滚点
Step 1  pro 全量审计：27 模块 vs 决策树（OV 141+ 节点）逐条核对
        → 产出"现状 vs 应然"清单 + 优先级（文档问题/死代码/行为决策三分类）
Step 2  用户拍板 §2 的 5 个设计问题（pro 给利弊分析，用户给结论）
Step 3  按批准计划执行（flash 机械执行 + pro 复核边界；pro 出方案 flash 落地）
Step 4  全量测试回归（基线 1008 passed）+ 文档同步 + 三副本同步
```

**分工原则**：
- **flash** = 编排（拆任务、写简报、收集结果、跑回归、同步文档/副本、维护进度）
- **pro** = 关键执行（深审计、设计矛盾分析、复杂重构实现、长上下文单点任务）
- **用户** = 设计把关（只拍板设计决策，不写代码）

**升 pro 触发条件**（flash 预设规则）：审计任务必 pro；修改任务先 pro 出方案 flash 执行；测试失败且 flash 两轮修不动 → 升 pro。

**pro 任务简报要求**：自包含（目标、文件路径、约束、验收标准），每次声明 §4 禁区为不可重构。

## 4. 禁区清单（用户设计定论，pro 只核对不重构）

来自 AGENTS.md「关键约束」，逐条声明为不可重构：
1. **不写 state.db** — 任何代码路径不调用 SessionDB/replace_messages/append_message
2. **reality 已取代 theme（决策 41）** — theme 层退役，工作对象在 realities 表
3. **4B+代码协作分工** — 代码=结构化提取/兜底，LLM=语义融合；Fct 数据模型必须含 OODA、stage_tag 结构化元信息
4. **Fct 按 OODA 四段存储** — 现象/背景/决策/后续，topic summarizer 必须保留四段结构
5. **4B 超时降级保守原则** — 不 assume same topic merge，保守 skip 优于错误合并
6. **E-stage 写即落盘** — 不经 buffer
7. **方向 B** — _build_conv_history_v6 从 DB 重建、不修改 Hermes 消息
8. **向量=负向排除器** — 必要不充分

## 5. 验证基线

```bash
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
pytest tests/ -q   # 期望 1008 passed / 1 failed(既有) / 1 skipped / 1 xfailed

# 插件加载验证（Hermes 真实加载器）
HERMES_HOME=/home/i1j/.hermes/profiles/tester \
  python3 -c "from hermes_cli.plugins import PluginManager; \
  m=PluginManager(scope_key='/home/i1j/.hermes/profiles/tester'); m.discover_and_load(); \
  p=m._plugins.get('ca_assembler'); print(p.enabled, p.error, p.hooks_registered)"

# 运行时验证（需新会话触发）
grep "CA plugin started" ~/.hermes/profiles/sysadmin/logs/agent.log   # 应出现新会话记录
grep "Failed to load plugin" ~/.hermes/profiles/sysadmin/logs/errors.log  # 不应新增
```

## 6. 关键参考文件

| 文件 | 内容 |
|------|------|
| `AGENTS.md` | 开发指南 + 关键约束（含 CE 壳注册暂停说明） |
| `docs/INDEX.md` | 架构 + 决策全量索引（已按代码修订状态） |
| `docs/migration-research-dsh.md` | §1.5 停摆证据链 / §6 设计符合性审计（含修订闭环） |
| `docs/architecture/01-overview` | 系统概览（CE 壳定位已修订） |
| `docs/decisions/20-ce-shell-registration` | CE 壳注册决策 + 2026-08-13 修订 |
| `docs/decisions/06-direction-b` | 方向 B + 生产未激活修订 |
| OV 决策树 | `viking://resources/projects/context-assembler/decisions/decision-points/`（141+ 节点） |

## 7. 新会话开场建议

> 新会话第一条消息可写：
> "阅读 /home/i1j/.hermes/profiles/tester/plugins/ca_assembler/docs/ca-refactor-handoff.md + AGENTS.md + docs/INDEX.md。
> 目标：CA 插件彻底检查与重构。按交接文档 §3 工作流执行：先 Step 0 基线冻结，再 Step 1 pro 全量审计（flash 编排、pro 执行），审计完成后把 §2 的 5 个设计问题连同 pro 分析一并交我拍板。"
