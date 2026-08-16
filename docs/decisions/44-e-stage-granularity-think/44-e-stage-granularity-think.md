# 决策 44：E 阶段细颗粒度采集 + think 录入 + Fct 多事务 OODA 支撑

> 版本：v1.0（需求规格 + 技术方案合一）｜状态：进入代码构建
> 来源：用户 2026-08-17 指令——参考 CA 的 DSH 版（`~/dsh-workspace/ca-v7`）与 EFLR（`~/projects/courier3/eflr`）改进 Hermes 版 E 阶段。
> 调查基线：三名调查员子 agent 报告（DSH ca-v7 / EFLR egr_courier / Hermes CA 现状）。

## 1. 需求（R 系列）

### R1 从最接近云端 LLM 响应的源头取数
- R1.1 E 阶段采集主锚点从「turn 级 `post_llm_call`」下沉为「每次 API 调用级 `post_api_request` + `pre_api_request`」，并补齐 `api_request_error`。
- R1.2 可选启用 `on_stream_start / on_stream_delta / on_stream_end` 流式观测（token 级 chunk 计数，旁路 fail-open，不落 chunk 正文）。

### R2 细颗粒度块模型（迁移 EFLR EnvelopeBlocker + DSH OODA_RULES）
- R2.1 turn_stream 每行增加 `block_type`：`user_message / thinking / agent_reply / tool_call_request / tool_call_result`（`api_metadata` 由 llm_calls 表承载，不污染 turn_stream role 语义）。
- R2.2 每行增加 `ooda_stage`，由代码确定性打标（零 LLM、零 HTTP）：`user_message→orient`、`thinking/agent_reply→decide`、`tool_call_request→act`、`tool_call_result→observe`。
- R2.3 一个 API 响应内 `reasoning_content` 与 `content` **不再混叠**：分别写 THINKING 行与 AGENT_REPLY 行；工具占位行语义改为 `tool_call_request`。
- R2.4 纯对话最终回复行在 `post_llm_call` 写为 `agent_reply + is_fin=1 + decide`，并用最近一次 API 调用元数据回填 request_id/provider/model/usage。

### R3 元数据录入（迁移 EFLR MetaMarker + DSH llm_calls）
- R3.1 新增 `llm_calls` 表（per-session DB），每次 API 调用一行：request_seq/request_id/turn/step/seq/provider/model/base_url/api_mode/messages_count/input_chars/reasoning_chars/text_chars/chunk_count/tool_calls_json/usage_json/finish_kind/duration_ms/failure_json/status。
- R3.2 `post_api_request` 全量消费 Hermes 已提供的 payload：provider/model/base_url/api_mode/api_duration/finish_reason/usage 全量 JSON/message_count/response_model。
- R3.3 turn_stream 增加 `request_id/provider/model/reasoning_chars/text_chars/result_chars/error_text/is_fin/metadata_incomplete` 列，供 Fct 无需 join 即可取基础元数据。
- R3.4 provider 推断采用 EFLR 规则表（正则顺序匹配 base_url+model），Hermes 已给 provider 时直接采用。

### R4 think（云端 reasoning）录入（迁移 DSH think_trace K0）
- R4.1 新增 `think_trace` 表，`UNIQUE(session_id, turn, seq)` 指向 turn_stream 的 THINKING 行；只存 `raw_len` 指针 + preview（≤160 字符，仅供调试），**不复制 reasoning 全文**（turn_stream.Elm 即 L2 权威）。
- R4.2 采集规则（DSH K0 + 用户裁定增补）：
  - 含 tool_calls 的 reasoning → `card_kind='decision'`；
  - **事务内首段 think 且无 tool_calls → `card_kind='orient'`，零门槛入卡**（提问后首轮 think 是事务划分的关键线索，不套 800 字门槛；同一事务第二段及以后的无工具短 think 不入 orient）；
  - fin 轮 reasoning 且（raw_len≥800 或命中修正词表 或 同 turn 存在工具错误）→ `card_kind='conclusion'`（长首段 fin think 仍优先 conclusion）；
  - 其余短/非 fin reasoning 不入卡（捡选纪律）。
- R4.2a reasoning 提取全字段：`provider_data.reasoning_content` → 顶层 `.reasoning` → `provider_data.reasoning_details`（OpenRouter `summary/thinking/content/text`）/ `codex_reasoning_items` / `codex_message_items` / `anthropic_content_blocks` → 最后才扫 content 内联 `<think>/<thinking>/<thought>/<reasoning>` 标签；双源去重合并，绝不把普通 content 当 reasoning。
- R4.3 `l1_json/l0_abstract/entities_json/embedding_json` 预留，`status='raw'`；L1 提炼后续走 7.2 模式（每次最多 1 次本地调用、失败保持 raw、fail-open），本轮 E 阶段不调 LLM。

### R5 Fct 多事务 OODA 支撑
- R5.1 F-stage 输入升级为「事务帧」格式：按 turn 分组，按 seq 输出 `[ooda_stage|block_type]` 前缀（旧行无 block_type 自动回退 legacy 文本，兼容历史 DB）。
- R5.2 FCT prompt 增加多事务提示：以 E 阶段 OODA 标记作为分割线索，四节按事务组织，每事务仍输出独立 `<stage_tag>/<core_change>` 对。
- R5.3 不改变 Fct JSON 顶层 schema 与 strand_summaries.ooda_json 四段契约（决策 28/35 已定义），避免破坏 topic_summary 全链路。

### R6 兼容与纪律
- R6.1 SQLite 增量迁移：旧 turn_stream 库 `ALTER TABLE ADD COLUMN`（幂等、缺列才加）；新库 schema 全量含新列；设置 `PRAGMA user_version=1`。
- R6.2 `write_turn_v5` 同内容重放跳过契约保持：核心列比较扩展到新列，旧行 core 以 NULL 补齐比较。
- R6.3 写即落盘、不写 state.db、不硬编码 `~/.hermes`、Elm/Fct/Hdl 术语唯一等 AGENTS.md 约束不变。
- R6.4 所有新 hook 回调 fail-open：异常只 warning，绝不冒泡影响 Hermes 主流程。

## 2. 验收标准（A 系列）

- A1 新 hook 全部注册（13 个）；`hermes plugin list` 无 TypeError。
- A2 旧库迁移：预置 18 列旧 schema 的 DB，打开后自动补列，旧数据可读。
- A3 E 阶段测试：reasoning+content 双块各落一行、块类型与 ooda_stage 正确、llm_calls 全字段落库、think_trace decision/orient/conclusion 门槛正确、纯对话不产生 THINKING 空行。
- A4 全量 pytest 基线 1008 passed 之外新增用例全绿，既有用例除「hook 计数 8→13」契约更新外零新增失败。
- A5 Fct 帧格式化纯函数：多事务输入按事务/阶段分组输出，历史行回退 legacy。
- A6 `tsc` 不适用（Python 项目）；`python -m py_compile` 全部新文件通过。

## 3. 技术方案

### 3.1 数据流

```
pre_api_request ──┐
on_stream_*  ─────┤  (旁路计数, fail-open)
                  ▼
post_api_request ─→ MetaMarker.extract ─→ write llm_calls
                  └─→ E-stage 拆块 ──→ turn_stream(thinking/agent_reply/tool_call_request)
                                      └─→ think_trace(decision | orient)
post_tool_call ──→ turn_stream(tool_call_result) + result_chars/error_text
post_llm_call  ──→ turn_stream(agent_reply fin) + think_trace(conclusion) + F-stage
```

### 3.2 新文件/改动面

| 文件 | 改动 |
|---|---|
| `ca/blocks.py`（新） | BlockType 常量、detect_block_type、map_ooda_stage、format_transaction_frames |
| `ca/meta_marker.py`（新） | provider 规则表、request/response 元数据提取、usage 归一化 |
| `ca/think_collect.py`（新） | DSH K0 规则的 Python 移植：修正词表、门槛判定、preview |
| `ca/store.py` | turn_stream 新列 + 迁移；llm_calls/think_trace 表；写入/读取函数 |
| `ca/e_stage.py` | EStageMixin v7：拆块写入、llm_calls、think_trace、fin 回填 |
| `ca/f_stage.py` | 结构化事务帧输入 + prompt 多事务提示 |
| `ca/prompts.py` | FCT prompt 多事务提示 |
| `ca/config.py` | CA_STREAM_OBSERVE_ENABLED、CA_THINK_MIN_REASONING_CHARS、CA_FCT_STRUCTURED_INPUT |
| `__init__.py` | 注册 5 个新 hook + stream 状态 + `_last_api_meta` |
| `plugin.yaml` | hooks 列表 8→13；config_schema 补新开关 |
| `docs/INDEX.md` | 决策表补 44 |

### 3.3 turn_stream 新列（可空，缺列迁移）

```sql
block_type TEXT,
ooda_stage TEXT,
request_id TEXT,
provider TEXT,
model TEXT,
reasoning_chars INTEGER,
text_chars INTEGER,
result_chars INTEGER,
error_text TEXT,
is_fin INTEGER DEFAULT 0,
metadata_incomplete INTEGER DEFAULT 0
```

### 3.4 llm_calls 表（按 Hermes request_id UPSERT，规避 stream 乱序）

Hermes `api_request_id = f"{turn_id}:api:{api_call_count}"`，retry 复用同一 id；`on_stream_*` 异步 worker 与 `post_api_request` 同步 invoke **无顺序保证**。因此主键用 request_id，stream 补丁与 post_api 主写都走 UPSERT，谁先到都不丢。

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_seq INTEGER,
  turn INTEGER, step INTEGER, seq INTEGER,
  provider TEXT, model TEXT, purpose TEXT, reasoning_effort TEXT,
  base_url TEXT, api_mode TEXT,
  messages_count INTEGER DEFAULT 0,
  input_chars INTEGER DEFAULT 0,
  reasoning_chars INTEGER DEFAULT 0,
  text_chars INTEGER DEFAULT 0,
  chunk_count INTEGER DEFAULT 0,
  tool_calls_json TEXT,
  usage_json TEXT,
  finish_kind TEXT,
  duration_ms INTEGER,
  failure_json TEXT,
  status TEXT NOT NULL DEFAULT 'streaming',
  created_at REAL,
  UNIQUE(session_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_request_id ON llm_calls(request_id);
```

写入规则：
- `patch_llm_call_stream()`：仅 UPSERT `request_id/turn/model/provider + chunk_count/reasoning_chars/text_chars`，不触碰 post_api 权威列。
- `write_llm_call()`：post_api 主写，合并 pending stream 计数（`max(pending, computed)`）后 UPSERT 全列。
- retry 同 request_id latest-wins，符合 Hermes 重试语义。

### 3.5 think_trace 表

```sql
CREATE TABLE IF NOT EXISTS think_trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn INTEGER, step INTEGER, seq INTEGER,
  txn_id INTEGER, topic_id INTEGER,
  source_kind TEXT NOT NULL DEFAULT 'cloud_think',
  card_kind TEXT,
  call_id TEXT, tool_name TEXT,
  question_text TEXT NOT NULL DEFAULT '',
  l0_abstract TEXT, l1_json TEXT, entities_json TEXT, embedding_json TEXT,
  raw_len INTEGER NOT NULL DEFAULT 0,
  preview TEXT,
  status TEXT NOT NULL DEFAULT 'raw',
  created_at REAL, updated_at REAL,
  UNIQUE(session_id, turn, seq)
);
```

## 4. Hermes 接口契约结论（补充调查，2026-08-17）

1. `on_stream_start/on_stream_delta/on_stream_end/pre_api_request/api_request_error` 均在 `VALID_HOOKS`（hermes_cli/plugins.py:156-190）；`ctx.register_hook` 未知 hook 仅 warning 不抛错；stream hook 与普通 hook 共用 `PluginManager._hooks`，`agent/plugin_stream_hooks._registered_callbacks` 读同一表。
2. stream 回调经 daemon worker `dispatcher.callback(**payload)` 直调，**不做签名裁剪** → 回调必须 `**kwargs`。队列 1024，满 drop-oldest；worker 异常仅 warning。
3. `post_api_request.assistant_message` 是未净化 `NormalizedResponse`：`content/tool_calls/finish_reason/reasoning/provider_data`。reasoning 全量在 `.reasoning`（归一化）与 `provider_data["reasoning_content"]/["reasoning_details"]`，与 `plugins.stream_reasoning_deltas`（默认 False）无关；该开关只控制 reasoning delta 流式事件。
4. `post_api_request.usage` 是 CanonicalUsage summary，键为 `input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/reasoning_tokens/request_count/prompt_tokens/total_tokens`；**没有 completion_tokens/cached_tokens** → turn_stream 旧列映射：`usage_prompt_tokens=prompt_tokens`，`usage_completion_tokens=output_tokens`（并兼容旧 dict 键）。
5. `pre_api_request` 每 retry 一次；`request["body"]["messages"]` 为净化视图（敏感键脱敏、8000/200 截断、总长 50k 收缩）。`api_request_error` 每失败尝试一次，`error` 为 `{"type","message"}` 嵌套 dict。
6. `on_stream_delta(kind="text")` 已过 think/context 双重脱敏；reasoning delta 未脱敏但受配置门控。`on_stream_*` payload 无 api_request_id，CA 用 `f"{turn_id}:api:{iteration}"` 重建 request_id。
7. `register(ctx)` 进程内一次；force reload 重 import 模块（模块级状态自然清零）。plugin.yaml `hooks/provides_hooks` 不参与运行时校验，但为过 `hermes plugins doctor` 应同步 13 个 hook。
8. 既有测试用 MagicMock ctx 即可把 hook 计数契约更新为 13。

## 5. 实施顺序（TDD）

1. 落新测试（`test_blocks/test_meta_marker/test_think_collect/test_store_v7_meta/test_e_stage_v7/test_f_stage_v7_frames`）+ 更新 plugin hook 计数测试 → RED。
2. 实现 `ca/blocks.py`、`ca/meta_marker.py`、`ca/think_collect.py`。
3. 实现 store 迁移 + llm_calls/think_trace + write/read 函数。
4. 改造 `ca/e_stage.py` 与 `__init__.py` 5 个新 hook。
5. F-stage 事务帧输入 + prompt 多事务提示 → GREEN。
6. 全量 pytest + 修复回归 → 验证诊断。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| `on_stream_*` 与 `post_api_request` 到达乱序 | stream 只做 llm_calls chunk 计数 patch（不存在则幂等建最小行），不参与 turn_stream 权威写入 |
| 旧库迁移 ALTER 失败 | 逐列 try/except + warning，读路径兼容缺列 |
| F-stage 输入格式变化影响既有测试 | 历史行无 block_type 走 legacy 分支；既有 mock 测试不感知 |
| reasoning 敏感数据 | 全量留在 per-session DB，不复制进共享 DB/OV；preview≤160 |
| hook 计数 8→13 影响插件测试 | 作为本决策契约同步更新测试 |
