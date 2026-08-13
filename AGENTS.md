# CA Assembler Plugin — 开发指南

> **首读** → [`docs/INDEX.md`](docs/INDEX.md)（架构关系图 + 全量索引）
> 双维文档体系：`docs/architecture/`（空间：当前系统组件）+ `docs/decisions/`（时间：决策树）
> **版本 v6.0（方向 B）** | `__init__.py: CE v5.10` | `ca/__init__.py: SessionManager`

## 项目结构

```
plugins/ca_assembler/
├── __init__.py               # 插件入口：hook 注册 + CE shell + 插件类
├── plugin.yaml               # 插件元信息
├── AGENTS.md                 # ← 当前文件（开发指南）
├── INDEX.md                  # 聚合索引（指向 wiki）
├── ca/
│   ├── __init__.py           # ContextAssembler + SessionManager
│   ├── a_stage.py            # A-stage: _build_conv_history_v6（方向 B）
│   ├── e_stage.py            # E-stage: 写即落盘（5 hooks）
│   ├── f_stage.py            # F-stage: 异步 LLM 摘要（fin 粒度）
│   ├── store.py              # turn_stream SQLite 存储
│   ├── cache.py              # AssemblyCache
│   ├── config.py             # 配置系统
│   ├── embedding.py          # 嵌入服务
│   ├── retrieval.py          # BM25 + 向量双路检索 + RRF
│   ├── tool_summarizer.py    # 工具调用摘要引擎
│   ├── lstage.py             # L-stage 后台守护线程
│   ├── grade.py              # Grade / TopicGrade 枚举
│   ├── health.py             # 健康检查
│   ├── prompts.py            # LLM prompt 模板
│   ├── stats.py              # 统计
│   └── exceptions.py         # 异常定义
├── topic_manager.py          # TopicGradeManager（话题检测+定级）
├── docs/
│   ├── INDEX.md              # ← ★ 入口：关系图 + 全量索引
│   ├── architecture/         # 空间维度：系统组件（01-overview 起）
│   ├── decisions/            # 时间维度：关键决策（含 2026-08-13 代码核对修订）
│   └── migration-research-dsh.md  # DSH 迁移调研 + 设计符合性审计（§6）
└── tests/                    # pytest 测试
```

## 关键约束

- **不写 state.db** — CA 任何代码路径不调用 `SessionDB`、`replace_messages`、`append_message`
- **不硬编码 `~/.hermes` 路径** — 使用 `get_hermes_home()` 获取 CA cache 目录
- **术语唯一** — Elm/Fct/Hdl（禁用 L0/L1/L2）
- **theme 语义定义（用户设计哲学）** — theme = 多个语义独立但工作中有关联的事物的集合（工作线集合，**非语义簇**）；语义与工作关联是正交维度（向量测语义、4B 判工作）。设计从根本目的出发（theme 目的=切换时提供有效背景数据），先立衡量标准再定参数。
- **reality 已取代 theme（v7 决策 41，2026-08-07）** — theme 层退役，现实工作对象存 `realities` 表；归并 `run_reality_merge`（S 匹配分）、注入 `pick_injection_realities`（提问云形心）、graphify 节点 `reality_{id}`。改 reality 链路代码前必读 `docs/decisions/41-reality-production-migration.md/41-reality-production-migration.md` + OV `decisions/41-reality-production-migration.md/41-reality-production-migration.md`。
- **向量=负向排除器** — 必要不充分：用词完全不相关必非同事物，向量近≠一定同。
- **4B+代码协作设计（用户偏好）** — 先理清入口/出口数据模型再动手，不靠反复迭代调参找方向（"仔细思考，不要来回折腾"）。Fct 数据模型必须含 OODA、stage_tag 等结构化元信息（非纯文本串）：4B 一份完整数据调一次做跨轮融合+事实提炼+title，代码做精确提取兜底。分工：代码=结构化提取/兜底，LLM=语义融合。
- **Fct 按 OODA 四段存储** — Fct 数据按 OODA 四段（现象/背景/决策/后续）组织，topic summarizer 必须保留四段结构，勿被清理逻辑剥离（用户强调过的数据模型约束）。
- **4B 超时降级保守原则** — 超时/失败时不 assume same topic merge，保守 skip 优于错误合并。
- **E-stage 写即落盘** — 每条消息立即写入 turn_stream，不经 buffer
- **方向 B** — `_build_conv_history_v6` 从 turn_stream DB 重建 conv_history，不修改 Hermes 消息
- **CE 壳 = 替代内置 compressor 的占位（用户设计定论）** — CA 注册 ContextEngine（`context.engine: ca_assembler`）是**替代** Hermes 内置 ContextCompressor 的占位；**CA 不触发 Hermes compress_context 流程**。注意（代码事实）：`_build_conv_history_v6`（A-stage 重建）**仅**由 CE 壳 `compress()` 调用，8 个 hooks 负责的是 E-stage 写入 / F-stage 摘要 / 话题检测 / recall 注入（即 A-stage 重建在 CE 壳未激活时不会运行；"A-stage 完全由 hooks 驱动"是文档误述，见 `docs/migration-research-dsh.md` §6）。
- **⚠️ CE 壳注册已暂停（2026-08-13 修复，勿再恢复旧 2 参写法）** — 旧代码 `ctx.register_context_engine("ca_assembler", _ce_engine)` 传 2 参数 vs hermes 接口 `register_context_engine(self, engine)` 1 参数 → 必抛 TypeError；Hermes commit `22af80bcf`（2026-08-01）起 register() 抛异常会 **dispose 该插件全部 registration（含 8 个 hooks）→ 整个插件加载失败、CA 停摆**（08-13 实测 errors.log 连续报 `Failed to load plugin 'ca_assembler'`，agent.log 无 CA 活动）。修复：`__init__.py` register() 已注释掉该行（hooks 恢复、engine 回退内置 compressor，恢复生产停摆前的实际状态）。**如要恢复 CE 壳**：改 1 参 `ctx.register_context_engine(_ce_engine)` 会激活 A-stage（should_compress 恒 True → 每轮 compress() 从 DB 重建 conv_history）——该路径从未在生产运行过且前检压缩先于 pre_llm_call 写 seq 0（存在当前轮用户消息缺失的轮序风险），恢复前先读 `docs/migration-research-dsh.md` §1.5/§5.3；若只想"纯占位"需同时把 should_compress 改 False。**旧文档中"参数警告=无害已知项、hooks 不被回滚"的记录已失效，勿再引用。**

## 注册接口

`register(ctx)` 注册 8 个 Hermes hooks（5 生命周期 + 3 工具轮数据采集），详见 `docs/architecture/03-e-stage/03-e-stage.md`。

## 初始化检查清单

```bash
# 1. 验证插件加载
hermes plugin list | grep ca_assembler

# 2. 验证 SessionManager 启动
grep "CA plugin" ~/.hermes/logs/agent.log

# 3. 验证 CE 壳状态（2026-08-13 起 CE 壳注册已暂停）
hermes config get context.engine
# 配置值仍为 'ca_assembler'，但运行时引擎已回退内置 compressor（CE 壳未注册，
# 见上方"CE 壳注册已暂停"约束）。CA 工作正常的判定标准 = hook 链路数据完整
# （agent.log 出现 "CA plugin started" + E-stage/F-stage/话题/recall 日志），
# 与 CE 是否被选中无关。
```

## 设计细节与调试细节（迁移至 OV）

> 本文件只保留入口级信息。话题摘要链路关键经验（Jaccard 铁则 / topic_split 阈值 / merge 策略 / theme 链路 / token 配置 / 路径解析链 / hdl 规范）、部署与多 profile 同步、Codex 修复工作流等**设计/调试细节已迁移至 OpenViking 技术文档**：

- **完整技术参考**：`viking://resources/projects/context-assembler/AGENTS/AGENTS.md`
- **决策页**（按主题）：`docs/decisions/35-strand-multi-affair-summarization/35-strand-multi-affair-summarization.md`（strand 摘要 / hdl 规范 / Jaccard 铁则）、`docs/decisions/36-theme-wiki-generation/36-theme-wiki-generation.md`（theme 归并 / 注入 / 链路经验）、`docs/decisions/37-reality-restructure.md/`（reality 重构）、`docs/decisions/38-reality-graph-inject-merge.md/`（图模型/S 匹配分）、`docs/decisions/39-reality-member-clouds.md/39-reality-member-clouds.md`（成员云）、`docs/decisions/40-flash-reprocess-pilot.md/40-flash-reprocess-pilot.md`（**flash 重跑**）、`docs/decisions/41-reality-production-migration.md/41-reality-production-migration.md`（**生产 reality 化，theme 退役**）、`docs/decisions/42-hindsight-borrowed-retrieval/42-hindsight-borrowed-retrieval.md`（**Hindsight 借鉴：检索图路/时序路 + 心智模型层**，精炼轮 v2.5 联动）

改代码前必读对应决策页（尤其涉及话题摘要链路、theme 归并、独立脚本跑 reprocess/evaluate 时）。

## OV 中的 CA 项目文档

完整项目文档（变更日志、决策树 141+ 节点、Wiki 知识库）在 OpenViking：

```
viking://resources/projects/context-assembler/
```

搜索命令：

```bash
viking_search("CA 项目", scope="viking://resources/projects/context-assembler/")
```
