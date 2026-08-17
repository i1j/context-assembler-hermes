# CA Assembler — Hermes Agent 插件

*[English](README.md)*

**ContextAssembler v6.x** — Context Assembler 的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
插件版（Python）：让上下文窗口保持高密度、缓存友好、低开销——花尽可能少的钱，向云端 LLM 提供
单位 token 互信息密度最大的 ctx。

> 这是 **Hermes（Python）版本**。设计已延续至 **CA-DSH V0.99**（
> [DeepSeek Harness（dsh）插件版](https://github.com/i1j/ca-dsh)），后者正在积极开发中。
> 本仓库作为 Hermes 原始设计的参考实现维护。

## 功能

- **E-stage 写即落盘**：5 个 hook 在会话轮到达时即时落盘，含工具调用、LLM 调用与思考轨迹
  （决策 44：块级 OODA 打标）。
- **F-stage 事实提炼**：后台本地 4B 守护线程把原始轮次转为事实：`Fct`（OODA 四段）+ `Hdl`，
  多事务交易帧支撑。
- **话题管理**：实时话题块分割（强制短语 → 确认轮 → Jaccard + 水位压力：ctx 越多切割越主动，
  满压必切）。
- **定级与缓存冻结**：话题切换时按当前提问 embedding 形心半径把旧块定级 ACT/REL/FAR，冻结到
  下次切换——上下文前缀稳定，云端 prompt 缓存持续命中。
- **A-stage 历史装配**：`_build_conv_history_v6`：尾部硬保护 2 个 user 轮全保留；ACT/REL/FAR
  按行类型降级；FAR 的 thought/tool 删除；切换时构建话题块摘要版本（每块 2-6 个 strand，一次 4B）。
- **reality 归并与注入**：strand 按工作关联归并为 reality（提问云/工作云 + 4B 承接判定）；新话题块
  开头注入相关 reality（θ≤0.5、top-15 池 → 4B 拣选；4B 合法空列表 = 空注入，宁缺勿错）。
- **工具轮摘要**：确定性 per-tool 结构化摘要（bash 退出码/stderr/关键行、read 路径+行数、
  edit/write 路径、MCP JSON 关键字段）。
- **L-stage 精炼**：空闲精炼守护：归并审查 → 详情重生成 → 交叉验证 → 健康评分 → 知识子图；
  L1 代码（0 token）→ L2 本地 4B → L3 云端。

## 结构

```
ca/                  # 插件实现（各 stage、store、reality、话题管理……）
topic_manager.py     # 话题分割引擎
tests/               # pytest 分层测试（unit / stage / store / plugin / audit）
scripts/             # 离线运维：reality 迁移、重跑管线、wiki 同步……
docs/                # 双维度 wiki —— 入口见 docs/README.md
├── architecture/    # 当前系统组件（01-overview … 14-idle-refinement + 设计笔记）
└── decisions/       # 决策树（01–45 编号 ADR）
plugin.yaml          # Hermes 插件清单
```

## 安装与运行

插件运行于 Hermes Agent profile 内。`plugin.yaml` 声明了 pip 依赖
（`numpy`、`urllib3`、`sentence-transformers`、`pyyaml`）与注册的 hooks。把 profile 的插件根
指向本仓库，按 `ca/config.py` 中文档化的 `CA_*` 环境变量配置（本地 4B LLM 端点、embedding 端点、
话题阈值、上下文预算），再启动 Hermes。

运行测试：

```sh
python3 -m pytest tests/
```

## 文档

- [docs/README.md](docs/README.md) — 双维度 wiki 入口（架构 + 决策）
- [docs/architecture/](docs/architecture/) — 当前系统组件
- [docs/decisions/](docs/decisions/) — 决策记录（ADR），01–45
- 权威设计意图与到 CA-DSH V0.99 的映射见 [CA-DSH docs/DESIGN.md](https://github.com/i1j/ca-dsh/blob/main/docs/DESIGN.md)

## 许可

MIT © 2026 [i1j](https://github.com/i1j) — 见 [LICENSE](LICENSE)。
