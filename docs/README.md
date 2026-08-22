# CA 技术方案

## 双维度文档

本目录是 CA 插件的完整技术方案，按双维度组织：

### architecture/ — 空间维度：当前系统组件

14 页，每页描述一个架构组件的当前设计。每页至少包含：

- frontmatter：`decisions`（哪些决策影响了此组件）、`depends_on`（依赖的其他组件）
- 问题 → 备选方案 → 选定方案 → 实现要点 → 数据验证 → 优点 → 约束

### decisions/ — 时间维度：决策树

32 页，每页描述一个时间点上的决策，按时间顺序排列。每页包含：

- frontmatter：`date`（决策日期）、`alternative`（备选方案）、`chosen`（选定方案）、`affects`（影响的组件）
- 触发条件 → 备选方案 → 选定 → 之前 vs 之后 → 影响

## 新旧对照

| 维度 | 旧方案 | 新方案 |
|------|--------|--------|
| 技术方案 | `docs/tech-plan-v5.5/`（14 页无 frontmatter，已删除） | `docs/wiki/architecture/`（14 页+frontmatter+决策关联） |
| 决策树 | OpenViking `projects/context-assembler/decisions/decision-points/`（119 节点，v4.x） | `docs/wiki/decisions/`（32 页，v0.x~v7.1.2，含旧树聚合） |
| 架构设计 | OpenViking `projects/context-assembler/architecture/`（7 个独立文档） | 已合并入 `docs/wiki/architecture/` 或 OV 查阅 |

## 文件结构

```
docs/
├── wiki/
│   ├── INDEX.md                        # 决策关系图 + 全量索引
│   ├── architecture/                   # 当前系统组件（14 页）
│   │   ├── 01-storage-model.md
│   │   ├── 02-e-stage-write-protocol.md
│   │   ├── ...
│   │   └── 14-rejected-approaches.md
│   └── decisions/                      # 决策树（32 页）
│       ├── 01-design-philosophy.md
│       ├── 02-sqlite-wal-storage.md
│       ├── ...
│       └── 32-state-db-user-dedup.md
├── design/              # 历史设计文档（被 wiki 取代，保留参考）
├── analysis/            # 分析报告（不合并，保持分析性质）
├── debug/               # 调试记录（不合并，保持记录性质）
└── AGENTS.md            # 开发指南（指向 wiki）
```

## 与 Git 的关联

本目录将作为 CA 插件 git 仓库的一部分提交。 

OpenViking watch 配置（可选）：
```yaml
path: "https://raw.githubusercontent.com/.../ca_assembler/docs/wiki/"
watch_interval: 1440
```

## 术语统一

- **Elm**：原始 LLM 数据（旧：L2、原始数据、`elp`）
- **Fct**：单轮摘要（旧：L1、摘要）
- **Hdl**：历元摘要（旧：L0、历元）
- **E-stage**：写入阶段（旧：C-stage）
- **F-stage**：异步摘要阶段（旧：L-stage）
- **A-stage**：装配阶段（保留）
- **Grade**: Grade.ELM / Grade.FCT / Grade.HDL（旧：2/1/0）
