# CA 移植到 Reasonix 可行性分析

> 日期：2026-06-06（第二版 — 修正分析）
> 背景：CA (ContextAssembler) 当前以 Hermes 插件（plugin）方式运行，通过 5 个生命周期 hooks 工作。
> 目标：评估移植到 Reasonix（Go 单二进制 + hook 插件 + cache-first 架构）的可行性和方案。

---

## 1. 核心架构差异

| 维度 | Hermes (当前) | Reasonix (目标) |
|---|---|---|
| 本质 | 编排器（User→编排器→LLM） | LLM 即 Agent，自主管理上下文 |
| 插件机制 | Python hooks（pre_llm_call / post_llm_call） | Shell hook 命令（JSON-RPC stdin/stdout） |
| Hook 点 | `on_session_start/end/reset`、`pre_llm_call`、`post_llm_call` | `SessionStart`、`SessionEnd`、`PreCompact`、`PostLLMCall`、`PreToolUse`、`PostToolUse` |
| **A-stage** | `pre_llm_call` 返回文本注入 user message | **`PreCompact`**（暂定）——接收 messages，输出替换文本 |
| **C-stage** | `post_llm_call` 接收完整 conversation_history | **`PostLLMCall`**——stdin 接收 JSON |
| 上下文管理 | CA 负责每轮分层构建 | Agent 自主管理 + 内置 compaction 机制 |

### 关键类比

```
Hermes CA：pre_llm_call (A-stage) + post_llm_call (C-stage)
                    ↓                     ↓
Reasonix CA：PreCompact hook (A-stage等价) + PostLLMCall hook (C-stage等价)
```

Reasonix 虽然没有 `pre_llm_call` 这个名字，但 **PreCompact hook 的输入输出契约（stdin 收 messages → stdout 出替换文本）恰好可以实现和 A-stage 相同的功能**。区别在于触发频率——Hermes 每轮触发，Reasonix 通过 compactRatio 控制触发阈值。

---

## 2. 移植方案：双 Hook 架构

### 架构图

```
┌─────────────────────────────────────────────────┐
│                Reasonix Agent                    │
│                                                   │
│ 每轮流程：                                         │
│                                                   │
│ ① PreCompact hook (compactRatio=0.01 → 几乎每轮) │
│    stdin: [sys][soul][user1][asst1含JSON]...[userN] │
│       ↓ CA assemble() 工作                        │
│    stdout: [sys][soul][L0:t1]...[L0:tN][L1相关]    │
│       ↓                                           │
│    Reasonix 用 stdout 替换 session 中间部分         │
│                                                   │
│ ② Agent 带着干净上下文思考 → LLM 请求              │
│                                                   │
│ ③ PostLLMCall hook                                │
│    stdin: 本轮的对话数据                            │
│       ↓ CA process_turn_async()                    │
│    → 写入 SQLite DB                               │
│                                                   │
│ ④ 进入下一轮 → 重复                               │
└──────────────────┬────────────────────────────────┘
                   │ shell hooks
    ┌──────────────▼────────────────────────────────┐
    │        CA 引擎 (Python 进程)                    │
    │                                                │
    │  PreCompact stdin → assemble() → stdout        │
    │  PostLLMCall stdin → process_turn_async()      │
    │                                                │
    │  ┌─ SQLite DB ───────────────────────────┐     │
    │  │ turn_cache: L0/L1/L2 分层存储           │     │
    │  │ bm25_tokens, embeddings, L-stage 异步   │     │
    │  └────────────────────────────────────────┘     │
    └────────────────────────────────────────────────┘
```

### 组件职责

| 组件 | 功能 | 对应 Hermes 组件 |
|---|---|---|
| **PreCompact hook** | stdin 收当前 messages → CA assemble() → stdout 输出结构化分层摘要 → Reasonix 替换 session 中的中间部分 | `pre_llm_call` (A-stage) |
| **PostLLMCall hook** | stdin 收本轮对话数据 → CA process_turn_async() → 写入 SQLite DB | `post_llm_call` (C-stage) |
| **CA 引擎** | BM25/embedding 检索、L0/L1/L2 分层、L-stage 异步摘要更新 | 不变 |
| **SQLite DB** | 同一份 DB，PreCompact 读取历史，PostLLMCall 写入新数据 | 不变 |
| **SessionStart hook** | 初始化 CA 引擎、配置 DB 路径 | `on_session_start` |

### 触发频率

```
PreCompact hook 的触发由 compactRatio 控制：

  compactRatio = 0.80（默认）
    → 只在达到 80% context_window 时触发
    → ~20-30 轮一次 → 缓存极好，但中间 20 轮是原始对话

  compactRatio = 0.01
    → prompt 长度满足 1% context_window 时触发
    → 几乎每轮触发（第二/三轮起就可满足）
    → 效果等同 Hermes CA 的每轮替换

这是用户可调的滑块——经济最优平衡点在两者之间。
```

---

## 3. 缓存分析

### 同一话题内：CA 不破坏缓存

```
同一个话题 BM25，连续 5 轮（有 CA）：

Turn 2:
  [sys][soul][L0:t1-BM25相关→L1][user2]
  ← 首次构建这条前缀

Turn 3:
  [sys][soul][L0:t1-BM25→L1][L0:t2][user3]
  ← 命中到 t1 末尾 ← 前缀稳定

Turn 4:
  [sys][soul][L0:t1→L1][L0:t2][L0:t3-BM25→L1][user4]
  ← 命中到 t2 末尾 ← 前缀稳定

Turn 5:
  [sys][soul][L0:t1→L1][L0:t2][L0:t3→L1][L0:t4-BM25→L1][user5]
  ← 命中到 t3 末尾 ← 前缀稳定
```

**CA 每轮追加在当前轮末尾，前缀稳定增长——和非 CA 的 append-only 完全等价。** 所谓的"CA 破坏缓存"只在**切换话题**时发生（此时升级配置变了），但非 CA 在切换话题时也会一样断前缀。这不是 CA 特有的代价。

### Hermes vs Reasonix 缓存对比

| 场景 | Hermes CA | Reasonix CA（compactRatio=0.01） |
|---|---|---|
| 同话题连续轮次 | ✅ 稳定增长 | ✅ 稳定增长 |
| 话题切换 | ⚠️ 升级配置变化 → 前缀断裂 | ⚠️ 同左 |
| 每轮替换频率 | 每轮替换（"碾碎"旧上下文） | 每轮替换（通过 PreCompact hook + 低阈值） |
| 缓存本质 | append-only，和非 CA 同等 | append-only，和非 CA 同等 |

**结论：CA 在两个平台的缓存行为完全一致。** 都在 append-only 路径上，都因话题切换而断前缀，都在同话题内稳定增长。

---

## 4. 需要确认的事项

### 4.1 PreCompact hook 的输入输出契约

当前通过二进制符号已知的 hook 方法：

```
reasonix/internal/hook.(*Runner).SessionStart
reasonix/internal/hook.(*Runner).SessionEnd
reasonix/internal/hook.(*Runner).PostLLMCall
reasonix/internal/hook.(*Runner).PreCompact
reasonix/internal/hook.(*Runner).PostToolUse
reasonix/internal/hook.(*Runner).PreToolUse
```

从现有 `PostLLMCall` hook 的实现（translator hook）和二进制字符串可知：

- **PostLLMCall**：stdin 接收 JSON（含 `reasoning`、`content`、`role` 等字段），stdout 输出替换内容
- **PreCompact**：推测收 `messages` + `session` JSON，stdout 输出替换的压缩文本
- **compactRatio**：可通过 `~/.reasonix/settings.json` 配置

### 4.2 配置方式

```json
// ~/.reasonix/settings.json
{
  "hooks": {
    "SessionStart": [
      {
        "command": "python3 /path/to/ca_reasonix/init.py",
        "description": "Initialize CA engine",
        "timeout": 5000
      }
    ],
    "PreCompact": [
      {
        "command": "python3 /path/to/ca_reasonix/compact.py",
        "description": "CA structured compaction via PreCompact hook",
        "timeout": 10000
      }
    ],
    "PostLLMCall": [
      {
        "command": "python3 /path/to/ca_reasonix/accumulate.py",
        "description": "CA C-stage data accumulation",
        "timeout": 8000
      }
    ]
  }
}
```

---

## 5. 与 Hermes CA 的差异

| 差异 | Hermes | Reasonix | 影响 |
|---|---|---|---|
| A-stage 入口 | `pre_llm_call`（Python hook） | `PreCompact`（shell hook，stdin/stdout JSON） | 接口不同但等价 |
| 触发时机 | 每轮 LLM 调用前 | 由 compactRatio 控制，设为 0.01 可近每轮 | 可调参数，非本质差异 |
| 引擎启动 | Python 同进程 | 每次 hook 启动一次 Python 解释器 ~50-100ms | 微小额外延迟 |
| C-stage 数据 | Python 函数直接调用 | JSON stdin → parse → process_turn_async() | 序列化/反序列化开销 |
| L-stage | 同进程异步线程 | 同进程异步线程 | 不变 |
| DB | SQLite 同进程访问 | SQLite 同进程访问 | 不变 |

**结论：不是能不能移植的问题，是接口适配的问题。** CA 的核心逻辑（assemble + process_turn_async + L-stage）一套不动，只在入口出口加一层 JSON stdin/stdout 适配。

---

## 6. 结论

**CA 可以完整移植到 Reasonix 上。** 方案是用 PreCompact hook（配置低 compactRatio 实现每轮触发）+ PostLLMCall hook 分别对应 A-stage 和 C-stage。CA 引擎本身的 BM25/embedding/L0/L1/L2/L-stage 全部不变。

两个平台的缓存行为完全一致——都是 append-only，同话题内稳定增长，话题切换时前缀断裂。

移植的复杂度和收益比较：
- 仅做 compaction 增强（PreCompact hook 替换默认 compaction）→ **低投入，高收益**
- 做完整每轮替换（compactRatio=0.01 + PostLLMCall 积累）→ **中投入，收益等同 Hermes 当前方案**
- 是否值得移植取决于是否有跨平台复用 CA 的需求，不是从零开始的需求

### 相关文档

| 文档 | 路径 | 内容 |
|---|---|---|
| CA v5 试验计划 | `docs/ca-v5-test-plan.md` | 缓存友好改进 + 参数调优 |
| 调试报告验证工作流 | `docs/ca-debug-report-fix-verification.md` | 修复验证流程 |
| 技术方案 | `docs/technical-plan.md` | CA 完整设计 |

---

## 7. 待确认：Reasonix messages 结构

移植的最后一道信息缺口是 Reasonix 的 messages 结构是否与 Hermes（OpenAI 标准格式）兼容。

### 7.1 已知的 messages 结构

**从 `~/.reasonix/sessions/default.jsonl` 观察到的格式：**

```json
// user 消息
{"role":"user","content":"用户输入"}

// assistant 消息（含工具调用）
{"role":"assistant","content":"回复","tool_calls":[
  {"id":"call_xxx","type":"function","function":{
    "name":"tool_name","arguments":"{\"key\":\"val\"}"
  }}
],"reasoning_content":"思考链"}

// tool 结果
{"role":"tool","tool_call_id":"call_xxx","name":"tool_name","content":"结果"}
```

**Hermes 的 messages 结构（CA 当前操作的对象）：**

```json
{"role":"user","content":"用户输入"}
{"role":"assistant","content":"回复","tool_calls":[
  {"id":"call_xxx","type":"function","function":{
    "name":"tool_name","arguments":"{\"key\":\"val\"}"
  }}
]}
{"role":"tool","tool_call_id":"call_xxx","content":"结果"}
```

**初步结论：两者高度兼容。** 都是 OpenAI chat completion 格式。Reasonix 多了一个 `reasoning_content` 字段和 tool 消息的 `name` 字段，这些都不影响 CA 的解析逻辑——CA 处理的是 `tool_calls`、`content`、`role` 这些标准字段。

### 7.2 待确认的关键细节

| 问题 | 重要性 | 需要查的内容 |
|---|---|---|
| **PreCompact hook 的 stdin JSON 格式** | 高 | 具体接收哪些字段？是否含 `session_id`？是否含 `messages`（完整列表）？ |
| **PreCompact hook 的 stdout 格式** | 高 | 输出纯文本还是 JSON 消息列表？是否需要指定替换的起止位置？ |
| **PostLLMCall hook 的 stdin 是否含全量 conversation_history** | 高 | C-stage 需要完整历史来提取工具轮次，否则无法工作 |
| **compactRatio 的配置位置和取值范围** | 中 | 是否在 `settings.json` 中配置？键名是什么？ |
| **SessionStart hook 是否能拿到 session_id** | 中 | CA 引擎初始化需要 session_id 来确定 DB 路径 |
| **PreCompact 输出如何替换 session 中的消息** | 高 | 是用 stdout 内容替换中间部分，还是追加，还是替换全部 messages？ |
| **多个 hook 的执行顺序** | 中 | PreCompact 在 Agent 思考前还是后？PostLLMCall 何时触发？ |

### 7.3 下一步

这些信息需要查看 Reasonix 源码（`internal/hook/runner.go`）来确认。建议开专门的对话去调查，查清后补充到此文档。调查入口：

```bash
# 在 reasonix 项目目录下
cd ~/projects/reasoning-translator/DeepSeek-Reasonix/
# 查看 hook runner 实现
cat internal/hook/runner.go
# 查看 PreCompact 的具体处理逻辑
grep -rn "PreCompact" internal/
```
