# CA Assembler Plugin — 开发指南

> **首读** → [`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)（架构关系图 + 全量索引）
> 双维文档体系：`docs/wiki/architecture/`（空间：当前系统组件）+ `docs/wiki/decisions/`（时间：决策树）
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
│   └── wiki/                 # 技术方案文档（首读 INDEX.md）
│       ├── INDEX.md          # ← ★ 入口：关系图 + 全量索引
│       ├── architecture/     # 空间维度：13 系统组件
│       └── decisions/        # 时间维度：23 关键决策
└── tests/                    # pytest 测试
```

## 关键约束

- **不写 state.db** — CA 任何代码路径不调用 `SessionDB`、`replace_messages`、`append_message`
- **不硬编码 `~/.hermes` 路径** — 使用 `get_hermes_home()` 获取 CA cache 目录
- **术语唯一** — Elm/Fct/Hdl（禁用 L0/L1/L2）
- **theme 语义定义（用户设计哲学）** — theme = 多个语义独立但工作中有关联的事物的集合（工作线集合，**非语义簇**）；语义与工作关联是正交维度（向量测语义、4B 判工作）。设计从根本目的出发（theme 目的=切换时提供有效背景数据），先立衡量标准再定参数。
- **reality 已取代 theme（v7 决策 41，2026-08-07）** — theme 层退役，现实工作对象存 `realities` 表；归并 `run_reality_merge`（S 匹配分）、注入 `pick_injection_realities`（提问云形心）、graphify 节点 `reality_{id}`。改 reality 链路代码前必读 `docs/wiki/decisions/41-reality-production-migration.md` + OV `design/41-reality-production-migration.md`。
- **向量=负向排除器** — 必要不充分：用词完全不相关必非同事物，向量近≠一定同。
- **4B+代码协作设计（用户偏好）** — 先理清入口/出口数据模型再动手，不靠反复迭代调参找方向（"仔细思考，不要来回折腾"）。Fct 数据模型必须含 OODA、stage_tag 等结构化元信息（非纯文本串）：4B 一份完整数据调一次做跨轮融合+事实提炼+title，代码做精确提取兜底。分工：代码=结构化提取/兜底，LLM=语义融合。
- **Fct 按 OODA 四段存储** — Fct 数据按 OODA 四段（现象/背景/决策/后续）组织，topic summarizer 必须保留四段结构，勿被清理逻辑剥离（用户强调过的数据模型约束）。
- **4B 超时降级保守原则** — 超时/失败时不 assume same topic merge，保守 skip 优于错误合并。
- **E-stage 写即落盘** — 每条消息立即写入 turn_stream，不经 buffer
- **方向 B** — `_build_conv_history_v6` 从 turn_stream DB 重建 conv_history，不修改 Hermes 消息
- **CE 壳 = 替代内置 compressor 的占位（用户设计定论，禁止再当缺陷排查）** — CA 注册 ContextEngine（`context.engine: ca_assembler`）是**替代** Hermes 内置 ContextCompressor 的占位；**CA 不触发 Hermes compress_context 流程**（should_compress 固定 False 或 sentinel 阻断 archive+rotation），A-stage 组装（`_build_conv_history_v6`）完全由 8 个 hooks 驱动（pre_llm_call 写 seq 0 → 话题检测 → wiki recall；post_llm_call 写 fin → F-stage）。"CE 未注册 / compress 未调用 / A-stage 未激活" 均属**预期**，不是缺陷。
- **register_context_engine 参数警告 = 无害已知项** — `__init__.py:191` `ctx.register_context_engine("ca_assembler", _ce_engine)` 传 2 参数 vs hermes 接口 `register_context_engine(self, engine)` 1 参数 → 每次进程启动打 "Failed to load plugin 'ca_assembler'"（errors.log 8 月 26 条，均发生在 gateway 重启时）。hooks 在 register() 前半段已注册成功、不被回滚，功能不受影响；按"替代 compressor"定位此警告**不构成缺陷，勿重复排查**。

## 注册接口

`register(ctx)` 注册 8 个 Hermes hooks（5 生命周期 + 3 工具轮数据采集），详见 `docs/wiki/architecture/03-e-stage.md`。

## 初始化检查清单

```bash
# 1. 验证插件加载
hermes plugin list | grep ca_assembler

# 2. 验证 SessionManager 启动
grep "CA plugin" ~/.hermes/logs/agent.log

# 3. 验证引擎版本
hermes config get context.engine
# 应返回 'ca_assembler'（或由 CE shell 触发）
```

## 设计细节与调试细节（迁移至 OV）

> 本文件只保留入口级信息。话题摘要链路关键经验（Jaccard 铁则 / topic_split 阈值 / merge 策略 / theme 链路 / token 配置 / 路径解析链 / hdl 规范）、部署与多 profile 同步、Codex 修复工作流等**设计/调试细节已迁移至 OpenViking 技术文档**：

- **完整技术参考**：`viking://resources/projects/context-assembler/AGENTS/AGENTS.md`
- **决策页**（按主题）：`docs/wiki/decisions/35-strand-multi-affair-summarization.md`（strand 摘要 / hdl 规范 / Jaccard 铁则）、`docs/wiki/decisions/36-theme-wiki-generation.md`（theme 归并 / 注入 / 链路经验）、`docs/wiki/decisions/37-reality-restructure.md` / `38-reality-graph-inject-merge.md` / `39-reality-member-clouds.md` / `40-flash-reprocess-pilot.md` / `41-reality-production-migration.md`（**生产 reality 化，theme 退役**）、`42-hindsight-borrowed-retrieval.md`（**Hindsight 借鉴：检索图路/时序路 + 心智模型层**，精炼轮 v2.5 联动）

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
