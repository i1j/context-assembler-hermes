# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项          | 值                                                                                |
| ----------- | --------------------------------------------------------------------------------- |
| 版本        | v6.0（方向 B：turn_stream DB 重建 conv_history，替代 mutation）                  |
| 注入方式    | `_build_conv_history_v6` — 从 turn_stream DB 按话题级别 + 行类型构造 conv_history，不再修改 Hermes 消息 |
| plugin.yaml | v5.5.0                                                                    |
| 部署方式    | 自包含独立副本                                                                    |
| 插件路径    | `~/.hermes/profiles/tester/plugins/ca_assembler/`                               |
| 核心引擎    | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`）                 |
| 插件适配    | `plugins/ca_assembler/__init__.py`（入口 `CAContextAssemblerPlugin`）         |
| 启用方式    | `plugins.enabled: [ca_assembler, ...]`                                          |
| 源项目      | `~/projects/context-assembler/`（已分化，含独有修复）                           |

## 注册接口

`register(ctx)` 注册 8 个 Hermes hooks（5 生命周期 + 3 工具轮数据采集）：

```python
def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call_v5)
    ctx.register_hook("post_llm_call",    _on_post_llm_call_v5)
    ctx.register_hook("post_api_request", _on_post_api_request_v5)
    ctx.register_hook("pre_tool_call",    _on_pre_tool_call_v5)
    ctx.register_hook("post_tool_call",   _on_post_tool_call_v5)
```

## Hook 分发函数

### `_on_session_start` — 引擎初始化

```python
def _on_session_start(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（必含）

行为：

1. 创建 `CAContextAssemblerPlugin` 实例
2. 检查断路器 `_engine_errored` → 3 次失败则 1 小时冷却（in-memory）
3. 从 `get_hermes_home()` 获取 profile 基路径
4. DB 路径：`{hermes_home}/ca_cache/{session_id}.db`
5. 通过 `session_manager.get(session_id, db_path)` 创建引擎
6. 注册到模块级 `_engines` 字典（`session_id → plugin`）
7. 成功后日志：`"CA plugin started for session {session_id}"` 含阈值信息

### `_on_session_end` — 资源清理

```python
def _on_session_end(**kwargs: Any) -> None:
```

**kwargs**: `session_id`

行为：从 `_engines` 移除实例 → `engine.wait_for_pending()` + `session_manager.remove()`

### `_on_session_reset` — 重置

```python
def _on_session_reset(**kwargs: Any) -> None:
```

**kwargs**: `session_id`（可选，无 session_id 时重置全部）

行为：
行为：
1. 从 `_engines` 移除 → `engine.reset()` + 清除断路器状态

### `_on_pre_llm_call_v5` — E-stage 写 user Elm + 话题检测（v6 方向 B）

```python
def _on_pre_llm_call_v5(**kwargs: Any) -> Optional[str]:
```

**kwargs**: `session_id`, `user_message`, `conversation_history`, `context_length`

**E-stage 行为**（数据积累，方向 B 不再做 mutation）：

1. 计算当前 turn = `conversation_history` 中 role=user 的消息数
2. 检测 bg_review：如是则**完全跳过**（不增 turn、不写 DB、不调话题检测），返回 None
3. 写入 `turn_stream (turn, seq=0)`：`role='user', content=user_message` + Fct/Hdl 初始占位

**话题检测**（方向 B 中 compress 不再调 mutation）：

```python
话题检测（内置到 _on_pre_llm_call_v5，不再委托 pre_llm_call_v5）
  ├─ TopicGradeManager.detect(turn, ca_rows, user_msg)
  │    话题切换与否 → grade_on_switch → 打包旧话题 OV 提交
  └─ 返回 None（conv_history 构造由 CE compress 中的 _build_conv_history_v6 完成）
```

### `_on_post_llm_call_v5` — E-stage 写 final assistant + F-stage 触发（fin 粒度）

```python
def _on_post_llm_call_v5(**kwargs: Any) -> None:
```

**kwargs**: `session_id`, `user_message`, `assistant_response`, `conversation_history`

**E-stage 行为**：

1. 写入 `turn_stream (turn, seq=N+1)`：`role='assistant', content=assistant_response`
2. （方向 B：不再需要从 `_saved_history_snapshot` 恢复 content，因 compress 不修改 Hermes 消息）

**F-stage 触发**：

- 调用 `engine.process_turn_f_stage(turn, fin_seq=seq)`，传入当前 turn 编号和 fin 行 seq
- F-stage 在 daemon 线程中异步执行，从 DB 读取增量 Elm（user + 上次 fin 之后到本次 fin 之间的内容）生成 Fct

### `_on_post_api_request_v5` — thought Elm 写入 + tool 占位行

```python
def _on_post_api_request_v5(**kwargs: Any) -> None:
```

**kwargs**: `api_request_id`, `assistant_message`, `api_call_count`, `finish_reason`, `usage`, `session_id`

**E-stage 行为**（写即落盘，不经过 buffer）：

1. 提取 `assistant_message.content` 作为 thought Elm
2. 写入 `turn_stream (turn, seq=1)`：`role='assistant', content=thought, Fct=thought Fct`
3. 如有 tool_calls，为每个工具写入占位行：`role='tool', status='pending'`
4. 记录 `_tool_seq_map[tool_call_id] = (turn, seq)` 供回填

### `_on_pre_tool_call_v5` — 无操作

```python
def _on_pre_tool_call_v5(**kwargs: Any) -> None:
    pass
```

工具占位行已在 `_on_post_api_request_v5` 中写入，无需此处处理。

### `_on_post_tool_call_v5` — tool 行回填 + per-tool Fct

```python
def _on_post_tool_call_v5(**kwargs: Any) -> None:
```

**kwargs**: `tool_name`, `args`, `result`, `tool_call_id`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message`, `session_id`

**E-stage 行为**：

1. 查 `_tool_seq_map[tool_call_id]` 定位 (turn, seq)
2. 写入 `turn_stream (turn, seq)`：`role='tool', content=result, status, duration_ms`
3. 同步生成 per-tool Fct（`ToolSummarizer.summarize()`），写入 Fct/Hdl

## 存储结构

### DB 路径

```
{hermes_home}/ca_cache/{session_id}.db
```

当前 profile 实际路径：`~/.hermes/profiles/tester/ca_cache/{session_id}.db`

### turn_stream 表（turn_stream 表）

**主键**：`(session_id, turn, seq)`

| 字段              | 类型    | 说明                                      |
| ----------------- | ------- | ----------------------------------------- |
| `session_id`    | TEXT    | 会话 ID                                   |
| `turn`           | INTEGER | 对话轮次（1-based，对应 user 消息数）     |
| `seq`            | INTEGER | 轮内序号（0=user, 1=thought, 2..=tool, N=assistant_fin） |
| `role`           | TEXT    | `user` / `assistant` / `tool`            |
| `Elm`         | TEXT    | Elm（原始消息文本）                        |
| `tool_name`      | TEXT    | 工具名（仅 tool 行）                       |
| `tool_call_id`   | TEXT    | 工具调用 ID（仅 tool 行）                  |
| `args_json`      | TEXT    | 工具参数 JSON（仅 tool 行）                |
| `status`         | TEXT    | `ok` / `error` / `blocked` / `cancelled` / `pending` |
| `duration_ms`    | INTEGER | 工具执行耗时                                |
| `tool_calls_json` | TEXT   | assistant 的 tool_calls 定义 JSON          |
| `finish_reason`  | TEXT    | `stop` / `tool_calls` / `length`          |
| `usage_*`        | INTEGER | LLM token 用量                             |
| `biz_category`   | TEXT    | `bg_review` 或 NULL                        |
| `written_at`     | REAL    | time.time() 写入时间戳                     |
| `Fct`        | TEXT    | Fct（结构化摘要 JSON，C/F-stage 写入）     |
| `Hdl`        | TEXT    | Hdl（一句话标题，C/F-stage 写入）          |

每行 = 一条消息切片。E-stage 写即落盘，F-stage 回写 Fct/Hdl。

### 存储特性（turn_stream）

- **Elm 是原始数据**：每条消息切片独立存储，按 (turn, seq) 排序
- **Fct 含 Hdl**：Fct 存结构化摘要（含 stage_tag），Hdl 是 Hdl（一句话标题）
- **全量嵌入**：成功时 hdl + fct 均为 4096 字节 BLOB
- **WAL 模式**：Store 初始化时设置 PRAGMA journal_mode=WAL

## 内存缓存结构（AssemblyCache）

| 缓存                    | Key 类型                      | 说明                                             |
| ----------------------- | ----------------------------- | ------------------------------------------------ |
| `Hdls`            | `Dict[int, str]`            | 对话轮 Hdl 文本，key=turn_index                   |
| `Fcts`            | `Dict[int, str]`            | 对话轮 Fct JSON，key=turn_index                   |
| `tool_Hdls`       | `Dict[Tuple[int,int], str]` | 个体工具 Hdl，key=(turn_index, seq_index)         |
| `tool_Fcts`       | `Dict[Tuple[int,int], str]` | 个体工具 Fct JSON，key=(turn_index, seq_index)    |
| `tool_group_Hdls` | `Dict[Tuple[int,int], str]` | 工具组 Hdl，key=(turn_index, api_call_count)      |
| `tool_group_Fcts` | `Dict[Tuple[int,int], str]` | 工具组 Fct JSON，key=(turn_index, api_call_count) |

`add_tool_group()` 已由 E-stage 写即落盘替代，仅在 `cache.add_turn()` 中同步缓存。

## 关键环境变量

| 变量                          | 默认值                     | 说明                                                                           |
| ----------------------------- | -------------------------- | ------------------------------------------------------------------------------ |
| `CA_EMBED_BACKEND`          | `ollama`                 | 嵌入后端                                                                       |
| `CA_EMBED_MODEL`            | `qwen3-embedding:0.6b`   | 嵌入模型                                                                       |
| `CA_EMBED_ENDPOINT`         | `http://127.0.0.1:11435` | 嵌入服务端点                                                                   |
| `CA_LLM_MODEL`              | `qwen3-4b-instruct`      | Fct 摘要生成模型                                                                |
| `CA_LLM_ENDPOINT`           | `http://localhost:11435` | LLM 服务端点                                                                   |
| `CA_EMBED_TIMEOUT`          | 30                         | 嵌入超时（秒）                                                                 |
| `CA_LLM_TIMEOUT`            | 180                        | LLM 超时（秒）                                                                 |
| `CA_CONTEXT_LENGTH`         | 50000                      | 上下文 Token 预算上限                                                          |
| `CA_L1_TEMPERATURE`         | 0.3                        | Fct 摘要生成温度                                                                |
| `CA_L1_MAX_TOKENS`          | 800                        | Fct 摘要生成最大 Token 数                                                       |
| `CA_PROTECT_TAIL_TOKENS`    | 20000（来自 settings.yaml） | 当前话题块 Token 保护安全阀。来源：`ca/settings.yaml`→`_YAML_DEFAULTS`→env `CA_PROTECT_TAIL_TOKENS` |
| `CA_TOOL_TAIL_TURN_COUNT`   | 2                          | 工具轮尾区保留最近对话轮数                                                     |
| `CA_SYSTEM_TAIL_TURN_COUNT` | 2                          | 系统尾区：最近 N 条系统消息原文透传                                            |
| `CA_COMPRESSION_THRESHOLD`  | 0.50                       | 压缩警戒比值                                                                   |
|| `CA_HISTORY_INJECTION`      | `replace`                | (v6 方向 B 已弃用) 旧 mutation 模式的注入模式。v6 由 `_build_conv_history_v6` 统一构造，此变量不再控制行为。 |
| `CA_HISTORY_MUTATE`         | (已弃用)                   | 2值开关，`1`→替换 `0`→追加。未设 `CA_HISTORY_INJECTION` 时兼容此旧变量 |
| `CA_LLM_THINK`              | 未设置（等效 false）       | Fct LLM think 参数（1/0/true/false）。Fct 是结构化提取任务，默认关闭推理链以节省 token。CA_LLM_THINK=1 开启。 |

### 话题等级配置（TopicGradeManager）

| 变量                           | 默认值 | 说明                           |
| ------------------------------ | ------ | ------------------------------ |
| `CA_TOPIC_JACCARD_ENTRY`     | 0.02   | 弱匹配 Jaccard 阈值（新话题接入） |
| `CA_TOPIC_JACCARD_CHAIN`     | 0.04   | 强匹配 Jaccard 阈值（链内延续） |
| `CA_TOPIC_RADIUS_WEIGHT`     | 2.0    | 半径公式中最近邻距离的权重系数 |
| `CA_TOPIC_MAX_UPGRADE`       | 10     | 检索升级最大 topic 数          |
| `CA_TOPIC_BG_LEVEL`          | Hdl    | BG 话题固定等级                |

## 话题分割与等级管理（TopicGradeManager）

`topic_manager.py` 的 `TopicGradeManager` 负责：
1. **增量话题分割** — 每次 pre_llm_call 检测新 turn 是否延续或切换话题
2. **等级缓存** — topic→grade 映射，切换间冻结（保障 prompt caching 稳定）
3. **切换定级** — 话题切换时 embed 用户消息、计算形心、按半径定级

### 实例化

```python
mgr = TopicGradeManager(store, embed_client)
```

由 `CAContextAssemblerPlugin.__init__()` 创建，挂载到 `self._topic_mgr`。

### Detect：增量话题分割

每次 `pre_llm_call_v5` 调用 `mgr.detect(turn, ca_rows, user_msg)`：

```
1. 强制短语匹配 → 新话题（_scan_forced_split_phrases）
2. 首轮 → topic 1
3. 当前轮 Fct 与累积 Fct 文本做 _jaccard_text()
   - ≥ CHAIN(0.04) → 强匹配 → 同话题，追加 Fct
   - ≥ ENTRY(0.02) → 弱匹配 → 同话题，追加 Fct
   - 其余 → 新话题
4. 返回 bool：话题是否切换
```

**Jaccard 特征提取**（`_jaccard_text()`）：
- 中文 CJK 单字符（实义字）
- CJK 二元组（每两个相邻实义字）
- 英文/数字词（含下划线分隔的 token）

### Grade on Switch：切换定级

当 `detect()==True`，调用 `mgr.grade_on_switch(q_emb, user_msg)`：

```python
if switched:
    q_emb = engine.embed_client.embed(user_msg)
    mgr.grade_on_switch(q_emb, user_msg)
```

流程：
1. 为每个旧话题计算形心（基于成员 turn 的 Fct embedding 均值）
2. 以 q_emb 为查询点，形心为参考，按**半径公式**定级：
   - `topic 半径 r = min(max_intra, nearest/WEIGHT)`
   - 内球(q→形心 ≤ r) → TopicGrade.ACT（使用 Fct 完整摘要）
   - 外球(q→形心 ≤ 2r) → TopicGrade.REL（使用 Hdl[:150] 截断摘要）
   - 远距离(q→形心 > 2r) → TopicGrade.FAR（使用"略"语义省略标记）
3. 新话题强制 ACT
4. topic→grade 缓存冻结，至下次切换前不变

### Get Turn Grade：等级查询

```python
grade = mgr.get_turn_grade(turn_num)  # 返回 TopicGrade.ACT | .REL | .FAR
```

- 先在 topic_grades 缓存中查 topic→grade
- 再查 turn→topic 映射
- 都没有 → 返回 TopicGrade.ACT（保守——完整保留）

### Reset

```python
mgr.reset()  # 清空所有状态：/new 或 /reset 时调用
```

## 增量缓存（⚠️ v6 方向 B 已废弃）

v6 的 `_build_conv_history_v6` 不再需要 A-stage 缓存（`_A_stable_cache` / `_A_cache_turns` / `_A_cache_is_stale`）。
每次 compress 从 turn_stream DB 直接重建 conv_history，不保留中间缓存。

旧 v5 增量缓存的调度逻辑、Fct pending 防护、回退条件均不再适用，仅保留缓存变量
清空以防旧代码残留。

## Fct 摘要生成架构

关键模块表见上。数据流详见 [v5.2 分析报告](docs/changelog.md)（v5.1.0 节）。

## 断路器

| 项目     | 值                                                       |
| -------- | -------------------------------------------------------- |
| 文件路径 | `{hermes_home}/.ca_assembler_state_{PID}.json`         |
| 状态格式 | `{"failures": N, "retry_after": null \| ISO_timestamp}` |
| 触发阈值 | 连续 3 次失败                                            |
| 冷却时间 | 1 小时                                                   |
| 恢复     | 1 次成功 reset 或超时后自动清除                          |

## 运行时验证

### 插件是否已注册

```bash
grep -i 'CA plugin started for session' ~/.hermes/profiles/tester/logs/agent.log
```

### 数据是否正在积累

```python
import sqlite3
from pathlib import Path
db = Path.home() / '.hermes/profiles/tester/ca_cache/{session_id}.db'
conn = sqlite3.connect(str(db))
cur = conn.cursor()
# 查询 turn_stream 行数和 Fct 覆盖率
cur.execute('SELECT COUNT(*), SUM(CASE WHEN Fct IS NOT NULL AND Fct != \'\' THEN 1 ELSE 0 END) FROM turn_stream')
total, has_fct = cur.fetchone()
print(f'{total} rows, {has_fct} with Fct')

# 按角色分布
cur.execute('SELECT role, COUNT(*), SUM(CASE WHEN Fct IS NOT NULL AND Fct != \'\' THEN 1 ELSE 0 END) FROM turn_stream GROUP BY role')
for role, count, fct_count in cur.fetchall():
    print(f'  {role}: {count} rows, {fct_count} with Fct')
```

## 测试接口清单

**全部 387 测试通过（10 ⏭️）**

```bash
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest tests/ --tb=short -q -p no:cacheprovider -o "addopts="
```

**测试覆盖**：387 ✅ / 10 ⏭️ / 0 ❌

| 测试文件 | 说明 | 状态 |
|---------|------|------|
| `test_plugin.py` | 插件适配层（生命周期、断路器、hook 注册） | ✅ 29 tests |
| `test_store.py` | turn_stream CRUD | ✅ 20 tests |
| `test_fstage.py` | Fct 处理（_extract_hdl、_format_fct_for_display、_is_valid_fct） | ✅ 19 tests |
| `test_astage.py` | A-stage 替换逻辑 | ✅ 5 tests |
| `test_estage.py` | E-stage 写即落盘 hook | ✅ 2 tests |
| `test_v460.py` | 话题分割（遗留兼容） | ✅ 22 tests |
| `test_parse_v1.py` | Fct 解析（PAIR_PATTERN、clean_increment、_json_to_v1_markdown） | ✅ tests |
| ... 其它 | config/embedding/health/quality/system/circuit/summarizer/lifecycle/degradation | ✅ 剩余 tests |
| ⚠️ `topic_manager` | 当前无专用测试文件 | ❌ missing |

**已删除的死测试**：
- `test_a.py`、`test_c.py`、`test_aligned_outcomes.py`（全文件）
- 6 条混文件死用例（引用已删除的 `assemble()`、`_compute_turn_plan_v2` 等）

### 调试记录

详见 `docs/changelog.md` 历史记录和 `docs/wiki/decisions/20-incidents-review.md` 事故报告。

### 预算实测

所有行 `_assemble_status=0`，budget 从未耗尽。详见 [变更历史](docs/changelog.md)（v5.1.0 节）。

## 已知问题 / 待办

| 问题 | 状态 |
|------|------|
| `topic_manager` 无专用测试文件 | ❌ missing |
| `_build_conv_history_v6` 测试覆盖不足（替换了 `_simple_mutation_mode_v5` 的测试） | ⏳ 待补充 |
| `reasoning_content`（模型思考链）在 conv_history 重建中如何处理 | ⏳ 待定（当前同 mutation 模式未携带） |

详见 [docs/changelog.md](docs/changelog.md) 和 [wiki 事故记录](docs/wiki/decisions/20-incidents-review.md)。

## ⚠️ 关键概念：CA 不是 context engine

**CA 是 Hermes 插件（plugin），不是 context engine。**

Hermes 有两条完全独立的机制：

1. **Plugin hooks** → `plugins.enabled` 中的 `ca_assembler`。通过 `register()` 注册的 8 个 hook 回调工作。
2. **Context engine** → `context.engine` 配置项。只从仓库 `plugins/context_engine/` 子目录加载引擎，与用户 profile 的 `plugins/ca_assembler/` 毫无关系。

**`context.engine` 设成什么、是否回退，都不影响 CA 插件的工作。**

## 与 source project 的差异

详见 [docs/changelog.md](docs/changelog.md)（v5.5.0 行）。

## 脚本工具

| 脚本                                   | 说明                             |
| -------------------------------------- | -------------------------------- |
| `scripts/backfill_tool_summaries.py` | 历史工具摘要回填（修复存量数据） |
| `scripts/benchmark_l1.py`            | Fct 生成性能基准测试              |

## 相关文档

所有文档按类别归档在 `docs/` 下。技术方案统一为 `docs/wiki/`（双维度：architecture + decisions），取代所有旧设计文档。

| 类别         | 目录                        | 内容                                                                        |
| ------------ | --------------------------- | --------------------------------------------------------------------------- |
| **技术方案**（双维度） | `docs/wiki/`          | **权威技术方案**：`architecture/`（14 页，空间维度）+ `decisions/`（30 页，时间维度） |
| 变更历史     | `docs/changelog.md`         | 全版本变更记录（唯一权威源）                                                |
| 设计（历史） | — | 旧设计文档已 [[清]]理，详见 OV `projects/context-assembler/design/` |
| 分析         | `docs/changelog.md`        | 历史分析见变更日志（v5.1.0/v5.2.0 节），详见 OV `design/` |
| 调试         | — | 调试记录已归档，详见 OV `projects/context-assembler/design/` |
| 知识图谱     | `graphify-out/`             | 代码静态图：573 节点、766 边、55 社区。God Nodes、社区结构              |

### 快速入口

- 📐 [技术方案总览](docs/wiki/README.md)
- 🗺️ [决策关系图 + 全量索引](docs/wiki/INDEX.md)
- 🧩 [当前系统组件（architecture/）](docs/wiki/architecture/)
- ⏳ [决策树时间线（decisions/）](docs/wiki/decisions/)

## 知识图谱（graphify）

`graphify-out/` 目录包含项目代码的静态知识图谱分析产物，用于快速理解架构和数据流。

| 文件 | 用途 |
|------|------|
| `GRAPH_REPORT.md` | 图谱概况：573 节点、766 边、55 社区（DeepSeek 命名）。God Nodes 排名、社区结构、边关系分布 |
| `graph.json` | 完整图谱数据（过滤后，不含 .graphify_pylib 缓存库） |
| `graph.html` | 力导向可视化（vis-network） |

**当前状态：** 0 孤立节点，全部 55 社区已命名，Wiki 文档 63 条代码外联边。

**构建命令：**
```bash
# 增量更新
PYTHONPATH=.graphify_pylib .graphify_pylib/bin/graphify update .

# 过滤缓存库 + 添加缺失边 + 重聚类 + 社区命名（四步完整流程）
# → 见 graphify skill 的「Graph Cleanup Workflow」节
```

## ContextEngine 壳（v6.0 方向 B）

`CAContextEngine` 实现了 `agent.context_engine.ContextEngine` ABC，通过 `register()` 中的
`ctx.register_context_engine("ca_assembler", _ce_engine)` 注册到 Hermes。

### 与插件钩子的关系

CE 路径与 Hook 路径的执行时序（由 Hermes `turn_context.py` 驱动）：

```
                                    ← should_compress()=True（每轮）
 pre_llm_call                      ← 写 user 到 turn_stream + 话题检测（不 mutation）
 LLM 调用
 post_llm_call                     ← 写 fin 到 turn_stream → F-stage
                                    ← compress() 从 turn_stream DB 重建 conv_history（方向 B）
```


| 路径         | 触发时机  | 后端                          |
|-------------|----------|-------------------------------|
| Hook 路径   | 每轮      | pre_llm_call → 写 turn + 话题检测（v6 不 mutation） |
| CE 路径     | post_llm_call 后 | should_compress()=True → compress() → `_build_conv_history_v6` |

### compress() 行为（v6 方向 B）

v6 compress() **不再修改 Hermes 消息**。而是调用 `_build_conv_history_v6(topic_mgr, system_message)`，
从 turn_stream DB 直接构造优化后的 conv_history 列表并返回，供 CE 管线替换。

**核心规则**：

| 位置（区域） | user/fin | thought/tool |
|---|---|---|
| 尾部保护区（最后 2 user 轮） | Elm（原文） | Elm（原文，不降级） |
| ACT 区 | Elm | Fct（降一级） |
| REL 区 | Fct | Hdl（降一级） |
| FAR 区 | Hdl | 删除（不保留） |

**尾部保护区**：最后 2 个 user 消息内的所有行（含 thought/tool）均保留为 Elm，
不受话题等级影响。

| `graph.html` | 可交互图谱（浏览器打开），可视化节点与边关系 |
| `graph.json` | 完整图谱数据（节点+边），可被代码分析工具消费 |
| `manifest.json` | 文件清单（AST 哈希、mtime），用于增量更新检测 |
| `cache/` | AST 解析缓存，加速后续 graphify 重建 |
| `.graphify_labels.json` | 社区标签映射 |

**何时使用**：代码重构前、跨模块数据流追踪、定位 God Nodes（高耦合中心如 `ContextAssembler` 100 度、`SQLiteStore` 62 度）。
