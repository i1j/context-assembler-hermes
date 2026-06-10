# [需求 v1: 工具轮数据重构]

> **评审状态**：已完成四轮多视角审查（三轮修正共 62 项闭环 + 第四轮零新问题），全部闭环。
> **定位**：方案 γ（完整 5 目标），3 个 PR 渐进合并，R5+R6 标记 P1 可按需 defer。
> **权威来源**：`docs/tool-turn-refactor-analysis.md` 为唯一权威技术参考。其他均为废稿。

<!-- auto-generated TOC -->
# design/tool-turn-refactor-req.md

## 目录

- [1. 需求规格](#1-需求规格)
- [2. 总体设计](#2-总体设计)
- [3. 边界与风险](#3-边界与风险)
- [4. 测试策略](#4-测试策略)
- [5. 实现计划](#5-实现计划)
- [6. 多视角审查处理记录](#6-多视角审查处理记录)
- [自检](#自检)

---
## 1. 需求规格

### 1.1 功能概述
5 大目标：存储结构重构 → 数据采集重定向 → 三级摘要 → 消除重复写入 → 三级注入。

> **P1 说明**：R5+R6（三级摘要 + 三级注入）是本版本完整目标，纯文本工具组摘要不调 LLM 风险低。但迭代紧张时可后置，核心收益（DB 膨胀消除）由 R1-R4 保障。

### 1.2 功能需求

| # | 需求点 | 验收标准 |
|---|--------|---------|
| **R1** | **存储结构重构**：主键 `(turn_index, api_call_count, seq_index)`；消息列 `role/content/tool_call_id/tool_name/tool_calls_json/finish_reason`；元数据列 `api_request_id/duration_ms/status/error_type/error_message/usage_json`。旧会话惰性迁移。**turn_plan 表同步扩展**：新增 `api_call_count` 列。 | ① 新 DB：`PRAGMA table_info(turn_cache)` 含新列 + `PRAGMA table_info(turn_plan)` 含 `api_call_count`；② 旧 DB（v4）：`read_session` 按旧列查询不抛异常 |
| **R2** | **数据采集重定向**：注册 `post_api_request`/`pre_tool_call`/`post_tool_call` 三钩子；`post_llm_call` 仅触发 flush + 生成对话轮摘要。Hermes 原始 status（`ok/error/blocked/cancelled`）透传。截断场景标记 `finish_reason`。`process_turn_async` 保留 `conversation_history`（用于 token_offset 估算 + 崩溃补偿），不再接受 `messages` 参数。 | ① 一次工具调用→DB 行数符合 §2.3 数据流图；② buffer tool_calls 个数 = DB 工具行数；③ Hermes 原始 status 可查；④ `finish_reason="length"`：assistant{tc}.l1_text 含 `"finish_reason":"length"` |
| **R3** | **Buffer 层**：`_tool_buffer: Dict[api_request_id, ToolGroupBuffer]`。Hermes 单线程模型下**不设线程锁**（flush 时 assert 无残留）；悬挂清理简化为"flush 时检测上一轮残留→告警+覆盖"。`_on_post_tool_call` 容错（key 不存在时自动创建条目）。多 API 同轮按 `api_call_count` 排序后写入。 | ① 多次 API：buffer N 个 key；② flush 后 buffer 为空；③ 悬挂测试：调 `_on_api_response` 后不 flush → 调 `reset()` → buffer 清空 + 告警；④ 多 API：flush 后 DB 行按 api_call_count 递增 |
| **R4** | **消除重复写入 + 职责分离**：`flush_tool_buffer` 只写工具行（user/assistant{tc}/tool），纯对话轮时不做写入。对话轮的 final 行 + L1 摘要由 `_run_c_stage` 写入。`_run_c_stage` 拆为 user 行（api=0, seq=0）和 final 行（api=999999, seq=0）+ l1_text 写入 final 行。 | 第 M 轮完成后，DB 中 `role='tool'` 的行数 = 前 M 轮所有工具调用次数之和。每轮新增工具行数 = 该轮实际工具调用数 |
| **R5** | **三级摘要**：对话轮 L1（`_call_llm_for_l1()` 不变）；工具组 L1（`generate_group_summary(thought, tool_results) → dict` 纯文本拼接，写入 assistant{tc}.l1_text）；工具轮 L1（`summarize(tool_call_msg, tool_responses)` 签名不变，由 flush 在调用后从返回值 `pop("thought_process", None)`） | ① assistant{tc}.l1_text = `{"group_intent":str,"group_result":str,"tool_count":int,"state":Literal["ok","error","blocked","cancelled"]}` JSON；② tool.l1_text 无 thought_process；③ 不调 LLM |
| **R6** | **三级注入（A-stage）**：`_rebuild_messages_from_cache` 按 `(turn, api, seq)` 排序 + **版本路由**（v4 旧表用 `ORDER BY turn_index ASC`）；`_compute_turn_plan_v2` 三级判定（TurnPlanEntry 扩展 api_call_count + turn_type="tool_group"，turn_plan 表已同步扩展）；注入 `[~/N/0]`/`[~/N/g]`/`[~/N/M]`。工具组格式：`"工具组：{intent}→{result}（{N}个，{state}）"`。`_CA_TAG_RE` → `r'^\[~/\d+(?:/\d+|/g)?\]\s*'`。`_deduplicate_messages` 扩充 `[~/N/g]` 指向标记。 | ① `assemble()` 返回含三类标记；② 工具组 L1 格式化后注入；③ 新正则匹配全部三类，旧正则不匹配 `/g`；④ `_deduplicate_messages` 正确处理 `[~/N/g]` |
| **R7** | **惰性迁移 + 只读模式**：`SQLiteStore(readonly=False)` 参数。`readonly=True` 时用 `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`；跳过 `PRAGMA journal_mode=WAL`、`executescript(_SCHEMA_SQL)`、`INSERT INTO _meta`；**不启动 checkpoint 守护线程**；`_check_schema` 只读检查（SELECT user_version，不迁移）；`read_session`/`read_turn` 按旧列+旧排序查询。版本缺失→旧 schema 只读+日志（不阻断）。 | ① 新 DB：`PRAGMA user_version=5`，turn_cache/turn_plan 含新列；② 旧 DB readonly：引擎加载后正常读取、`assemble()` 不崩溃、DB 文件 mtime 不变（无写操作）；③ 同一进程先后打开新旧 DB，各自读写互不影响 |

### 1.3 约束条件
- 技术栈：Python 3.10+，SQLite（WAL 仅写模式下），零新依赖
- 不修改：Hermes 宿主、`ooda_parser.py`/`post_process.py`、断路器、话题分割
- 版本号：v5.0

## 2. 总体设计

### 2.1 方案概述
三层架构：三钩子采集层 → Buffer 临时层 → 新 schema 存储层。Readonly 模式支持惰性迁移。

### 2.2 模块改动清单

| 模块 | 改动 |
|------|------|
| `__init__.py` (插件) | 注册 3 新 hook + 3 分发函数；post_llm_call 分发函数依次 flush → 异步 process_turn_async |
| `ca/__init__.py` (引擎) | `_tool_buffer` + 3 handler + flush；`process_turn_async` 去 messages 保留 conversation_history；`_run_c_stage` 拆 user+final 行；`_rebuild_messages_from_cache` 版本路由排序；三级注入；`_CA_TAG_RE`；`_deduplicate` 适配；Buffer 无锁 |
| `ca/store.py` | 新 schema（v5：turn_cache + turn_plan 扩展 `api_call_count`）；`write_turn` 向后兼容（旧签名→自动填充 api=0,seq=0）；`write_tool_group()` 事务内多行；`read_session`/`read_turn` 版本路由排序；`SQLiteStore(readonly=False)`；`_get_conn` readonly 路径 |
| `ca/cache.py` | **PR2 再动**（随 buffer 一起，PR1 不动） |
| `ca/tool_summarizer.py` | 签名不变；`generate_group_summary(thought, tool_results) → dict` 纯文本拼接 |
| `ca/lstage.py` | 新主键读取 + `get_pending_backfill` 复合键查询 |
| `ca/ooda_parser.py` / `post_process.py` | **不变** |

### 2.3 数据流

```
【工具调用轮】
post_api_request       → _tool_buffer["req_001"] = {thought, tool_defs, api_count, turn}
pre_tool_call          → _tool_buffer["req_001"].results[tc_id] = {pending}
post_tool_call         → _tool_buffer["req_001"].results[tc_id] = {result, status, dur, pending=False}

post_llm_call
  → flush_tool_buffer()                    ← 同步
       ├─ user 行       (api=0, seq=0)
       ├─ assistant{tc} (api=N, seq=0) + tool_group L1 (generate_group_summary)
       ├─ tool × M       (api=N, seq=1..M) + per_tool L1 (summarize 去 thought_process)
       │  按 api_call_count 排序写入多组
       └─ 清空 buffer
  → process_turn_async()                   ← 异步线程
       └─ _run_c_stage()
            └─ final 行 (api=999999, seq=0) + dialogue L1 (_call_llm_for_l1)

【纯对话轮（buffer 空）】
post_llm_call
  → flush_tool_buffer() → 空 buffer，无写入
  → process_turn_async()
       └─ _run_c_stage()
            ├─ user 行   (api=0, seq=0)
            └─ final 行  (api=999999, seq=0) + dialogue L1

【A-stage 重建 — 版本路由】
v5:  ORDER BY turn_index, api_call_count, seq_index
v4:  ORDER BY turn_index ASC    ← readonly 模式走此路径
注入: [~/N/0] = 对话轮  [~/N/g] = "工具组：{intent}→{result}（{N}个，{state}）"  [~/N/M] = 工具轮
```

### 2.4 行数速查

| 场景 | flush 后行数 | 总行数（含 _run_c_stage）|
|------|-------------|------------------------|
| 纯对话轮 | 0 | user(1) + final(1) = **2 行** |
| 工具轮（M 工具，1 组 API） | user(1) + assistant{tc}(1) + tool×M(M) = **M+2** | + final(1) = **M+3 行** |
| 工具轮（M 工具，N 组 API） | user(1) + assistant{tc}×N(N) + tool×M(M) = **N+M+1** | + final(1) = **N+M+2 行** |

## 3. 边界与风险

### 不做
- 不改变 `_call_llm_for_l1`/`parse_v1_markdown_xml`/OODA/断路器/去重核心逻辑（仅扩充 `[~/N/g]`）
- 不修改 Hermes 宿主
- 不做旧 DB 数据迁移重建
- 不引入新外部依赖

### 技术风险

| 风险 | 缓解 |
|------|------|
| Buffer 线程竞争 | Hermes 单线程同步调用模型，Buffer 无锁设计，flush 时 assert |
| Hermes hook 可用性 | 分析文档确认；实现前验证 |
| 进程崩溃 buffer 丢失 | 接受丢失；conversation_history 保留用于补偿回读 |
| PR1→PR2 中间状态 | write_turn 向后兼容（旧签名→自动填充 api=0,seq=0）|
| readonly 模式空文件 | 空文件 readonly 时 `read_session` 返回空列表（不抛异常）|
| turn_plan 表扩展 | 同步纳入 PR1 v5 schema |

## 4. 测试策略

### 4.1 测试分布

| 文件 | 新增 | 验证目标 |
|------|------|---------|
| `test_tool_buffer.py`（新增） | ~8 | Buffer 无锁生命周期、API 归组、悬挂清理（reset()）、截断、多 API 排序 |
| `test_store.py`（扩展） | ~12 | v5 schema 读写、turn_plan api_call_count 列、`write_turn` 向后兼容（旧签名→自动填充）、**readonly 模式**（`?mode=ro` 跳过写操作 + `read_session` 版本路由）、`write_tool_group` 事务 |
| `test_plugin.py`（扩展） | ~5 | 三钩子注册、分发正确性、post_llm_call 调用顺序（flush→async process_turn_async）|
| `test_c.py`（扩展） | ~10 | 三级摘要写入、`role='tool'` 行数验证、thought_process 剥离单元测试（pop 逻辑）|
| `test_a.py`（扩展） | ~8 | 三级注入标记、`_CA_TAG_RE` 新旧正则对比、`_deduplicate` 指向标记、**版本路由排序**|
| `test_v440.py`（适配） | — | 新主键格式适配 |

### 4.2 Fixture

| Fixture | 签名 | 说明 |
|---------|------|------|
| `simulate_tool_call_turn(engine, user_msg, assistant_with_toolcalls, tool_results)` | `user_msg: str`; `assistant_with_toolcalls: NormalizedResponse`; `tool_results: list[dict]`（含 tool_call_id/tool_name/args/result/status/duration_ms） | 封装 _on_api_response → _on_pre_tool_call → _on_post_tool_call → flush 时序 |
| `legacy_db_v4(tmp_path, with_data=False)` | 创建 schema_version=4 旧 DB；`with_data=True` 时灌入 2 轮对话+1 次工具调用的种子数据 | 供 R7 readonly 测试 |
| 取消 `_mock_llm_without_thought` | — | 不需要（ToolSummarizer 不调 LLM）|

### 4.3 注意事项
- **PR1 store 测试**：直接 `SQLiteStore` 实例化，不依赖 `ca_engine`
- **R6 单元测试**：seed DB 新格式数据（INSERT turn_cache 行），调 `assemble()` 验证标记
- **R4 行数验证**：`SELECT COUNT(*) FROM turn_cache WHERE role='tool'` = 累计工具调用数
- **readonly 测试**：读前记录 DB 文件大小，读后校验不变；校验无 -wal 文件产生

## 5. 实现计划

### PR 1：存储重构 + 惰性迁移（R1 + R7）— 7 步，可独立合入
1. `ca/store.py` — 新 schema v5（turn_cache 新主键+独立列 + turn_plan 扩展 api_call_count）
2. `ca/store.py` — `write_turn()` 向后兼容（旧签名→自动填充 api_count=0, seq_index=0）
3. `ca/store.py` — 新增 `write_tool_group()`（事务内多行写入）
4. `ca/store.py` — `read_session()`/`read_turn()` 版本路由 ORDER BY（v5 新排序，v4 旧排序）
5. `ca/store.py` — `SQLiteStore(readonly=False)` + readonly 模式（`?mode=ro` + 跳过所有写 + 无 checkpoint 线程 + _check_schema 只读不走 migration）
6. 测试：`test_store.py`（直接 SQLiteStore）

### PR 2：数据采集重定向（R2 + R3 + R4）— 7 步，需 PR1
7. `__init__.py`(插件) — `register()` 新增 3 hook + 3 分发函数（post_llm_call 分发顺序：flush → async process_turn_async）
8. `ca/__init__.py`(引擎) — `_tool_buffer` + 3 handler（_on_api_response/_on_pre_tool_call/_on_post_tool_call 容错 auto-create）
9. `ca/__init__.py`(引擎) — `flush_tool_buffer()` 实现（含事务、按 api_count 排序写入、generate_group_summary、summarize 后 pop thought_process）
10. `ca/__init__.py`(引擎) — `process_turn_async` 去 messages 参数，保留 conversation_history
11. `ca/__init__.py`(引擎) — `_run_c_stage` 职责分离（纯对话轮写 user+final 行；工具轮只写 final 行）
12. `ca/__init__.py`(引擎) — Buffer 无锁设计 + 悬挂清理（flush 时检测覆盖）
13. 测试：`test_tool_buffer.py` + `test_plugin.py`

### PR 3：摘要升级 + 三级注入（R5 + R6）— 8 步，P1 可 defer
14. `ca/tool_summarizer.py` — 新增 `generate_group_summary(thought, tool_results) → dict` 纯文本拼接
15. `ca/__init__.py`(引擎) — `_rebuild_messages_from_cache` 按新主键排序（版本路由）
16. `ca/__init__.py`(引擎) — `_compute_turn_plan_v2` 三级判定（TurnPlanEntry 扩展 api_call_count + turn_type="tool_group"）
17. `ca/__init__.py`(引擎) — `_build_messages_from_plan` 三级注入（含工具组 L1 格式化）+ `_CA_TAG_RE` 更新
18. `ca/__init__.py`(引擎) — `_deduplicate_messages` 扩充 [~/N/g] 指向标记
19. `ca/lstage.py` — 新主键读取 + get_pending_backfill 复合键查询
20. `ca/cache.py` — 新 key 路径写入 + `CacheBuilder.build()` 适配三元组 key
21. 测试：`test_c.py` + `test_a.py` + `test_v440.py`

## 6. 多视角审查处理记录

### 第一轮（25 项）→ 全部已关闭
详情见上版本 §6。采纳 21 项，用户确认不采纳 4 项。

### 第二轮（18 项）→ 全部已关闭

| # | 来源 | 意见 | 裁决 | § 对应 |
|---|------|------|------|--------|
| C1 | 一致性 | 数据流图对话轮 L1 归属矛盾 | flush 只写工具行，dialogue L1 归 _run_c_stage | §2.3 |
| C2 | 一致性 | PR1 write_turn 签名改→调用方断 | 向后兼容 + 自动填充 api=0,seq=0 | §5 PR1.2 |
| C3 | 一致性 | summarize 签名矛盾 | 签名不变，上层 pop thought_process | §2.2 |
| D1 | 可验证性 | 旧 DB 自动迁移 vs 只读冲突 | SQLiteStore(readonly=True) + ?mode=ro | R7 + §2.2 |
| D2 | 可验证性 | R6 跨 PR 不可测 | 单元测试 seed DB + 集成全链路 | §4.3 |
| A1 | 完整性 | PR2↔PR3 摘要函数依赖断裂 | generate_group_summary 提前到 PR2 | §5 PR2.9 |
| A2 | 一致性 | cache 三元组消费者滞后 | cache 推迟到 PR2（PR1 不动）| §5 PR1 |
| A3 | 完整性 | 纯对话轮未定义 | flush 空 buffer 不写，归 _run_c_stage | §2.3 |
| A4 | 一致性 | [~/N/g] 格式化未定义 | "工具组：{intent}→{result}（{N}个）" | R6 |
| A5 | 可验证性 | 进程中断不可测 | 改为 reset() 模拟 | R3 |
| A6 | 可验证性 | simulate 接口未定义 | 签名完整定义 | §4.2 |
| B1 | 必要性 | R5+R6 可 defer | P1 标记 | §1.1 |
| B2 | 完整性 | _deduplicate 未适配 [~/N/g] | 扩充指向标记 | §5 PR3.18 |
| B3 | 可验证性 | _mock_llm_without_thought 不需要 | 删除 | §4.2 |
| B4 | 可验证性 | store 测试应独立 | 直接 SQLiteStore | §4.3 |
| B5 | 一致性 | _CA_TAG_RE + 去重联合验证 | 在 test_a.py 覆盖 | §4.1 |

### 第三轮（19 项）→ 全部如下已闭环

| # | 来源 | 意见 | 裁决 | 对应改动 |
|---|------|------|------|---------|
| 3-1 | 完整性 | TurnPlanEntry.api_call_count 需 schema 配合 | **落库**：PR1 turn_plan 表扩展 api_call_count 列 | R1 + §2.2 |
| 3-2 | 完整性 | _run_c_stage 需拆 user+final 行 | PR2 中改 _run_c_stage：纯对话轮写 2 行，工具轮写 1 行 | R4 + §2.3 |
| 3-3 | 一致性 | readonly 模式 read_session 引用不存在的列 | **版本路由**：v5 用新 ORDER BY，v4 用旧 ORDER BY | R7 + §2.3 |
| 3-4 | 一致性 | write_turn NULL→ORDER BY 排序错乱 | 向后兼容层自动填充 api_count=0, seq_index=0 | §5 PR1.2 |
| 3-5 | 一致性 | readonly 模式完全未实现 | R7 明确定义 readonly 完整语义（构造参数 + 连接路径 + 跳过内容）| R7 |
| 3-6 | 必要性 | Buffer 线程锁过度设计 | **去锁**：Hermes 单线程模型，去 threading.Lock / 去 tool_groups property / 悬挂简化为 flush 时检测 | R3 |
| 3-7 | 完整性 | _CA_TAG_RE 未匹配 [~/N/g] | 已有 PR3 覆盖，补充 _deduplicate 指纹计算 | §5 PR3.17-18 |
| 3-8 | 完整性 | messages vs conversation_history 混淆 | process_turn_async 去 messages 留 conversation_history（token_offset + 补偿）| R2 + §3 |
| 3-9 | 完整性 | readonly _get_conn 跳跃路径未定义 | 伪代码路径补充 | R7 + §4.3 |
| 3-10 | 完整性 | 多 API 同轮排序未定义 | flush 按 api_call_count 排序写入 | R3 + §2.3 |
| 3-11 | 一致性 | PR1 cache 双写无消费者 | cache 推迟到 PR2（PR1 只改 store）| §5 PR1 |
| 3-12 | 一致性 | read_turn 等硬编码旧主键列 | PR1 不动这些方法（读旧表），PR3 新增 v5 方法 | §2.2 |
| 3-13 | 可验证性 | 行数验证精确性 | 两阶段表：flush 后 / 总 DB 行数公式 | §2.4 |
| 3-14 | 一致性 | post_llm_call 调用顺序 | 分发函数：flush 同步 → process_turn_async 异步 | §2.2 |
| 3-15 | 一致性 | buffer 未处理 pre/post 乱序 | _on_post_tool_call 自动创建条目 | R3 |
| 3-16 | 可验证性 | R4 表述歧义 | 改为 `role='tool'` 行数 = 累计工具次数 | R4 |
| 3-17 | 可验证性 | seed DB fixture 版本 | 用 PR1 升级后 fixture 或新增 ca_engine_v5 | §4.3 |
| 3-18 | 可验证性 | R7③ 过于简单 | 两个文件天然隔离，简单验证即可 | R7 |
| 3-19 | 完整性 | read_session 排序版本路由（与 3-3 合并）| 同 3-3 | §2.3 |

## 自检
- [x] 四轮多视角审查全部关闭（62 项 + 第四轮零新问题）
- [x] 验收标准可验证（每项有自动化方法）
- [x] 改动范围明确（6 模块 + 3 PR 22 步）
- [x] 测试策略可执行（fixture 签名 + 方法定义）
