# CA 插件迁移到 DeepSeek Harness（DSH）调研报告

> 调研日期：2026-08-13 | 调研人：DeepSeek Harness 会话（工作目录 `plugins/ca_assembler`）
> 调研对象：`ca_assembler` v6.0/v7 插件（Hermes 上下文组装插件）→ DeepSeek Harness 0.1.0-rc.6（Cordis 插件体系）
> 结论速览：**技术可行性高（核心机制可 1:1 映射到 DSH 扩展点），必要性取决于"是否以 DSH 为主平台"这一前提；迁移是"语义层重写 + 管道层再造"，不是搬运。**
> **⚠️ 2026-08-14 修订**：§1.5"CA 已停摆"结论经复核**只对 08-13 20:57~21:10 瞬时窗口成立**（根因 Hermes 22af80bcf 升级生效）；CA 的 8 个 hooks 此前一直正常运行（08-12 07:53 会话实证），CE 壳（方向 B 压缩路径）自 v6.0 引入即未注册成功。详见 §1.5b 时间线考证。

---

## 0. 摘要

| 维度 | 结论 |
|------|------|
| 可行性 | ✅ 高。DSH 的扩展点体系（`agent/pre-step`、`session/event`、`tools/*`、`ctx.compaction` 服务缝、`ctx.sessions`）与 CA 的 8 个 Hermes hook + ContextEngine 壳存在清晰的一一映射 |
| 必要性 | ⚠️ 条件性，且叠加紧迫性：**CA 的 Hermes 适配壳曾在 08-13 20:57~21:10 因 Hermes 插件 API 变更（22af80bcf）瞬时加载失败**（1 行修复已恢复；hooks 一直正常，仅 CE 壳自 v6.0 未注册，详见 §1.5b）。若 DSH 将取代 Hermes 成为主 Agent 平台 → 必要；若 Hermes 继续主用 → 已修复，迁移不必要（除非要把 CA 重构到更稳的宿主上） |
| 工作量 | 核心逻辑约 1.7 万行 Python（另有 1.7 万行测试）。语言迁移 Python→TypeScript，纯算法层可直译（~95% 自包含），LLM prompt 语义层需重写，管道层需按 DSH 事件模型再造 |
| 原生差距 | DSH 原生已覆盖 CA 的"压缩"职责（compaction 服务）；CA 的**结构化记忆**（turn_stream → Fct/Hdl → topic → reality → 跨会话召回注入）超出 DSH 原生能力，是迁移后 CA 仍存在的价值 |
| 建议 | 决策分叉：①Hermes 留任主平台 → 1 行修复 + 更新 AGENTS.md 过时记录；②DSH 转正 → 分三阶段渐进迁移（最小 CA → compaction 自定义后端 → 语义记忆层） |

---

## 1. CA 插件现状（是什么、解决什么、怎么跑）

### 1.1 定位

CA（ContextAssembler）是 **Hermes 的 Python 插件**，解决 Hermes 内置 ContextEngine（`ContextCompressor`）"被动 LLM 压缩、一条 Markdown 摘要替代历史、上下文质量随对话变长下降"的问题（见 `docs/architecture/01-overview/01-overview.md`）。用户设计定位（AGENTS.md 关键约束）：**CA 注册 ContextEngine 是"替代内置 compressor 的占位"**；`select_context()` 每轮从 turn_stream DB 重建 conv_history（方向 B），`should_compress()` 恒 False 阻断 Hermes 的 compress_context，`compress()` 仅作为手动 /compress 回退路径。（2026-08-16 修订：本节原「should_compress 恒 True + abort 标志、A-stage 由 8 个 hook 驱动」为 08-13 快照的旧状态，已按当前代码更新。）

### 1.2 架构（v6.0 方向 B / v7 reality 化）

```
8 个 Hermes hooks（5 生命周期 + 3 工具轮）
   ├─ on_session_start / on_session_end / on_session_reset   → 引擎生命周期
   ├─ pre_llm_call     → E-stage 写 seq 0 (user Elm) + 话题检测 + wiki/reality 召回注入（返回值进 user message）
   ├─ post_llm_call    → E-stage 写 fin (assistant Elm) + 触发 F-stage 异步摘要
   ├─ post_api_request → E-stage 写 thought 行 + tool 占位行
   ├─ pre_tool_call    → no-op
   └─ post_tool_call   → E-stage 回填 tool 行 + per-tool Fct
        ↓
E-stage 写即落盘 → ca_cache/{session_id}.db (SQLite, turn_stream 表)
        ↓
F-stage 异步 LLM 摘要（每 fin 一个 daemon 线程，Ollama /api/generate）→ Fct(OODA 四段) + Hdl
        ↓
话题管理（TopicGradeManager：Jaccard 检测 + 定级 ACT/REL/FAR + 提问云形心）
        ↓
A-stage 装配（_build_conv_history_v6：从 turn_stream DB 重建 conv_history，Tail/ACT/REL/FAR 三区降级）
        ↓
语义层（v7）：strand 摘要 → reality 归并（run_reality_merge）→ 跨会话召回注入（pick_injection_realities，提问云形心 + 4B 拣选）→ graphify graph.json
        ↓
L4 空闲精炼守护线程（IdleRefinementDaemon）
```

### 1.3 规模与依赖

| 项 | 数据 |
|----|------|
| 源码 | `ca/*.py` 27 个模块共 ~14.9K 行 + `__init__.py` 1.44K + `topic_manager.py` 0.67K ≈ **1.7 万行 Python** |
| 测试 | 63 个 pytest 文件 ≈ **1.7 万行** |
| 外部服务 | Ollama embedding（`qwen3-embedding:0.6b`，127.0.0.1:11435）+ 本地 4B LLM（`qwen3-4b-instruct`）经 `urllib → /api/generate`（**不依赖 Hermes 的 LLM 栈**） |
| 内部状态 | `ca_cache/{session_id}.db`（turn_stream/strand_summaries/realities/...）+ `graphify-out/graph.json` + 断路器状态文件 |
| 第三方库 | numpy、urllib3、sentence-transformers（embedding 后端）、pyyaml |

### 1.4 生产运行状态（实测）

- **活动 profile = sysadmin**，`context.engine: ca_assembler`，plugins.enabled 含 `ca_assembler`（`~/.hermes/profiles/sysadmin/config.yaml`）
- sysadmin 的 `ca_cache/` 有 **139 个会话 DB**；agent.log 中 CA 日志 **259 条**，全链路证据齐全：
  `CA plugin started → pre_llm_call: wrote seq 0 → [CA_WIKI] Session-start wiki recall injected (1820 chars, 3 entries) → grade → post_llm_call: wrote asst_fin → F-stage`
- 主 Agent 模型：`deepseek-v4-flash`，context_length 524288
- **⚠️ 截至调研日（2026-08-13）CA 曾短暂加载失败/停摆**（08-13 20:57~21:10，Hermes 22af80bcf 升级生效，详见 §1.5/§1.5b）——08-14 已修复恢复，调研时发现的瞬时故障

### 1.5 ⚠️ 关键现场发现：CA 当前在 Hermes 上已停摆（AGENTS.md 记录已过时）

> **⚠️ 2026-08-14 修正**：本节"停摆"结论**只对 08-13 20:57~21:10 的 1 小时窗口成立**，且根因是 Hermes 升级（22af80bcf dispose 逻辑生效），不是"CA 停摆已久"。**07-30~08-13 20:59 之间 errors.log 虽持续报 `Failed to load plugin`，但那是 CE 壳注册失败的 WARNING——CA 的 8 个 hooks 一直在正常注册运行**（08-12 07:53 会话 `mspbenwzz2s4i7` 完整 E-stage/F-stage/recall 链路实证）。完整时间线见 §1.5b。

调研中实测确认（证据链完整）：

1. **`register_context_engine` 参数错误长期存在**：`__init__.py:191` 调 `ctx.register_context_engine("ca_assembler", _ce_engine)`（2 参），而 Hermes 签名是 `register_context_engine(self, engine)`（1 参）→ 必然抛 `TypeError`。errors.log 反复记录：`Failed to load plugin 'ca_assembler': PluginContext.register_context_engine() takes 2 positional arguments but 3 were given`（08-13 20:59/21:00/21:09/21:10 多次）。
2. **AGENTS.md"hooks 不被回滚"的说法在当前源码下已失效**：Hermes commit `22af80bcf`（2026-08-01，"feat(plugins): add ownership ledger unload lifecycle"）在 `hermes_cli/plugins.py:4345-4361` 的异常路径加入了 `_dispose_registrations(owned)` + `_forget_registrations(owned)` + `_remove_plugin_subscriptions(plugin_key)`——**register() 抛异常会回滚该插件全部 registration（含 8 个 hooks）并移除订阅**。
3. **08-13 20:59 工作树更新后网关重启即生效**：`plugins.py` mtime = 2026-08-13 20:59:29，与第一批加载失败错误同一分钟；agent.log 中 CA 最后成功启动是 **08-12 07:53**（session mspbenwzz2s4i7），此后无任何 CA 活动（无 E-stage 写入、无 F-stage、无 recall 注入）。
4. **结论**：CA 当前在 sysadmin（活动 profile）上**加载失败、hooks 全部未注册**，相当于停摆。AGENTS.md 把它列为"无害已知项，勿重复排查"的结论已不适用于当前 Hermes 版本。
5. **修复成本极低（若继续用 Hermes）**：`hermes_cli/plugins.py:1898` 是 1 参签名；把 `__init__.py:191` 改为 `ctx.register_context_engine(_ce_engine)` 即不再抛异常 → 8 个 hooks 保留、引擎经 `get_plugin_context_engine()` 回退链（`agent/agent_init.py:2461-2497`）可被 `context.engine: ca_assembler` 选中。或干脆删除该行（AGENTS.md 定位 CE 壳只是占位，hooks 驱动一切）。

**附：CE 壳（CAContextEngine）当前为不可达代码（三重证据）**
1. `register_context_engine("ca_assembler", _ce_engine)` 抛 TypeError → 引擎从未进入 `_manager._context_engine`；
2. 即便注册成功，tester/winker profile 的 `context.engine: compressor` 也不会选中它（`agent_init.py:2461` 仅非 "compressor" 时走插件引擎）；
3. 代码中 `should_compress` 无条件返回 True（`__init__.py:294-308`），与 AGENTS.md 声称的"should_compress 固定 False"**不符**（文档-代码漂移）；若引擎真生效会每轮触发 compress() 重建消息列表。
→ CA 的实际生效路径只有 8 个 hooks（E-stage/F-stage/话题/召回），这与 AGENTS.md"CE 壳=占位、hooks 驱动一切"的定位一致，但"占位"的前提是**注册不失败**——当前已失败。

> **对"必要性"的影响**：CA 的 Hermes 适配壳（~600 行 `__init__.py`）正骑在 Hermes 快速演进的插件 API 上，一个"无害"的参数错误在 08-01 的 Hermes 变更后升级为致命故障——这既是"继续留在 Hermes 需付的维护成本"的实证，也是"若 DSH 转正主平台则应迁移"的动机之一。

### 1.5b 停摆时间线精确考证（2026-08-14 补，多源交叉验证）

> **结论**：CA **从未整体停摆过**。停摆的是 CE 壳（方向 B 压缩路径）——从 v6.0 出生（06-27）即断；而 **hooks 全链路一直运行到 08-13 20:59**。08-13 的"加载失败"是 Hermes 升级瞬间导致，21:52 修复即恢复。DSH 报告把"最后成功启动 08-12 07:53"误读为"停摆已久"，实际是**升级导致的瞬时故障**。

**完整时间线（证据来源：git 历史 + errors.log/agent.log/gateway.log + ca_cache DB + reality-strand）**：

| 时间 | 事件 | 状态 | 证据 |
|---|---|---|---|
| 06-26/27 | v6.0 引入 `register_context_engine("ca_assembler", _ce_engine)`（2 参）；Hermes 签名恒 1 参（`92382fb00` 起） | ❌ CE 壳注册必失败（出生即断） | git `562cbe8`/`d33215a` L188；`e4c7daf` commit 信息 |
| 06-19~06-27 | **双路径并存**：CE 壳 compress 调 `_build_conv_history_v6` + pre_llm_call mutation 都在跑（reality 39 记录 06-19 曾"3参→2参修复后正常加载运行"） | ✅ 方向 B 与 mutation 并存运行 | reality 39 + 06-28 会话（mqxire67e7uw9g）"双路径同时活着的后果" |
| 07-26~07-27 | CA hooks 正常全链路（`[CA_v5] simple_mutation: replaced=0` 实证 v5 mutation 在生产跑） | ✅ hooks + mutation 运行 | `agent.log.1` L935-977 |
| **07-30 17:39** | sysadmin errors.log **首条** `Failed to load plugin 'ca_assembler'`（2 参 TypeError）——此后每日 2-23 条 | ⚠️ CE 壳注册失败 WARNING；**hooks 仍注册**（22af80bcf 前 Hermes 不 dispose） | `errors.log.1` 首条 |
| **08-12 07:53** | 会话 `mspbenwzz2s4i7`：CA hooks **完整运行**（E-stage 写 seq 0 + recall 注入 1840 chars + grade + F-stage），turn_stream 6 turns 写入 | ✅ **最后正常活动** | agent.log + `ca_cache/mspbenwzz2s4i7.db`（08-12 07:53~08:10 双源） |
| **08-13 20:59** | Hermes 部署更新（`plugins.py` mtime 20:59:29）→ **22af80bcf（08-01）dispose 逻辑生效**：register() 抛异常 → `_dispose_registrations(owned)` 回滚全部 8 hooks | ❌ 插件整体加载失败（首次真正停摆） | mtime + errors.log 20:57:59/20:58:04/20:59:43/21:00:26/21:09:44/21:10:05 |
| **08-13 21:52** | DSH 会话修复：注释 CE 壳注册 + 三副本同步（tester/sysadmin/winker md5 一致）→ gateway 重启（pid 8218） | ✅ 恢复：21:15 后 0 失败；08-14 0 失败 | errors.log 计数 + gateway-exit-diag.log pid 659→8218 |

**三层停摆的区分（这是本报告最重要的修正）**：

1. **CE 壳（方向 B 压缩路径，`_build_conv_history_v6` 唯一生产调用点 `__init__.py:352`）**：**06-27 引入即断**——2 参注册 vs 1 参签名必抛 TypeError，引擎从未进入 `_manager._context_engine`。`should_compress()`/`compress()` 从未被 Hermes 触发。
2. **hooks 全链路（E-stage/F-stage/话题/recall）**：**从未停摆**——07-26~08-13 20:59 持续运行（07-26 `simple_mutation` + 08-12 `mspbenwzz2s4i7` 实证）。07-30 起的 `Failed to load plugin` 仅是 CE 壳注册失败告警，Hermes 22af80bcf 之前不 dispose hooks。
3. **插件整体加载**：**08-13 20:57~21:10 瞬时失败**——Hermes 22af80bcf（08-01 提交，08-13 20:59 部署生效）在 register() 异常路径加入 `_dispose_registrations(owned)`（`plugins.py:4345-4361`），把原本"无害"的 2 参错误升级为致命故障。21:52 修复（注释 CE 壳注册）即恢复。

**附带发现（与 DSH 报告 §1.5 结论的差异）**：

- DSH 报告第 3 条说"agent.log 中 CA 最后成功启动是 08-12 07:53，此后无任何 CA 活动"——**属实但被误读**：08-12 07:53 是最后正常活动，但"此后无活动"的 08-13 白天 sysadmin 无新会话（非停摆），20:59 升级后才真正失败。
- DSH 报告第 4 条"CA 加载失败、hooks 全部未注册"**只对 20:57~21:10 的窗口成立**，不能回推为"CA 停摆已久"。
- 本修正基于 4 源交叉验证：git 提交历史、errors.log/agent.log/gateway.log 时间线、ca_cache DB（`mspbenwzz2s4i7.db` 08-12 写入实证）、reality-strand（reality 39/106 记录）。

---

## 2. DeepSeek Harness 现状（目标平台）

### 2.1 平台形态

- DSH = DeepSeek Harness，**TypeScript/Node 的 Cordis 插件框架**；`dsh` CLI 启动 profile（`web`/`headless`/自定义），profile = 插件 bundle 补丁栈（`dsh-base` + `dsh-web-app` + 用户 `cordis.patch.yml`）
- 版本 **0.1.0-rc.6**（早期 RC）
- 插件即 npm 包（声明 `dsh.bundle` 的加入补丁栈），经 `dsh plugin --profile <name> <pnpm args>` 安装；自定义插件 = TS/JS Cordis 插件
- 当前部署：`~/.dsh/profiles/web`，标准 bundle，无用户补丁；`$DSH_HOME/settings.yaml` 提供热重载用户设置

### 2.2 原生上下文/记忆/压缩能力清单（与 CA 逐项对照）

| 能力 | DSH 原生 | 说明 |
|------|----------|------|
| 会话事件存储 | ✅ `dsh-session` | **事件溯源**的 append-only session log；`surface` 层 = 消息产生事件的顺序投影；`deriveMessages()` 派生 LLM 消息历史（≈ Hermes state.db + CA turn_stream 的合体，且更干净） |
| 持久化 | ✅ `dsh-session-persistence-jsonl` | 每会话 JSONL（默认 zstd），原生落盘（≈ CA E-stage 写即落盘的等价物——DSH 已替 CA 干掉了"自己写 turn_stream"这件事） |
| 会话全文检索 | ✅ `dsh-session-query-sqlite` | SQLite FTS5 跨会话/会话内全文检索（web profile 默认 `openAt: never` 未启用；**无向量/语义检索**） |
| 跨会话引用 | ✅ `dsh-session-reference` | 跨会话只读快照注入（`dsh-session:<base64url>` URI、@label 提及，maxReferences=3） |
| 压缩 | ✅ `dsh-compaction` + `dsh-compaction-basic` | 服务缝设计（Service Definition / Provider 分离）：`ctx.compaction`，tokenMeter 压力触发（thresholdRatio 0.8 / retainRatio 0.16），LLM 一次性摘要替换旧区间（`<compacted-summary>`），压力检查挂 `agent/pre-step` + 溢出恢复挂 `agent/request-error`，摘要调用带 `purpose: 'compaction'` 标记；`ctx.toolResultPruner` 工具结果裁剪，`/compact` 命令（`dsh-command-compact`） |
| 工具结果溢出 | ✅ `dsh-spill-policy` + `dsh-output-retention` | 超 inline 预算的工具结果全量落 `ctx.spillStore`，模型只见 head/tail 预览 |
| 非会话 KV 存储 | ✅ `dsh-storage` + `dsh-storage-domain` | 域 KV（`domain/changed` 事件、写串行化；原生后端为 JSON 整文件原子替换）——可承载 CA 的 graph.json 等派生产物 |
| 上下文注入扩展点 | ✅ `agent/pre-step` | 每个 step 进入前的事件（waterfall）；listener 可向批次**追加/替换带 source 的 durable 消息**（`dsh-time-context`/`dsh-agent-instructions`/`dsh-goal-round-driver` 均为同款范例） |
| 系统提示词扩展 | ✅ `system-prompt/assemble` | 改写系统 prompt 的 sections/contexts/tools/variables（waterfall） |
| 请求改写扩展点 | ✅ `agent/request` | 只改 provider/model 等调用配置；**不能改消息**（消息改写走 pre-step / compaction surface replace） |
| 会话只读模型 | ✅ `dsh-session-projection` | `ctx.sessionProjections`：纯函数 fold 注册表，`session/event` 驱动、`snapshot()` 一致快照 —— **正是 CA 派生数据（Fct/Hdl/topic/reality）的理想宿主** |
| 技能/知识 | ✅ `dsh-skill` + `dsh-skill-filesystem` | 技能注册表 + 本地文件后端（AGENTS.md 注入由 `dsh-agent-instructions` 负责） |
| 长程目标 | ✅ `dsh-goal` + `dsh-goal-round-driver` | 事件溯源目标状态 + 回合驱动 |
| 待办/任务 | ✅ `dsh-tool-todo` | 结构化待办 |
| 时间上下文 | ✅ `dsh-time-context` | 时区/耗时注入（pre-step 模式范例） |
| 工作区指令 | ✅ `dsh-agent-instructions` | AGENTS.md 链注入（durable user/message，`<system-reminder>` 框） |
| **话题检测/定级** | ❌ 无 | |
| **逐轮结构化摘要（Fct/OODA）** | ❌ 无（compaction 是整段一次摘要，无 Elm/Fct/Hdl 分层；唯一辅助 LLM 摘要另有会话标题 `purpose: 'session-title'`） | |
| **向量嵌入/语义检索/召回注入** | ❌ 无（无任何 embedding/vector/retrieval 包；`dsh-tool-fs-search` 是 ripgrep 文件搜索，`dsh-web-search-deepseek` 是 web 检索） | |
| **跨会话语义记忆/wiki** | ❌ 无（只有 FTS 全文检索 + skill 文件 + session-reference 快照） | |
| **知识图（graphify）** | ❌ 无 | |

### 2.3 Agent 循环扩展点（与 CA hook 映射）

DSH agent 循环（`dsh-agent-loop`）把所有"模型调用、跑工具、重复"之外的事交给**事件分类法**上的插件：

```
agent/created, agent/disposed, agent/session-start, agent/status, agent/error
agent/inbox/claimed, agent/inbox/spliced, agent/inbox/inserted, agent/inbox/discarded
agent/pre-step        ← 每 step 进入前（waterfall：可拒绝或返回/追加进入该步的完整消息批次）
system-prompt/assemble ← 改写系统 prompt 的 sections/contexts/tools/variables（waterfall）
agent/request         ← provider/model/effort/maxTokens 解析（waterfall；只能改调用配置，不能改消息）
agent/request-error   ← 溢出/错误恢复（waterfall：返回 {kind:'retry'}；compaction 在这里做 overflow repair）
agent/turn-stopping   ← 回合终止钩子（serial）
llm/stream            ← 可拦截的 LLM 流调用（waterfall；请求深冻结只读）
llm/retry
session/event         ← 全部持久事件的观察流（assistant/message、assistant/chunk、tool/call、
                        tool/result、user/message、turn/start、step/start、step/end、turn/end、request/header）
session/flush, session/created, session/disposed
tools/pre-execute → tools/execute → tools/post-execute → finalizeContent → tools/result
                      ← 工具生命周期管线（pre-execute 故意不支持输入改写；post-execute 可改结果/附加上下文）
```

---

## 3. 迁移可行性分析（核心章节）

### 3.1 CA → DSH 接口映射表

| CA（Hermes 侧） | 依赖的 Hermes API | DSH 对应扩展点 | 映射难度 |
|---|---|---|---|
| `ctx.register_hook("on_session_start/end/reset", ...)` | `hermes_cli.plugins.py:2770` `register_hook` | `agent/session-start`、`agent/disposed`、`session/created`、`turn/end` | 🟢 低 |
| `ctx.register_hook("pre_llm_call", ...)`（返回值注入 user message） | `agent/turn_context.py:1152-1204` hook 分发 + 上下文注入 | `agent/pre-step` listener 追加 sourced `user/message`（`dsh-time-context`/`dsh-agent-instructions` 同款模式） | 🟢 低 |
| `ctx.register_hook("post_llm_call", ...)`（写 fin + 触发 F-stage） | `agent/turn_finalizer.py:577-596` | 订阅 `session/event` 的 `assistant/message`（或 `step/end`）事件 → 异步处理 | 🟢 低 |
| `ctx.register_hook("post_api_request", ...)`（thought/tool 占位） | `agent/conversation_loop.py:6143-6150` | 订阅 `session/event` 的 `assistant/chunk`/`request/header` 事件 | 🟡 中 |
| `ctx.register_hook("pre_tool_call"/"post_tool_call", ...)` | tool 管线 | durable `tool/call` + `tool/result` 事件（含 name/arguments/result/meta）；`tools/post-execute` 可改结果内容 | 🟢 低 |
| `ctx.register_context_engine("ca_assembler", _ce_engine)` + `should_compress`/`compress` | `hermes_cli/plugins.py:1898`（1 参签名，CA 传 2 参 → TypeError；**AGENTS.md 称"无害"已过时，08-01 起回滚全部 hooks**，见 §1.5）；`agent/conversation_loop.py:1299 _apply_context_engine_selection` | 实现 `CompactionEngine` 子类注册为 `ctx.compaction`（Service Definition 明确支持第三方 backend：`dsh-compaction/README.md` "Implementing a backend"） | 🟡 中 |
| `select_context()`（按需） | `agent/context_engine.py` ABC | `agent/pre-step`（注入）/ `system-prompt/assemble`（改写系统 prompt 与工具 schema）。注意：`agent/request` **只能改 provider/model 等调用配置，不能改消息**（消息改写只能走 pre-step 批次 / compaction surface replace） | 🟡 中 |
| E-stage 自建 SQLite `turn_stream` | —（CA 私有） | **可整体删除**：DSH 会话日志已逐事件落盘（`session/event` + JSONL zstd）；CA 消费 `session/event` 即可 | 🟢 低（反而是减负） |
| F-stage 异步 LLM 摘要（daemon 线程） | `urllib → Ollama /api/generate`（**不依赖 Hermes**） | Node 异步事件处理 + `ctx.llm.stream()`（或继续 HTTP 调 Ollama 4B）；DSH 有 `dsh-jobs-local` 可托管后台任务 | 🟢 低 |
| 话题检测/定级（`topic_manager.py`） | 仅用 CA 自有 store/embedding | 纯算法 + `ctx.sessionProjections`（或自建 sidecar DB） | 🟢 低（纯移植） |
| A-stage `_build_conv_history_v6`（Tail/ACT/REL/FAR 三区降级重建） | —（CA 私有，从 turn_stream 读） | 两条路：① 自定义 `CompactionEngine` backend（`compactRegion`/surface `replace` 语义天然契合"REL/FAR 降级替换"，compaction-basic 即官方版"LLM 摘要 + surface replace"先例）；② `agent/pre-step` 批次注入/`system-prompt/assemble` 改写。**`agent/request` 不能改消息** | 🟡 中 |
| wiki/reality 召回注入（`pick_injection_realities`，提问云形心 + 4B 拣选） | pre_llm_call 返回值字符串注入 | `agent/pre-step` 追加 sourced user/message（可做成 durable，比 CA 现在的临时注入更强） | 🟢 低 |
| 跨会话记忆库（strand/reality 表） | —（CA 私有 SQLite） | 自建 sidecar SQLite（Node `node:sqlite` 可用）或 `ctx.sessionProjections` 派生 | 🟡 中 |
| graphify 知识图 | —（CA 私有，graph.json） | 自建（MCP 服务可继续复用，DSH 有 MCP client） | 🟢 低 |
| L4 空闲精炼守护线程 | threading | `dsh-schedule` / `dsh-jobs-local` 或普通定时任务 | 🟢 低 |

### 3.2 数据模型映射

| CA 数据 | 结构 | DSH 对应 | 备注 |
|---------|------|----------|------|
| `turn_stream`（session_id, turn, seq, role, Elm, tool_*, Fct, Hdl…） | SQLite | **会话事件日志**（`user/message`、`assistant/message`、`tool/call`、`tool/result`、`step/*`、`turn/*`） | 事件日志即 turn_stream 的超集；CA 的 `seq` 语义 ≈ 会话事件 seq |
| `strand_summaries`（hdl/ooda/changes/key_facts/centroid） | SQLite | `ctx.sessionProjections` 派生单元 或 sidecar SQLite | 派生数据天然适合 projection 注册表（纯函数 fold、`stateVersion` 失效锚） |
| `realities`（reality_id, name, current_status, timeline, centroid, question cloud） | SQLite | 同上（projection 或 sidecar） | 4B 归并（`run_reality_merge`）是异步 LLM 工作，投影层只能存结果 |
| `graphify-out/graph.json` | JSON | 自建/MCP 复用 | |
| 断路器状态（`.ca_assembler_state_{pid}.json`） | JSON | 事件日志/进程内 | DSH 事件日志天然抗崩溃 |

### 3.3 CA 对 Hermes 的依赖面（子代理实证 + 人工复核）

**运行时依赖极薄**——CA 只用到 4 个 Hermes 符号，且全部带 ImportError 降级：

| Hermes 符号 | 用途 | 降级 |
|-------------|------|------|
| `hermes_constants.get_hermes_home()` | 定位 ca_cache 目录 | `~/.hermes` 兜底 |
| `agent.context_engine.ContextEngine` | CE 壳基类 | `object`（duck-typing） |
| `tools.skill_provenance.get_current_write_origin()` | bg_review 检测（跳过 A-stage） | `"unknown"` |
| `get_model_context_length`（经 Config） | 模型上下文长度 | 常量兜底 |

**两处结构性耦合（迁移需处理）**：
1. `post_api_request` 的 `assistant_message` 是 Hermes `NormalizedResponse` **对象**（`agent/transports/types.py:90` dataclass：content/tool_calls[ToolCall]/finish_reason/reasoning/usage/provider_data；ToolCall 在 types.py:18），CA 用 `getattr(assistant_message, "provider_data"/"content"/"tool_calls")` 取字段（`ca/e_stage.py:53-71`）——换宿主需改成 DSH 事件负载（`assistant/message` 的 data）
2. `a_stage.py` 假设 OpenAI 风格消息 dict（`{role, content, tool_calls}`；`__init__.py:996` 按 `role=="user"` 计数重算 turn；`a_stage.py:204-231 _row_to_message` 产出同构结构）——DSH 的派生消息也是该形态，适配成本低

**注册面极窄 + 配置/异步完全独立**：
- `PluginContext` 共约 40 个方法，**CA 只用 2 个**：`register_hook`（8 次）+ `register_context_engine`（1 次，参数错误）；其余（get_config/state/llm/spawn_task/inject_message/register_tool/emit/subscribe/on_unload…）均未用
- 配置**不读 Hermes config.yaml**（只被 `plugins.enabled` 门控）：全走 CA_* 环境变量 + 自带 `ca/settings.yaml`；plugin.yaml 的 config_schema 注释明言"未接入消费"
- 异步**不用 ctx.spawn_task / asyncio**：全部自带 threading 守护线程（SessionManager 清理循环、F-stage 线程、话题摘要线程、会话启动清理线程、IdleRefinementDaemon）；与 Hermes 主循环交互仅经同步 hook 回调
- 运行时**不写 state.db**（grep 证实；`scripts/` 下的 reprocess/migrate 离线脚本直读 state.db 属独立批处理，不经插件加载）
- 例外耦合：`tools.skill_provenance.get_current_write_origin`（bg_review 轮跳过逻辑，Hermes 内部 ContextVar）——换宿主需等价机制或移除

**隐性契约（换宿主必须重建，代码里看不到）**：
- `on_session_end` 在 Hermes 每轮触发而非真会话结束（CA 因此把清理移到 atexit/gateway expiry）
- `pre_llm_call` 返回值 = 注入 user message 的字符串（DSH 对应 pre-step 追加 sourced message）
- turn 计数法 = `len([m for m in conversation_history if m["role"]=="user"])`（DSH 用事件日志的 turn 语义）

### 3.4 语言与依赖迁移评估

- **纯算法层（可直译）**：Jaccard 话题检测、BM25、RRF 融合、token 估算、OODA 四段解析、三区降级规则、优先级裁剪渲染 —— 与宿主无关，TS 直译即可
- **LLM prompt 语义层（需重写，工作量主体）**：F-stage 摘要 prompt、topic summarizer（4B 提炼）、reality 归并/拣选 prompt、压缩指令 —— prompt 文本可搬运，但调用栈要从 `urllib→Ollama` 换成 `ctx.llm.stream()` 或保留 HTTP（**CA 的 LLM/embedding 调用与 Hermes 解耦，这是最大利好**）
- **管道层（需再造）**：8 hooks → DSH 事件监听；daemon 线程 → 事件驱动异步；SessionManager TTL → DSH 会话生命周期（DSH 会话由 `ctx.sessions` 管理，无需 CA 自管 LRU/TTL）
- **独立性量化（子代理估算）**：store/cache/embedding/retrieval/reality/theme/refinement/topic_summary 等 20 个核心模块 ≈95% 自包含可原样搬；需重写的仅是 `__init__.py` 适配壳（~600 行）+ 上述两处结构耦合
- **外部依赖**：numpy（→ 无，BM25/向量可手写或用轻量库）、sentence-transformers（→ Ollama embedding HTTP，已在用）、urllib3（→ fetch）、pyyaml（→ 原生 YAML 已有）

### 3.5 不可直接迁移/需决策的点

1. **语言栈**：CA 是 Python，DSH 插件必须是 TS/JS —— 无法"复制粘贴"，是**重写**（但算法可直译，prompt 可搬运）
2. **宿主差异**：Hermes hook 传 `conversation_history` 完整消息列表；DSH 事件驱动，需自行从 session 派生所需视图（`deriveMessages()`/`sessionProjections` 可补）
3. **compaction 并置问题**：DSH 默认跑 `dsh-compaction-basic`（auto 压缩）。若 CA 要实现"替代 compressor"的定位，需**替换** compaction backend 或**并存**（CA 管语义降级、DSH 管 token 压力兜底）——需设计决策
4. **`<compacted-summary>` 与 CA 分层摘要的关系**：DSH 压缩产出单一摘要 checkpoint；CA 是 Elm/Fct/Hdl 多级结构。要让模型看到 CA 的分层降级，需在 `agent/pre-step`/`system-prompt/assemble` 或自定义 compaction backend 里做，而不是默认 backend
5. **版本成熟度**：DSH 0.1.0-rc.6，早期 RC；`agent/pre-step` 等 API 仍在演进，迁移需跟随上游变更
6. **注入的持久性差异**：DSH 官方注入模式 = 追加**带 source 的 durable user/message**（进会话日志，可被 compaction 遮蔽）；CA 现有 recall 注入是 pre_llm_call 返回的临时字符串（不进对话记录）。迁移后可升级为 durable 注入，语义更强，但要注意别污染会话历史统计

---

## 4. 必要性分析

### 4.1 迁移的前提问题

"是否有必要"取决于一个前提：**DSH 是否将取代 Hermes 成为主 Agent 平台？**

- 当前事实：Hermes（gateway + web-ui :8648）是活跃主平台；DSH（web :3080）与 Hermes 并存。二者不是竞争关系，是两个独立 Agent 栈。
- **（修订）原"新增紧迫事实"已失效**：调研当时 CA 在 Hermes 上停摆（插件 API 变更导致加载失败，见 §1.5），**08-14 已 1 行修复恢复**（§1.5b）。因此当下问题是"**恢复后的 CA 如何规划**"而非"修复还是迁移"：
  - 若答案是"是，逐步迁到 DSH" → CA 的能力需要以某种形态在 DSH 上重现，否则迁移后丢失上下文/记忆能力 → **必要（且现在就是窗口期）**
  - 若答案是"否，Hermes 继续主用，DSH 是另一环境" → **CA 已修复恢复**（08-14），继续维护即可；迁移属于双份维护，**不必要**（除非想借机把 CA 重构到事件溯源模型上）
  - 折中：维持 CA 于 Hermes 生产，同时以 DSH 为实验环境按 §5.2 渐进验证迁移路径，双向对冲

### 4.2 DSH 原生与 CA 的差距 = CA 迁移后的存在价值

DSH 原生已经覆盖"**控制上下文体积**"（compaction + tokenMeter + tool-result pruner），但它**不覆盖 CA 的"结构化记忆"**：

| CA 独有能力 | DSH 原生替代 | 差距 |
|-------------|--------------|------|
| 逐轮结构化摘要（Fct/OODA 四段） | 无（compaction 是整段一次摘要） | 细粒度可追溯记忆 |
| 话题检测与 ACT/REL/FAR 定级 | 无 | 按话题降级 vs 按时间窗口截断 |
| 跨会话语义召回注入（wiki/reality） | 仅 FTS 全文检索 + skill 文件 | 语义级"记得上次做什么" |
| reality 归并（工作线追踪） | goal/todo（回合目标/待办，非语义记忆） | 长期工作线连续性 |
| 知识图 | 无 | 关系可视化/检索 |

→ **若迁移，DSH 版 CA 的定位应明确为"记忆层"而非"压缩器"**（压缩交给 DSH 原生 compaction 或由 CA backend 接管）。

### 4.3 迁移的动机矩阵

| 动机 | 强度 | 说明 |
|------|------|------|
| 平台统一（DSH 为主平台） | 强 | 单栈运维、事件溯源会话模型比 Hermes state.db 更利于 CA 类派生数据 |
| **CA 当前在 Hermes 停摆（08-13 实测）** | 强 | 插件 API 变更已致加载失败；若 Hermes 升级节奏持续，适配壳需持续跟进——这是迁移的现成窗口与动机 |
| 事件溯源数据模型红利 | 中 | DSH 会话日志 = 天然 turn_stream，CA 不再自建写入管道；`sessionProjections` 提供一致性快照 |
| 服务缝设计红利 | 中 | `ctx.compaction` 明确支持第三方 backend，CA 的压缩哲学可落为正规 backend |
| 注入机制更强 | 中 | pre-step 注入可 durable（进会话日志），CA 现在的 recall 是临时字符串 |
| 技术栈统一 TS | 中 | 若团队/工具链向 JS 生态靠拢 |
| 反动机：成熟度 | 强 | DSH 0.1.0-rc.6 早期版本，CA 的语义层（v7）在 Hermes 上已有 141+ 决策节点支撑；迁移有平台演进风险 |
| 反动机：工作量 | 强 | ~1.7 万行重写 + 1.7 万行测试移植；CA 的设计决策树需在 DSH 语义下重新验证 |
| 反动机：双栈并存 | 中 | 迁移期间 CA 数据（ca_cache 139 个会话库 + realities）与 DSH 会话数据隔离，历史记忆不互通 |

---

## 5. 结论与建议

### 5.1 结论

1. **能够迁移（可行）**：CA 的每个机制在 DSH 都有对应扩展点或等价宿主（§3.1 映射表全绿/黄，无红）；CA 的 LLM/嵌入调用本就不依赖 Hermes，算法层可直译（核心管道 ~95% 自包含）。
2. **是否必要是前提选择题，且叠加紧迫性**：DSH 若转正为主平台则必要；否则不必要（已 1 行修复恢复 Hermes 生产，08-13 20:57~21:10 的瞬时加载失败已解决，见 §1.5b）。不能按"CA 停摆"来规划——CA hooks 一直正常，仅 CE 壳（方向 B）自 v6.0 未注册。
3. **迁移本质是"重写"，不是"搬运"**：管道层（hooks/线程/SQLite 自管）被 DSH 事件模型取代（反而减负），语义层（prompt 工程 + 算法）是主要工作量；CA 与 Hermes 的契约面薄（4 个可降级符号 + 弱类型 kwargs），但 hook 时序语义（on_session_end 每轮触发、pre_llm_call 注入语义、turn 计数法）是换宿主时必须重建的隐性契约。
4. **迁移后的 CA 应重新定位为"记忆层"**，与 DSH 原生 compaction 分工：DSH 管体积，CA 管结构。

### 5.2 建议路线（若决定迁移）

```
Phase 0  数据盘点：导出 ca_cache 全量 schema/数据 + reality 库，评估历史记忆是否要迁移（建议：不迁，冷存档）
Phase 1  最小 CA（2-3 天量级）：
         - 订阅 session/event → 派生 CA 视图（turn/topic 数据结构）
         - agent/pre-step listener 实现 recall 注入（先做首轮 recall）
         - 验证注入链路（对照 dsh-time-context / dsh-agent-instructions 模式）
Phase 2  压缩后端（1-2 周量级）：
         - 实现 CompactionEngine 子类（三区降级重建 conv_history 作为自定义 backend）
         - 决策：替换 or 并置 dsh-compaction-basic
Phase 3  语义记忆层（主要工作量，数周）：
         - F-stage 异步摘要（ctx.llm 或 Ollama HTTP）
         - 话题检测/定级（直译 topic_manager）
         - reality 归并 + 提问云形心召回（prompt 重写为 DSH 调用栈）
         - 可选：ctx.sessionProjections 承载派生数据 / graphify 复用
Phase 4  对等测试：以 sysadmin 历史会话为语料，对比 Hermes 版与 DSH 版召回/降级质量
```

### 5.3 决策前置问题（供用户拍板）

1. **CA 当前在 Hermes 上停摆（08-13 实测）——先决定"修复还是迁移"**：若 Hermes 留任主平台，1 行修复（`__init__.py:191` 改 1 参）即可恢复；若 DSH 转正，则以此为迁移窗口。
2. DSH 是否定位为未来的主 Agent 平台？（决定必要性）
3. 若迁移，历史记忆（139 会话库 + realities + graph.json）是否要随迁？
4. CA 在 DSH 上是否继续维护"替代 compressor"定位，还是退居"记忆层"与原生 compaction 并存？
5. LLM/嵌入是否继续走本地 Ollama，还是切到 DSH 的 `ctx.llm`（deepseek 适配器）？

---

## 附录 A：关键文件索引

- CA 插件入口：`plugins/ca_assembler/__init__.py`（8 hooks + CE 壳 + 召回注入格式化）
- CA 引擎核心：`plugins/ca_assembler/ca/__init__.py`（SessionManager + ContextAssembler）
- CA A-stage：`plugins/ca_assembler/ca/a_stage.py`（`_build_conv_history_v6`）
- CA 存储：`plugins/ca_assembler/ca/store.py`（turn_stream/strand/realities schema）
- CA 文档：`plugins/ca_assembler/docs/INDEX.md`、`docs/architecture/01-overview/01-overview.md`、`docs/decisions/20-ce-shell-registration/`
- Hermes hook 分发：`~/.hermes/hermes-agent/agent/turn_context.py:1152`（pre_llm_call）、`agent/turn_finalizer.py:577`（post_llm_call）、`agent/conversation_loop.py:6143`（post_api_request）、`agent/context_engine.py:89`（ABC）、`hermes_cli/plugins.py:1898`（register_context_engine，注意签名 1 参）
- Hermes 内置压缩器：`~/.hermes/hermes-agent/agent/context_compressor.py:1577`（ContextCompressor）
- DSH agent 循环：`dsh-agent-loop/README.md`（事件分类法：agent/*、tools/*、session/event、llm/stream；`agent/pre-step` 为 waterfall）
- DSH 压缩服务：`dsh-compaction/README.md`（Service Definition + backend 实现指南）、`dsh-compaction-basic/README.md`
- DSH 注入范例：`dsh-time-context/README.md`、`dsh-agent-instructions/README.md`、`dsh-goal-round-driver/README.md`、`dsh-session-reference/README.md`
- DSH 只读模型：`dsh-session-projection/README.md`；工具管线：`dsh-tools/lib/types/index.d.ts`（pre/execute/post-execute/result）

## 附录 B：调研记录与未确认项

- DSH 全仓检索未发现 embedding/vector/BM25/RRF/topic/semantic-memory 相关包——结论可信（覆盖 `node_modules/@deepseek-ai/` 全部包名与 README）
- `dsh-session` 类型目录含 `hook/invoked`、`hook/result` 事件类型但无任何包实现，疑似生成产物预留（未确认）
- `agent/session-start` 的 `clear`/`compact` source 在 README 标注预留未发射（TODO(compaction)）
- `dsh-storage` README 提及 sqlite 后端可并存，但本安装无 `dsh-storage-sqlite` 包（未发货）
- **CA 停摆实证**（2026-08-13）：`hermes_cli/plugins.py` mtime 20:59:29 与首批加载失败（20:59:43）同一分钟；回滚逻辑源自 commit `22af80bcf`（2026-08-01 "feat(plugins): add ownership ledger unload lifecycle"）；agent.log 最后 CA 启动 08-12 07:53；errors.log 08-13 20:59-21:10 连续报 `PluginContext.register_context_engine() takes 2 positional arguments but 3 were given`
- **AGENTS.md 过时点**：声称"register_context_engine 参数警告为无害已知项、hooks 不被回滚"——该结论在 commit `22af80bcf`（08-01）及 08-13 工作树更新后失效；AGENTS.md 中"should_compress 固定 False"与代码（`__init__.py:294-308` 无条件 True）不符、"→ state.db" docstring 与方向 B 实际路径不符（均属文档-代码漂移，报告不展开）
- **CE 壳不可达（三重证据，见 §1.5 附注）**：TypeError 致引擎从未注册 + tester/winker 的 `context.engine: compressor` 不选中 + `should_compress` 恒 True 若生效会每轮 compress()——CA 实际生效路径仅为 8 个 hooks
- **CA 运行活动核查**：sysadmin 最后 CA 启动 08-12 07:53（session mspbenwzz2s4i7）；tester 最后活动 08-12 08:52-08:55（session mspdj4vub862jr）；08-13 gateway 重启后两 profile 均无 CA 活动，errors.log 连续加载失败
- Hermes 侧 hook 语义（返回值注入、CE 选择路径、失败回滚）基于 `~/.hermes/hermes-agent` 源码（hermes_agent 0.20.0 editable install）逐一核实

---

## 6. 设计符合性审计（2026-08-13 修补核对 + 代码 vs 设计差异）

> 本节回答两个问题：①本次"暂停 CE 壳注册"的修补是否符合 CA 设计文档；②代码与设计文档的现存差异清单。
> 证据来源：`docs/decisions/06/20/22/15/17/21`、`docs/decisions/decision-points/TP-009-means-vs-ends.md`、`docs/architecture/01-overview`、`docs/architecture/03-e-stage`、AGENTS.md 原记录 vs `__init__.py`/`ca/*.py` 代码。
> **闭环说明（2026-08-13 同日）**：本节 §6.2/§6.4 列出的文档差异，除"设计簇 A/B 分裂"（属 TP-009 与决策 06/20 的历史张力，需用户定夺）外，**均已按代码现实修订完毕**——architecture 01/05/08、decisions 06/08/09/15/20/22、`docs/INDEX.md`、根 `INDEX.md`、AGENTS.md 结构块与检查清单。修订方式：架构文档直接改正文；决策文档保留历史正文、追加"修订（2026-08-13 代码核对）"小节，不篡改决策记录。

### 6.1 修补符合性结论：✅ 符合"CE 停用"设计簇；未实现"CE 激活"设计簇（后者本就未落地）

设计文档对 CE 壳存在**两个互相矛盾的簇**：

| 设计簇 | 出处 | 主张 | 与修补的关系 |
|--------|------|------|--------------|
| **A：CE 停用/占位** | 决策 06（"CE 管线 2026-06-28 停用"）、决策 20 frontmatter（status "已实装（v6.0 停用）"）、overview 01（"CE 管线已停用"）、AGENTS.md 原记录（"CE 壳=占位"、"CA 不触发 Hermes compress_context 流程"） | CE 管线停用，CA 不触发 compress_context | **修补（暂停注册）符合本簇** ✅ |
| **B：CE 激活** | TP-009 方向二（2026-06-22）：CA 实现 ContextEngine ABC，compress() 获得消息列表完全操控权（删行/重组） | CE 应作为激活引擎运行；should_compress = "有旧话题且 token 超阈值"（条件式）；双模式守卫：CE 激活 → pre_llm_call 静默跳过 | 修补未实现本簇；**但 TP-009 的 Phase 1 复选框全部未勾选（"[ ] Phase 1: CA 实现 ContextEngine ABC"），即本簇从未按设计落地** ⚠️ |

**关键佐证（决策 06/20 与代码一致）**：代码中 CE 壳因注册签名错误**从未进入 Hermes 引擎槽**，`should_compress`/`compress()` 在生产中从未被调用——事实状态即"CE 管线停用"，与簇 A 一致。修补只是把"必然抛错的死注册"改为"不注册"，使代码与簇 A 的文档描述对齐，**且恢复了 08-12 前的生产实际状态**（hooks 生效 + 引擎回退内置 compressor）。

**为什么不实现簇 B（激活 CE）**——代码与簇 B 的设计前提有三处脱节，直接激活有风险：
1. **should_compress 立场不一致**：代码无条件 True（第三种立场）vs 簇 B "有旧话题且 token 超阈值" vs 簇 A "固定 False"；
2. **双模式守卫缺失**：TP-009 把"CE 激活 → pre_llm_call 跳过 assemble"列为最高风险项（"CE 引擎模式下 pre_llm_call 和 compress() 冲突 — 高"），代码中 pre_llm_call 无任何引擎模式检查（只有 bg_review 检测）；
3. **轮序隐患（本调研新发现）**：Hermes 前检压缩（`turn_context.py:841-1024`）运行在 pre_llm_call（`:1152` 写 seq 0）**之前**；若 CE 激活，compress() 从 turn_stream DB 重建历史时当前轮 seq 0 尚未写入 → 重建列表缺失当前用户消息 → 请求可能丢用户消息。TP-009 的双模式守卫设计针对"assemble 冲突"，并未覆盖该轮序问题。

### 6.2 代码 vs 设计差异清单

| # | 设计声明 | 出处 | 代码事实 | 差异等级 |
|---|---------|------|---------|---------|
| 1 | "A-stage 组装（_build_conv_history_v6）完全由 8 个 hooks 驱动" | AGENTS.md 原记录、overview 01 | `_build_conv_history_v6` **仅**由 CE 壳 `compress()` 调用（`__init__.py:352`）；8 个 hooks 只做 E-stage/F-stage/话题/recall，从不调用重建 | 🔴 高（A-stage 在生产从未运行过） |
| 2 | "should_compress 固定 False" | AGENTS.md 原记录 | 无条件 `return True`（`__init__.py:309`） | 🔴 高（文档-代码漂移；因引擎未注册而无生产影响） |
| 3 | should_compress 应为"有旧话题且 token 超阈值"（条件式） | TP-009 Phase 1 Step 1 | 无条件 True（第三种立场，与两簇都不符） | 🔴 高（若激活 CE 壳会每轮触发 compress()） |
| 4 | 决策 22：注册 `on_session_finalize` 清理 state.db 相邻重复 user 行 | decisions/22（status 已实装） | 代码无 `on_session_finalize` 注册（register() 只有 8 hooks） | 🟡 中（决策声称已实装但代码无此 hook） |
| 5 | 决策 15：SHA256 指纹去重嵌入调用（ca/cache.py） | decisions/15（status 已实装） | cache.py 无 sha256/fingerprint 相关实现 | 🟡 中（v4.1 机制已不存在） |
| 6 | TP-009 双模式守卫：CE 激活 → pre_llm_call 静默跳过 assemble | TP-009 Phase 1 Step 2 | pre_llm_call 无引擎模式检查（仅 bg_review 检测） | 🔴 高（TP-009 自评最高风险项未实现） |
| 7 | should_compress docstring 描述 v5 行为（"FAR 行删除/原地删行/_full_backup 恢复→state.db"） | `__init__.py:294-319` | 与 v6 方向 B 不符（`__init__.py:921-922` 注释已声明 `_full_backup` 废弃） | 🟡 中（过时 docstring） |
| 8 | ca/__init__.py:12 注释 "lstage.py — L-stage 生命周期 + 补全线程" | ca/__init__.py | lstage.py 仅 LStageMixin（reset/wait_for_pending），补全线程已由 F-stage 取代（决策 17） | 🟢 低（注释过时） |
| 9 | 决策 20 status "已实装（v6.0 停用）" | decisions/20 | 代码 CE 壳定义仍在但从未注册成功 → 事实停用 | ✅ 一致（佐证修补方向） |
| 10 | 决策 06 "CE 管线 2026-06-28 停用" | decisions/06 | compress()/CE 壳代码存在但引擎槽为空 → 管线事实性停用 | ✅ 一致（佐证修补方向） |
| 11 | E-stage "5 Hook 写即落盘协议" | architecture 03 | pre_llm_call(0)/post_api_request(1)/pre_tool_call(no-op)/post_tool_call(m)/post_llm_call(N) 与代码一致 | ✅ 一致 |
| 12 | 决策 21 引擎 TTL 恢复（CR-009） | decisions/21 | `_refresh_engine` 已实现（`__init__.py:950-976`） | ✅ 一致 |
| 13 | 决策 16 预算闸门 "a-stage (legacy)" | INDEX.md | a_stage.py 无 budget 逻辑（v6 移除） | ✅ 一致（文档已标 legacy） |
| 14 | "不写 state.db" | AGENTS.md | 运行时路径无 SessionDB/replace/append（scripts/ 离线脚本例外，非插件路径） | ✅ 一致 |
| 15 | plugin.yaml config_schema | plugin.yaml 注释自述"未接入消费" | config.py 用 CA_* env + ca/settings.yaml | ✅ 一致（已有注释声明） |

### 6.3 审计小结

- **修补符合设计**：选择的是文档中"CE 停用"簇（决策 06/20 + AGENTS.md + overview），并恢复生产实际状态；未实现"CE 激活"簇（TP-009），而该簇本身从未落地（复选框全空）且激活前提（条件式 should_compress、双模式守卫、轮序修复）在代码中均缺失。
- **最重要的代码-设计差异**：#1/#2/#3/#6 —— 设计文档对 A-stage/CE 壳的描述（hooks 驱动、should_compress False）与代码（compress 驱动、should_compress True）系统性错位，且 TP-009 的高风险守卫未实现。**任何未来激活 CE 壳的动作，必须先补：①条件式 should_compress；②pre_llm_call 模式守卫；③前检压缩与 seq 0 写入的轮序处理**，并跑通 `tests/unit/test_ce_shell.py` E2E。
- **决策 22（on_session_finalize）与决策 15（指纹去重）标记"已实装"但代码缺失**——建议在 OV 决策树补注"已撤销/未落地"，避免后人误信。

### 6.4 架构文档（docs/architecture/）全量核对（2026-08-13 补查）

> 上一轮 §6.1-6.3 以决策文档为主；本节把 13 篇架构文档全部与代码交叉核对。

**一致（11/13）**：

| 文档 | 关键声明 | 代码核实 |
|------|---------|---------|
| 02-store | turn_stream 18 列、WAL、行不可变、v7 reality 表族 | ✅ `store.py`（`PRAGMA journal_mode=WAL` :57/:406；表结构一致） |
| 03-e-stage | 5 Hook 写即落盘协议 | ✅ `e_stage.py` 与 `__init__.py` hook 转发一致 |
| 04-f-stage | fin 粒度 daemon、MEANINGLESS_CORE 清洗、OODA→JSON→Regex 降级 | ✅ `f_stage.py:122/136/165` 一致 |
| 06-topic-management | detect/grade、Jaccard 归一化（`_extract_fct_semantic_text`/`_FCT_METADATA_KEYS`/阈值 0.04·0.08） | ✅ `topic_manager.py:80-85/492-498` 一致 |
| 07-retrieval | 自标**死代码**（无生产调用方） | ✅ 核实无生产调用（仅 `cache.py:223` 定义 `get_bm25_snapshot` 无消费方）——文档诚实 |
| 09-tool-summarizer | 11 个 `_summarize_*` handler、三段回退链 | ✅ 恰 11 个 handler（`tool_summarizer.py`） |
| 10-embedding | Ollama / sentence-transformers / fallback 三后端、余弦自实现 | ✅ `embedding.py:53/139-141/212` 一致 |
| 11-l-stage | LStageMixin 仅生命周期（reset/wait_for_pending）；L4 在 `refinement.py` | ✅ `lstage.py` 仅 mixin；`refinement.py:415 IdleRefinementDaemon` |
| 14-idle-refinement | 精炼管线任务表、宁并不分、REFINEMENT_* 配置 | ✅ `config.py` 40 处 `REFINEMENT_*`；refinement.py 实现齐全 |
| 12-config | CA_* env > settings.yaml > 内建默认 | ✅ `config.py` 一致 |
| 01-overview | 8 hooks 数据流、三阶段、CE 壳定位 | ⚠️ 见下方差异 a（"CE 管线停用"与 05 文档"已实装"的表述张力） |

**差异（2 处，新增发现）**：

| # | 文档声明 | 代码事实 | 等级 |
|---|---------|---------|------|
| a | 08-cache："AssemblyCache 未接入主链路、从不触碰 self.cache" + "指纹去重：SHA256 指纹避免重复嵌入" | `__init__.py:475` 把 `engine.cache`（AssemblyCache 实例，`ca/__init__.py:172-173`）传给 `TopicGradeManager`，后者读 `self._cache.semantic_fct_embeddings`（`topic_manager.py:628`，cache.py:133/161 定义并填充）——**AssemblyCache 部分接入**（仅 `semantic_fct_embeddings` 活，四字典其余死）；且 **cache.py 无任何 sha256/fingerprint 实现**（去重实际靠 dict key） | 🟡 中（文档"全死"与"SHA256"两处不精确） |
| b | 05-a-stage status"已实装" | 方向 B 代码与测试存在，但**生产从未运行**（A-stage 仅经 CE 壳 compress() 可达，引擎从未注册，见 §6.2 差异 #1）——"已实装"未区分"代码落地"与"生产上线" | 🟡 中（文档状态语义不完整） |

**架构文档核对小结**：13 篇中 11 篇与代码一致、2 篇有中等级别差异（08-cache 的"死代码"与"SHA256"声明、05-a-stage 的"已实装"状态语义）；加上 §6.2 的 6 项决策层差异，CA 文档体系整体质量高，**最需要修正的是 08-cache 文档**（删除/修正"未接入主链路"与"SHA256 指纹"两处），以及统一"CE 壳/A-stage 是否激活"的表述（决策簇 A vs 簇 B 的分裂，见 §6.1）。
