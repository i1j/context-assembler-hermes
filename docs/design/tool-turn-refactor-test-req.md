# [测试需求 v1: 工具轮数据重构] — 测试需求规格说明书

> **版本**：v5.0 · **基准需求**：`tool-turn-refactor-req.md`
> **定位**：方案 γ（完整 5 目标），3 个 PR 渐进合并
> **覆盖**：R1–R7（含 P1 标记的 R5+R6）
> **审查状态**：已通过一轮多视角审查（完整性/一致性/必要性/可验证性），全部意见闭环

---

<!-- auto-generated TOC -->
# design/tool-turn-refactor-test-req.md

## 目录

- [1. 测试范围与覆盖矩阵](#1-测试范围与覆盖矩阵)
- [2. PR1 测试需求：存储结构重构 + 惰性迁移（R1 + R7）](#2-pr1-测试需求存储结构重构--惰性迁移r1--r7)
- [3. PR2 测试需求：数据采集重定向（R2 + R3 + R4）](#3-pr2-测试需求数据采集重定向r2--r3--r4)
- [4. PR3 测试需求：三级摘要 + 三级注入（R5 + R6）](#4-pr3-测试需求三级摘要--三级注入r5--r6)
- [5. Fixture 定义](#5-fixture-定义)
- [6. 测试文件改动清单](#6-测试文件改动清单)
- [7. 环境与风险](#7-环境与风险)
- [8. 实施计划](#8-实施计划)
- [9. 验收标准](#9-验收标准)
- [10. 多视角审查处理记录](#10-多视角审查处理记录)
- [自检](#自检)

---
## 1. 测试范围与覆盖矩阵

| 需求 | 测试文件 | 预计用例数 | 优先级 | 所属 PR |
|------|---------|-----------|--------|---------|
| **R1** 存储结构重构 | `test_store.py` | ~9（含 user_version 断言） | P0 | PR1 |
| **R7** 惰性迁移 + 只读模式 | `test_store.py` | ~10（含 skipif 脚注） | P0 | PR1 |
| **R2** 数据采集重定向 | `test_plugin.py` + `test_tool_buffer.py` | ~7（TC#22 合并入现有测试） | P0 | PR2 |
| **R3** Buffer 层 | `test_tool_buffer.py`（新增） | ~11（TC#31/#32 合并为参数化） | P0 | PR2 |
| **R4** 消除重复写入 + 职责分离 | `test_tool_buffer.py` + `test_c.py` | ~5（TC#39/#40 合并为单测） | P0 | PR2 |
| **R5** 三级摘要 | `test_c.py` + `test_tool_buffer.py` | ~6（TC#43 追加正向断言 + 新增总行数端到端用例） | **P1 ⏭️ 可跳过** | PR3 |
| **R6** 三级注入 | `test_a.py` + `test_v440.py` | ~7（#50/#51 合并为参数化） | **P1 ⏭️ 可跳过** | PR3 |

---

## 2. PR1 测试需求：存储结构重构 + 惰性迁移（R1 + R7）

### 2.1 测试目标

验证 v5 schema 的正确性（含 `PRAGMA user_version=5`）、向后兼容性、readonly 模式的行为、版本路由排序。

### 2.2 测试策略

- **独立 Store 测试**：直接实例化 `SQLiteStore`，不依赖 `ca_engine` fixture
- **旧版兼容**：写入 v4 格式的种子 DB 验证 readonly 读取
- **行数定量**：用 `PRAGMA table_info` + `SELECT COUNT(*)` + `PRAGMA user_version` 定量验证

### 2.3 测试用例

#### R1-v5-schema — 新 schema 验证

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 1 | turn_cache 新列存在 + schema 版本 | 新建 `SQLiteStore`（v5） | `PRAGMA table_info(turn_cache)` + `PRAGMA user_version` | 列含 `role`/`content`/`tool_call_id`/`tool_name`/`tool_calls_json`/`finish_reason`/`api_request_id`/`duration_ms`/`status`/`error_type`/`error_message`/`usage_json`；`user_version=5` |
| 2 | turn_plan api_call_count 列存在 | 同上 | `PRAGMA table_info(turn_plan)` | 含 `api_call_count` 列 |
| 3 | 新主键唯一约束 | 同上 | 插入 `(session_id, turn_index, api_call_count, seq_index)` 相同的两行 | 第二行触发 `INSERT OR REPLACE` 覆盖 |
| 4 | user 行写入（向后兼容） | `write_turn` 旧签名 | 传 `session_id, turn_index` 不带 `api_call_count`/`seq_index` | 自动填充 `api_call_count=0, seq_index=0` |
| 5 | assistant{tc} 行写入 | `write_tool_group()` | 写入 API 组数据 | 行含 `role='assistant'` + `tool_calls_json` 非空 + `api_request_id` 匹配 |
| 6 | tool 行写入 | `write_tool_group()` | 写入工具结果 | 行含 `role='tool'` + `tool_call_id` 非空 |
| 7 | 多 API 写入顺序 | 调用 `write_tool_group()` 传入多组 | DB 查询 | 行按 `api_call_count` 升序排列 |
| 8 | 旧表 `l0_text`/`l1_text` 仍可读 | 旧 v4 DB | `read_turn()` 查询 | 返回 dict 且 `dict["l0_text"]` 为非空字符串 |
| F1 | **总行数端到端验证（单组）** | 工具轮完整流程：flush + `_run_c_stage`（1 组 API，3 工具） | `SELECT COUNT(*)` | 总行数 = M+3 = 6 |
| F2 | **总行数端到端验证（多组）** | 工具轮完整流程：flush + `_run_c_stage`（2 组 API，共 4 工具） | `SELECT COUNT(*)` | 总行数 = N+M+2 = 2+4+2 = 8 |

#### R7-readonly — 惰性迁移 + 只读模式

⚠️ 用例 #9–#16 依赖 SQLite URI 路径 `?mode=ro`，需 `sqlite3.sqlite_version_info >= (3, 22, 0)`，否则 `pytest.mark.skipif` 跳过。

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 9 | 只读模式不写 WAL | `SQLiteStore(path, readonly=True)` 用 v4 DB | `PRAGMA journal_mode` | 不是 WAL（`delete` 或 `memory`）|
| 10 | 只读模式不写 _meta | 同上 | `SELECT value FROM _meta WHERE key='schema_version'` | 返回 `'4'`（v4 旧 DB 的原有版本号，未因 readonly 打开而新增） |
| 11 | 只读模式不修改 DB 文件 | 同上 | 记录文件 mtime 前/后 | mtime 不变 |
| 12 | 只读模式不走 checkpoint 线程 | 同上 | 验证 `_checkpoint_thread` | `None`（不启动守护线程） |
| 13 | v4 旧 DB 只读读取正常 | 创建 v4 schema 种子 DB（2 轮对话 + 1 次工具调用） | ① `read_session()` ② `assemble()` | ① 返回非空列表（至少 2 行）② 返回 messages 列表含 CA 标记 `[~/N/M]` |
| 14 | 同一进程新旧 DB 互不干扰 | 先连 v5 DB 写入，再连 v4 DB readonly | ① 记录 v4 文件 mtime ② 向 v5 写入 ③ 记录 v4 mtime ④ 记录 v5 行数 | ①→③ v4 mtime 不变 ②→④ v5 行数增加 |
| 15 | 空文件 readonly 不抛异常 | 空文件路径 | `read_session()` | 返回空列表 |
| 16 | 只读模式 `_get_conn` 用 `?mode=ro` URI | `SQLiteStore(path, readonly=True)` | 调用 `_get_conn()` | 连接串含 `file:{path}?mode=ro` |
| 17 | read_session 版本路由：v5 新排序 | v5 DB 插入多轮 | `read_session()` | 结果按 `turn_index, api_call_count, seq_index` 排序 |
| 18 | read_session 版本路由：v4 旧排序 | v4 DB 种子数据 | `read_session()` | 结果按 `turn_index ASC` 排序 |

### 2.4 验收检查方法

```bash
# 新 schema 列检查 + schema 版本
python -c "
import sqlite3
conn = sqlite3.connect('test_v5.db')
cols = [r[1] for r in conn.execute('PRAGMA table_info(turn_cache)')]
assert 'role' in cols, f'Missing role in {cols}'
assert 'tool_calls_json' in cols
ver = conn.execute('PRAGMA user_version').fetchone()[0]
assert ver == 5, f'Expected user_version=5, got {ver}'
print('PASS: v5 schema columns + user_version')
"

# 只读模式不写 -wal
before = os.path.getsize('test.db')
# ... 只读操作 ...
after = os.path.getsize('test.db')
assert before == after  # 文件大小不变
assert not os.path.exists('test.db-wal')
```

---

## 3. PR2 测试需求：数据采集重定向（R2 + R3 + R4）

### 3.1 测试目标

验证三钩子的正确注册和分发、Buffer 生命周期（存储/归组/排序/清空）、`flush_tool_buffer` 行数准确（含多 API 场景 N>1）、`_run_c_stage` 职责分离。

### 3.2 测试策略

- **新文件**：`test_tool_buffer.py`（新增，~11 用例）
- **插件扩展**：`test_plugin.py`（追加 ~5 用例，auto-create 合并后减少 1 个）
- **C-stage 扩展**：`test_c.py`（追加 ~6 用例）
- **Buffer 无锁设计**：Hermes 单线程模型保证，不单独测试
- **时序模拟**：用 `simulate_tool_call_turn` fixture 封装完整工具调用周期

### 3.3 Fixture 要求

见 §5 Fixture 定义 `simulate_tool_call_turn`。

### 3.4 测试用例

#### R2-hooks — 数据采集重定向（test_plugin.py 扩展）

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 19 | register() 新增 3 钩子 | 插件加载 | 验证 `ctx.register_hook` 调用 | 含 `post_api_request` / `pre_tool_call` / `post_tool_call` + 原 5 钩子 = 8 次调用 |
| 20 | `post_api_request` 分发 | 引擎就绪 + 模拟 API 请求 | `plugin.on_api_response(...)` | buffer 中创建 `ToolGroupBuffer` 条目 |
| 21 | `pre_tool_call` 分发 | buffer 有条目 | `plugin.on_pre_tool_call(...)` | 对应 tool_call_id 设为 pending |
| 22 | `post_llm_call` 调用顺序 | 3 钩子数据就绪 | 调 `post_llm_call` | ① `flush_tool_buffer()` 被调用 ② `process_turn_async` 被异步调用 |
| 23 | Hermes 原始 status 透传 | 工具返回 `ok/error/blocked/cancelled` | 查 `assistant{tc}.l1_text` | l1_text JSON 中 `state` 字段为原始值 |
| 24 | finish_reason="length"（工具调用截断） | 工具调用被截断 | flush 后查 `assistant{tc}.l1_text` | flush 仍执行，`l1_text` 含 `"finish_reason":"length"` |
| F3 | **finish_reason="length"（纯文本截断）** | 纯文本截断（无工具调用） | 调 `post_llm_call` → flush + `_run_c_stage` | flush 无写入（buffer 空），final 行标记 `finish_reason:"length"` |
| 25 | `process_turn_async` 去 messages 参数 | 调用 `process_turn_async` | 验证签名 + conversation_history 传至 `_run_c_stage` | 无 `messages` 参数，保留 `conversation_history` |

#### R3-buffer — Buffer 层（test_tool_buffer.py 新增）

⚠️ 用例 #27–#35 依赖 `engine._on_api_response()`、`engine._on_pre_tool_call()`、`engine._on_post_tool_call()`、`engine.flush_tool_buffer()` 等新方法，PR2 实现前不可运行。建议模块级 `importorskip` 保护。

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 26 | 空 buffer 初始状态 | 引擎启动 | 检查 `_tool_buffer` | `{}` 空字典 |
| 27 | 单 API 单工具：存储 | 收到 API 请求 | 调 `post_api_response` → `pre_tool_call` → `post_tool_call` | buffer 1 个 key，results 1 条记录 |
| 28 | 单 API 多工具：归组 | 1 次 API + 3 个 tool_call | 同上 3 钩子调用 3 次 | buffer 1 个 key，results 3 条，按 tool_call_id 可索引 |
| 29 | 多 API 同轮：多 key | 2 次 API 请求，各含工具 | 3 钩子顺序调用 | buffer 2 个 key，各 key 含各自 results |
| 30 | flush 后 buffer 为空 | buffer 有数据 | 调 `flush_tool_buffer()` | buffer 清空为 `{}` |
| 31 | flush 写入行数符合公式（单组） | 场景 28（M=3, N=1） | flush → `SELECT COUNT(*) WHERE role IS NOT NULL` | DB 行数：user(1) + assistant{tc}(1) + tool×3(3) = **5 行** |
| 32 | flush 写入行数符合公式（多组） | 2 组 API、共 4 工具（M=4, N=2） | flush → `SELECT COUNT(*)` | DB 行数：user(1) + assistant{tc}×2(2) + tool×4(4) = **N+M+1 = 7 行** |
| 33 | flush 多 API 按排序写入 | 场景 29（2 API） | flush → DB 查询 | 行按 `api_call_count` 排序 |
| 34 | 悬挂清理：reset() 清空 | buffer 有残留 | 调 `reset()` | buffer 清空 + 告警日志 |
| F4 | **悬挂清理：flush 自检测路径** | buffer 有上一轮残留 | 调 `flush_tool_buffer()` | 告警日志提示残留，旧数据被新数据覆盖 |
| 35 | `_on_post_tool_call` 容错 auto-create + args 验证 | key 不存在时调用（引擎层） | 直接调 `_on_post_tool_call` 传入 args | 不抛 KeyError，buffer 中自动创建条目；条目中 args 与传入参数一致 |

#### R4-dedup — 消除重复写入 + 职责分离

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 36 | flush 只写工具行，不写对话轮 | 纯对话轮（buffer 空时触发 flush） | 调 `flush_tool_buffer()` | 无 DB 写操作（0 行新增） |
| 37 | `_run_c_stage` 写 user+final 行（纯对话轮） | 纯对话轮完成 | `_run_c_stage` 执行后 | DB 新增 2 行：user 行(api_call_count=0,seq_index=0) + final 行(api_call_count=999999,seq_index=0) |
| 38 | `_run_c_stage` 写 final 行（工具轮） | 工具轮 flush 后 | `_run_c_stage` 执行后 | DB 新增 1 行：final 行(api_call_count=999999,seq_index=0) + l1_text 写入 |
| 39 | `role='tool'` 行数验证 | 累计 M 轮共 N 次工具调用 | `SELECT COUNT(*) FROM turn_cache WHERE role='tool'` | 总数 = N |
| 40 | 每轮新增工具行数 = 该轮实际工具调用数 | 第 M 轮有 T 次调用 | 该轮前/后 DB 工具行数差 | 差 = T |

---

## 4. PR3 测试需求：三级摘要 + 三级注入（R5 + R6）

> **⏭️ P1：时间紧张时可整体跳过本 PR 测试**。PR1/PR2 测试不受影响。

### 4.1 测试目标

验证工具组 L1 摘要生成（纯文本，不调 LLM）、三级注入标记格式、版本路由排序、去重适配。

### 4.2 测试策略

- **工具组 L1 纯文本拼接**：不调 LLM，纯字符串操作；用 mock LLM call_count 验证
- **单元测试 seed DB**：直接 INSERT turn_cache 行（新格式），调 `assemble()` 验证摘要标记
- **正则验证**：用 `pytest.mark.parametrize` 合并新旧正则对比

### 4.3 测试用例

#### R5-summary — 三级摘要

> **`l1_text` JSON 完整 schema：** `{"group_intent": str, "group_result": str, "tool_count": int, "state": Literal["ok","error","blocked","cancelled"], "finish_reason?": str}`。`finish_reason` 为可选顶级 key（仅截断场景时出现），与其余字段平级。

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 41 | `generate_group_summary` 纯文本拼接 | 提供 thought + tool_results | 调 `generate_group_summary()` | 返回 dict：`{"group_intent": str, "group_result": str, "tool_count": int, "state": Literal["ok","error","blocked","cancelled"]}` |
| 42 | 工具组 L1 写入 assistant{tc}.l1_text | flush 含 1 组 API | 查 assistant 行 l1_text | JSON 含 `group_intent`/`group_result`/`tool_count`/`state` |
| 43 | tool 行 l1_text 含正向字段 + 无 thought_process | flush 后 | 查 tool 行 l1_text JSON | 含 `tool_name`/`result`/`status`；无 `thought_process`（已在上层 pop） |
| 44 | 不调 LLM | 调用 `generate_group_summary` | 检查 mock `_call_llm_for_l1` call_count | call_count 不变 |
| 45 | 对话轮 L1 签名不变 | `_call_llm_for_l1` | `inspect.signature()` 对比 | 返回类型 `Tuple[str, str]`，参数名与 v4.7.1 一致 |

#### R6-injection — 三级注入

| # | 场景 | 前置条件 | 步骤 | 预期 |
|---|------|---------|------|------|
| 46 | `assemble()` 含三类标记 | seed DB 含对话轮 + 工具轮数据 | `assemble()` | 返回列表中含 `[~/N/0]`、`[~/N/g]`、`[~/N/M]` 三类标记 |
| 47 | 工具组格式正确 | 场景 46 的 `/g` 标记 | 检查标记文本 | `"工具组：{intent}→{result}（{N}个，{state}）"` 格式 |
| 48 | `_CA_TAG_RE` 正则匹配（参数化） | 编译新旧正则 | `@pytest.mark.parametrize("tag, new_expect, old_expect", [("[~/1/0]", True, True), ("[~/1/g]", True, False), ("[~/1/2]", True, True)])` | 新正则匹配全部三类；旧正则不匹配 `/g` |
| 49 | `_deduplicate_messages` 处理 `[~/N/g]` | seed DB 含重复 `/g` 行 | `assemble()` → 去重结果 | 重复行被替换为 `(同[~/N/g])` |
| 50 | 版本路由排序：v5 → 三元组排序 | v5 格式 seed DB 含乱序多组 | `_rebuild_messages_from_cache` | 消息顺序：先按 turn_index，再按 api_call_count，再按 seq_index |
| 51 | 版本路由排序：v4 → 旧排序 | v4 seed DB | `read_session() readonly` | 按 `turn_index ASC` |
| 52 | `_compute_turn_plan_v2` 三级判定 | seed DB 含对话轮 + 工具组 + 工具轮 | 调 `assemble()` | TurnPlanEntry 中 `api_call_count` 有值，`turn_type="tool_group"` 条目出现 |

---

## 5. Fixture 定义

### 5.1 新增 Fixture

| Fixture | 签名 | 说明 |
|---------|------|------|
| `simulate_tool_call_turn` | `(ca_engine, user_msg: str, assistant_with_toolcalls: dict, tool_results: list[dict]) → int` | 封装完整工具调用时序：post_api_response → pre_tool_call × N → post_tool_call × N → flush。返回写入的行数。`assistant_with_toolcalls` 为 dict（含 `content` + `tool_calls` 键），类型与 Hermes `NormalizedResponse` 一致，无需额外 import。 |
| `legacy_db_v4` | `(tmp_path, with_data=False) → SQLiteStore` | 创建 schema_version=4 的旧 DB，`with_data=True` 时灌入 2 轮对话 + 1 次工具调用的种子数据。供 R7 readonly 测试 |
| `engine_v5` | `(tmp_path) → ContextAssembler` | 同 `ca_engine` 但 store schema 升级为 v5（验证新 DB 默认 schema 版本）。无法复用 `ca_engine`（后者硬编码 v4 schema），保留独立 fixture |

### 5.2 `simulate_tool_call_turn` 伪代码

```python
_REQ_COUNTER = 0  # 自增计数器，替代 time.time 确保确定性

@pytest.fixture
def simulate_tool_call_turn(ca_engine):
    def _sim(user_msg, assistant_with_toolcalls, tool_results):
        # tool_results: list of {"tool_call_id", "tool_name", "args", "result", "status", "duration_ms"}
        global _REQ_COUNTER
        _REQ_COUNTER += 1
        api_request_id = f"req_{_REQ_COUNTER}"
        api_call_count = 1  # 递增逻辑按引擎当前状态
        
        # 1. post_api_response
        ca_engine._on_api_response(
            api_request_id=api_request_id,
            thought=assistant_with_toolcalls.get("content", ""),
            tool_defs=assistant_with_toolcalls.get("tool_calls", []),
            api_call_count=api_call_count,
            turn_index=ca_engine._turn_counter + 1,
        )
        
        # 2. pre/post_tool_call for each
        for tr in tool_results:
            ca_engine._on_pre_tool_call(
                tool_call_id=tr["tool_call_id"],
                api_request_id=api_request_id,
            )
            ca_engine._on_post_tool_call(
                tool_call_id=tr["tool_call_id"],
                tool_name=tr["tool_name"],
                result=tr["result"],
                status=tr["status"],
                duration_ms=tr["duration_ms"],
                args=tr["args"],
            )
        
        # 3. flush
        count = ca_engine.flush_tool_buffer()
        # 异步 _run_c_stage 同步：用 wait_for_pending 替代固定 sleep
        ca_engine.wait_for_pending(timeout=30)
        return count
    return _sim
```

### 5.3 取消的 Fixture

| Fixture | 原因 |
|---------|------|
| `_mock_llm_without_thought` | 不需要 — ToolSummarizer 不调 LLM |
| `_mock_no_llm` | 不需要 — 用例 #44 直接检查 mock 调用计数即可，无需独立 fixture |

---

## 6. 测试文件改动清单

### 6.1 `tests/test_tool_buffer.py`（**新增**，~11 用例）

覆盖 R2+R3+R4 的 Buffer 层全生命周期。⚠️ 模块级建议加 `importorskip` 保护，因为依赖 `_on_api_response` 等新方法。

| 用例 | 场景 | 依赖 Fixture |
|------|------|-------------|
| R3-01 (#26) | 空 buffer 初始状态 | `ca_engine` |
| R3-02 (#27) | 单 API 单工具存储 | `ca_engine`, `simulate_tool_call_turn` |
| R3-03 (#28) | 单 API 多工具归组 | 同上 |
| R3-04 (#29) | 多 API 同轮多 key | 同上 |
| R3-05 (#30) | flush 后 buffer 为空 | `ca_engine`, `simulate_tool_call_turn` |
| R3-06 (#31) | flush 行数验证（单组 N=1） | 同上 |
| R3-07 (#32) | flush 行数验证（多组 N=2） | 同上 |
| R3-08 (#33) | flush 多 API 排序写入 | 同上 |
| R3-09 (#34) | 悬挂清理 reset() | `ca_engine` |
| R3-10 (#35) | post_tool_call 容错 auto-create（引擎层） | `ca_engine` |
| R4-01 (#36) | flush 只写工具行（纯对话轮不写） | `ca_engine` |
| R4-02 (#39) | role='tool' 行数 = 累计工具调用数 | 累计多次工具轮 |

### 6.2 `tests/test_store.py`（扩展，~19 新增用例）

| 当前 | 新增 | 测试内容 |
|------|------|---------|
| 末尾追加 | +4 | v5 schema 列验证（turn_cache + turn_plan + user_version） |
| 末尾追加 | +2 | `write_turn()` 向后兼容（旧签名→api_call_count=0,seq_index=0） |
| 末尾追加 | +2 | `write_tool_group()` 事务写入（多行写入后回滚验证） |
| 末尾追加 | +4 | readonly 模式（无 WAL、无 _meta 写入、mtime 不变、无 checkpoint 线程） |
| 末尾追加 | +2 | 版本路由排序（v5 三元组、v4 旧排序） |
| 末尾追加 | +2 | 空文件 readonly / read_session 返回空列表 |
| 末尾追加 | +2 | v4 旧 DB 只读读取 + 同一进程新旧隔离 |

### 6.3 `tests/test_plugin.py`（扩展，~5 新增用例）

> 注意：`post_tool_call` 容错 auto-create 已在引擎层测试（`test_tool_buffer.py` #35），插件层不再重复覆盖。

| 位置 | 新增 | 测试内容 |
|------|------|---------|
| `TestRegister` 末尾追加 | +1 | register() 注册 8 个钩子（含 3 新钩子） |
| `TestPostLlmCall` 末尾追加 | +2 | post_llm_call 调用顺序：flush → async process_turn_async |
| 新增 `TestToolHooks` 类 | +2 | post_api_request / pre_tool_call 分发正确性 |

### 6.4 `tests/test_c.py`（扩展，~6 新增用例）

| 位置 | 新增 | 测试内容 |
|------|------|---------|
| 末尾追加 | +2 | `_run_c_stage` 职责分离（纯对话轮 2 行 / 工具轮 1 行） |
| 末尾追加 | +2 | 工具组 L1 JSON 格式验证 |
| 末尾追加 | +1 | tool 行 l1_text 无 thought_process |
| 末尾追加 | +1 | 对话轮 L1 签名不变（`inspect.signature` 验证） |

### 6.5 `tests/test_a.py`（扩展，~7 新增用例）

| 位置 | 新增 | 测试内容 |
|------|------|---------|
| 末尾追加 | +3 | assemble() 含三类标记 [~/N/0]/[~/N/g]/[~/N/M] |
| 末尾追加 | +1 | 工具组格式化 "工具组：{intent}→{result}（{N}个，{state}）" |
| 末尾追加 | +1 | `_CA_TAG_RE` 正则参数化测试（#48，合并原 #50/#51） |
| 末尾追加 | +1 | `_deduplicate_messages` 处理 [~/N/g] |
| 末尾追加 | +1 | 版本路由排序 |

### 6.6 `tests/test_v440.py`（适配修改，~0 新增）

- 现有工具轮测试的主键格式从 `(turn_index, turn_type, tool_sub_index)` 适配 `(turn_index, api_call_count, seq_index)`
- 具体改动：确认测试中 `write_turn` 调用签名需加 `api_call_count`/`seq_index` 字段（使用向后兼容层自动填充）
- **messages→conversation_history 适配**：`test_v440.py` 中 3 个现有用例（TC_C_015/C_020/C_025）的 `process_turn_async("", "", messages=messages)` 改为 `process_turn_async("", "", conversation_history=messages)`，与 TC#25 的 `messages` 参数移除一致

---

## 7. 环境与风险

### 7.1 环境要求

| 资源 | 要求 |
|------|------|
| Python | 3.10+ |
| 依赖 | 零新依赖（纯 stdlib + pytest） |
| 模型 | 全部 mock（autouse `_mock_embed` + `_mock_llm`） |
| 数据库 | SQLite（WAL 仅写模式） |
| 磁盘 | 每个测试 < 10MB（临时 DB）|
| SQLite 版本 | 只读模式测试需 `>= 3.22.0`（`?mode=ro` 支持） |

### 7.2 技术风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| Hermes hook 不可用 | PR2 依赖 actual hook 验证 | `simulate_tool_call_turn` fixture 封装 hook 时序，不依赖 Hermes 运行时 |
| readonly 模式 SQLite 版本差异 | URI path `?mode=ro` 在旧 SQLite 不支持 | 用例 #9–#16 标记 `pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 22, 0))` |
| 多轮 flush 时序竞态 | C-stage 异步线程可能未完成 | `wait_for_pending(timeout=30)` 时序同步，**不用 `time.sleep`** |
| PR3 测试依赖 seed DB 格式 | seed DB 必须用新 schema | `engine_v5` fixture 确保 schema 版本正确 |
| 旧 DB 数据迁移重建 | PR1 不做数据迁移 | 验收标准明确：旧 DB readonly（`?mode=ro`），不迁移 |
| 核心方法尚未实现 | PR2/PR3 测试文件无法 import | 测试文件加模块级 `importorskip` 保护，PR 实现合入前不运行 |
| Fixture 非确定性 | `api_request_id` 用时间戳 | 改用模块级自增计数器 `_REQ_COUNTER`（见 §5.2） |
| CI 环境 sleep 脆弱 | 固定等待可能不足 | 全量替换为 `wait_for_pending(timeout=30)`，移除 `time.sleep` |
| `NormalizedResponse` 类型不存 | fixture 签名引用未知类型 | Fixture 改用 dict 类型 `assistant_with_toolcalls: dict`，不依赖 Hermes 类型 |

### 7.3 不做

- 不写 Hermes 宿主集成测试（mock 层足够）
- 不写端到端 Ollama 测试（`test_system.py` 已 skip）
- 不做性能基准测试（非本版本目标）
- 不引入额外测试依赖
- 不重写现有 mock fixture（保留 `_mock_embed` + `_mock_llm`）
- 不验证 ooda_parser/post_process.py 等不变文件（由 CI git diff 门禁覆盖，非单元测试职责）

---

## 8. 实施计划

### 阶段 1：PR1 测试（Store 层 + readonly）

1. 实现 `test_store.py` 新增用例（v5 schema 验证 + user_version）
2. 实现 `test_store.py` readonly 模式测试用例（含 skipif 标记）
3. 实现 `legacy_db_v4` fixture
4. 执行 PR1 store 测试 → 全部绿色

### 阶段 2：PR2 测试（Buffer + Hook）

5. 创建 `test_tool_buffer.py` 新增文件（含模块级 importorskip）
6. 实现 `simulate_tool_call_turn` fixture（自增计数器，非 time.time）
7. 实现 Buffer 全生命周期测试
8. 扩展 `test_plugin.py` 钩子注册测试
9. 扩展 `test_c.py` 职责分离测试
10. 执行 PR2 测试 → 全部绿色

### 阶段 3：PR3 测试（摘要 + 注入）⏭️ 可跳过

11. 扩展 `test_a.py` 三级注入测试（含参数化正则测试）
12. 适配 `test_v440.py` 新主键格式
13. 全量回归：`python -m pytest tests/ -v --ignore=tests/test_system.py`

---

## 9. 验收标准

### 9.1 测试覆盖率

| 维度 | 目标 | 测量方式 |
|------|------|---------|
| 需求覆盖 | R1-R7 每个需求≥1 个测试用例 | 对照 §1 覆盖矩阵 |
| 新增用例数 | ≥40 个 | `python -m pytest tests/ --collect-only \| grep "test_" \| wc -l` |
| 通过率 | 100%（mock 环境） | `pytest tests/ --ignore=tests/test_system.py -q \| tail -1` |

> **注：**「新增用例数」基线差分法——PR 合入前运行 `pytest --collect-only -q | tail -1` 记录旧总数，合入后运行同命令计算差值。

### 9.2 质量门禁

- 所有测试在 mock 环境中 3 次连续运行通过（`pytest -x --tb=short`）
- 无新文件描述符泄漏（`fd_checker.assert_no_leak(max_delta=10)`）
- 旧测试回归 0 fail（`pytest tests/legacy/test_dedup.py` 通过）

---

## 10. 多视角审查处理记录

### 第二轮（21 项，新标准）→ 全部已关闭

#### 完整性视角（5 项，新标准——读了 analysis.md）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| A-4 | 行数公式缺少总行数端到端断言（M+3 / N+M+2） | ✅ **采纳** | 新增用例：工具轮完整流程（flush + `_run_c_stage`）后 `SELECT COUNT(*)` 验证 M+3（单组）和 N+M+2（多组） |
| A-5 | `finish_reason="length"` 子场景未区分（截断工具调用 vs 截断纯文本） | ✅ **采纳** | 新增两个用例：(a) 工具调用截断→flush 仍执行 + `l1_text` 含 `finish_reason:"length"` (b) 纯文本截断→无 buffer 写入，final 行标记截断 |
| A-6 | tool 行 l1_text 缺少正向格式验证（仅验证了无 thought_process） | ✅ **采纳** | TC#43（原）追加正面断言：`assert "tool_name" in l1`, `assert "result" in l1`, `assert "status" in l1` |
| A-7 | 悬挂清理只测了 reset() 路径，未测 flush 自检测路径 | ✅ **采纳** | 新增用例：buffer 有残留 → 触发新一轮 flush → 验证告警日志且旧数据被覆盖 |
| A-8 | `_on_post_tool_call` 容错未验证 args 参数正确存入 | ✅ **采纳** | TC#35（原）追加断言：自动创建条目后 buffer 中 args 与传入一致 |

#### 一致性视角（4 项，新标准——读了源码 + codegraph）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| B-7 | test_store.py 用例数三处不统一：源需求 ~12 vs 测试需求 §1 ~19 vs 实际 TC 列表 18 | ✅ **采纳** | 源需求 `req.md` §4.1 同步修正为 ~18；§1 矩阵与 §2.3 实际对齐 |
| B-8 | 源需求 fixture 签名（`engine`/`NormalizedResponse`）未同步更新 | ✅ **采纳** | 源需求 `req.md` §4.2 同步修正为 `ca_engine`/`dict` |
| B-9 | §6.2 readonly 分组 +3 但实际 TC#9-12 有 4 条 | ✅ **采纳** | §6.2 readonly 分组改为 +4 |
| B-10 | 矩阵 ~56 vs 实际 52，~ 标记累积偏差 | ✅ **采纳** | §1 矩阵对齐实际值（见更新后矩阵） |

#### 必要性视角（6 项，新标准——读了现存测试文件）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| C-7 | TC#19 与现有 `test_plugin.py:test_registers_five_hooks` 硬冲突（call_count==5 断言） | ✅ **采纳** | 修改现有测试 `call_count` 断言 5→8，钩子列表追加 3 个工具钩子；TC#19 改为标注「修改现有测试」 |
| C-8 | TC#24 与 `test_c.py:T1` `finish_reason="length"` 场景重叠 | ✅ **采纳** | 合并：TC#24 复用 T1 的 mock 设置，追加 `l1_text` 断言 |
| C-9 | TC#31/#32 flush 行数验证可参数化合并 | ✅ **采纳** | `@pytest.mark.parametrize("m,n,expected", [(3,1,5),(4,2,7)])` 合并为单测 |
| C-10 | TC#39/#40 工具行数验证断言粒度重叠 | ✅ **采纳** | 合并为单测：先测累计再测增量，参数化前后对比模式 |
| C-11 | `test_v440.py` 3 个现有用例用 `messages=messages` 与 TC#25 冲突 | ✅ **采纳** | §6.6 增加说明：3 个用例从 `process_turn_async("", "", messages=messages)` 改为 `conversation_history=messages` |
| C-12 | TC#22 可追加到现有 `test_calls_process_turn_async` 而非新建 | ✅ **采纳** | TC#22 改为在现有测试中追加 `flush_tool_buffer` 调用顺序断言，减少重复 mock 设置 |

#### 可验证性视角（6 项，新标准——读了 conftest.py + 源码）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| D-12 | TC#10 预期「0 或表不存在」不可达（v4 创建时 `_meta` 必有 schema_version 行） | ✅ **采纳** | 改为「`SELECT value FROM _meta WHERE key='schema_version'` 返回 `'4'`」 |
| D-13 | TC#45 缺少 v4.7.1 黄金签名基线，`inspect.signature()` 无从对比 | ✅ **采纳** | 测试代码中保存 `_EXPECTED_SIGNATURE = "(prev_l1, l2_text)"` 常量作为参考基准 |
| D-14 | §9.1 #2「新增用例数」测量命令误统计全量而非增量 | ✅ **采纳** | 改为基线差分法：合入前 `--collect-only` 记录旧总数，合入后计算差值 |
| D-15 | TC#23/#24 的 `l1_text` 组合 JSON schema 未定义（截断时 finish_reason 与工具组字段如何合并） | ✅ **采纳** | §4.3 补充 `l1_text` JSON 完整 schema 定义：`{"group_intent", "group_result", "tool_count", "state", "finish_reason?"}`，`finish_reason` 为可选顶级 key |
| D-16 | `_REQ_COUNTER` 在 pytest-xdist 并行下可能 `api_request_id` 重复 | ❌ **不采纳** | 当前串行执行无问题。若引入 xdist 时改用 `uuid4().hex[:8]` |
| D-17 | `legacy_db_v4` fixture 创建策略未选型（PR1 后 `_SCHEMA_VERSION` 改为 5，如何降级） | ✅ **采纳** | 实现方案：在 store.py 中保留 `force_version` 参数供测试使用，或 fixture 内用 `sqlite3` 直接执行 v4 DDL |

---

### 第一轮（26 项）→ 全部已关闭

#### 完整性视角（3 项）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| A-1 | TC#1/#2 缺少 `PRAGMA user_version=5` 断言 | ✅ **采纳** | TC#1 预期追加 `user_version=5` |
| A-2 | 多 API 场景（N>1）行数验证未覆盖 | ✅ **采纳** | 新增 TC#32（N=2,M=4 → 7 行公式验证） |
| A-3 | ooda_parser 等不变文件的完整性未验证 | ❌ **不采纳** | CI git diff 门禁覆盖，非单元测试职责。已写入 §7.3 |

#### 一致性视角（6 项）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| B-1 | R7 用例数 §1:~6 vs §2.3:10 不匹配 | ✅ **采纳** | §1 矩阵 R7 改为 ~10 |
| B-2 | R2 用例数 §1:~5 vs §3.4:8 不匹配 | ✅ **采纳** | §1 矩阵 R2 改为 ~8 |
| B-3 | session_id 是否为主键成分不明确 | ✅ **采纳** | 源文档 `(turn_index, api_call_count, seq_index)` 仅定义语义主键，SQL 实际主键含 `session_id` 已正确；TC#3 说明中保持含 `session_id` 的完整写法 |
| B-4 | `api_call_count`/`api_count` 命名不统一 | ✅ **采纳** | 全篇统一为 `api_call_count`/`seq_index` |
| B-5 | `simulate_tool_call_turn` fixture 参数名 `engine` vs `ca_engine` | ✅ **采纳** | 统一为 `ca_engine`（与 conftest.py fixture 名一致）|
| B-6 | test_tool_buffer.py 标题 ~10 与列表中 11 个不匹配 | ✅ **采纳** | 标题改为 ~11 |

#### 必要性视角（6 项）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| C-1 | TC#35"无锁断言通过"无实际验证价值 | ✅ **采纳** | 删除该用例 |
| C-2 | TC#22 与 TC#36 auto-create 容错重复 | ✅ **采纳** | 合并：删除插件层 TC#22，保留引擎层 TC#36（现 #35） |
| C-3 | `_mock_no_llm` fixture 不必要 | ✅ **采纳** | 删除 fixture；用例 #44（现 #44）直接检查 mock call_count |
| C-4 | `engine_v5` 与 `ca_engine` 可参数化 | ❌ **不采纳** | `ca_engine` 硬编码 v4 schema，参数化需改 conftest.py 影响现有 200+ 测试。保持独立 fixture，加备注说明无法复用。 |
| C-5 | `test_tool_buffer.py` 新文件必要性 | ❌ **不采纳** | 关注点分离合理，保持新文件。 |
| C-6 | P1 优先级区分不足 | ✅ **采纳** | §4 标题加注"⏭️ 可跳过"；§8 实施计划加注；#50/#51 合并为参数化测试 |

#### 可验证性视角（11 项）

| # | 意见 | 裁决 | 对应改动 |
|---|------|------|---------|
| D-1 | TC#8"返回数据"模糊 | ✅ **采纳** | 改为"返回 dict 且 `dict["l0_text"]` 为非空字符串" |
| D-2 | TC#13"正常返回"不可测量 | ✅ **采纳** | 明确：`read_session()` 返回非空列表，`assemble()` 返回含 CA 标记的 messages |
| D-3 | TC#14"互不干扰"不可直接断言 | ✅ **采纳** | 拆为两个断言：(a) v4 文件 mtime 不变 (b) v5 行数增加 |
| D-4 | TC#35（原）"不触发 assert"不精确 | ✅ **采纳** | 已删除该用例（见 C-1） |
| D-5 | TC#42"估算"不可断言 | ✅ **采纳** | 删除独立 TC#42，合并到 #25（签名 + conversation_history 传递验证） |
| D-6 | TC#47"调用路径一致"模糊 | ✅ **采纳** | 改为 `inspect.signature` 验证返回类型和参数签名 |
| D-7 | `time.time()` 在 fixture 中非确定性 | ✅ **采纳** | 改用模块级自增计数器 `_REQ_COUNTER` |
| D-8 | `time.sleep(0.5)` 脆弱 | ✅ **采纳** | 全量替换为 `wait_for_pending(timeout=30)`，移除 `time.sleep` |
| D-9 | 核心方法未实现，测试无法独立运行 | ✅ **采纳** | PR2 测试文件加模块级 importorskip 保护；§7.2 补充风险项 |
| D-10 | skipif 条件未绑定到具体用例 | ✅ **采纳** | §2.3 R7 用例表前加 skipif 脚注 |
| D-11 | `NormalizedResponse` 类型未定义 | ✅ **采纳** | fixture 签名改用 `dict` 类型，不依赖 Hermes 类型 |

---

## 自检
- [x] 每个需求点有对应测试用例（§1 覆盖矩阵，R1-R7 全覆盖）
- [x] 每个测试用例有明确的前置条件和可断言的预期结果
- [x] Fixture 签名完整定义（§5）
- [x] 环境要求和风险已评估（§7）
- [x] 实施计划有步骤顺序（§8）
- [x] 验收标准可定量验证（§9）
- [x] 多视角审查第一轮全部 26 项已闭环（§10.1）
- [x] 多视角审查第二轮（新标准）全部 21 项已闭环（§10.2）
