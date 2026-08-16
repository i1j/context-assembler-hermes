# 决策 44 测试需求（测试线）

> 对象：docs/decisions/44-e-stage-granularity-think/44-e-stage-granularity-think.md 的 R1-R6。
> 策略：TDD——先落测试用例（RED），再改业务代码（GREEN）。Python 项目，`pytest -q`。

## 1. 新增测试文件映射

| 新测试文件 | 覆盖需求 | 关键断言 |
|---|---|---|
| `tests/unit/test_blocks.py` | R2/R5 | detect_block_type 8 类优先级；map_ooda_stage 全映射；format_transaction_frames 多事务分组 + legacy 回退 |
| `tests/unit/test_meta_marker.py` | R3 | provider 规则顺序匹配/未知 fallback；usage 归一化 dict+Namespace；request/response 提取 |
| `tests/unit/test_think_collect.py` | R4 | 修正词表；decision 门槛（含 tool_calls）；conclusion 门槛（≥800/修正/工具错误）；preview≤160；raw_len 指针 |
| `tests/store/test_store_v7_meta.py` | R3/R4/R6 | 新表 schema 存在；turn_stream 新列存在；旧 18 列库 ALTER 迁移幂等；write_turn_v5 新列核心比较/重放保留 Fct；llm_calls/think_trace 写入与 latest-wins |
| `tests/stage/test_e_stage_v7.py` | R1/R2/R3/R4 | post_api 拆 THINKING/AGENT_REPLY 行；tool_call_request 占位；post_tool 回填 observe+result_chars/error_text；纯对话早退不变；llm_calls 全字段；think_trace decision 落库；post_llm fin 行 metadata+is_fin+conclusion 卡 |
| `tests/stage/test_f_stage_v7_frames.py` | R5 | 带 block_type 的增量行 → 事务帧文本含 `[orient|user_message]` 等前缀；legacy 行回退 |
| `tests/plugin/test_plugin.py`（修改） | R1 | register 计数 8→13 且新 hook 名全在 |

## 2. 契约级断言

- **列迁移**：手工建 18 列旧库（`_SCHEMA_SQL_V50` 旧文本写临时文件）→ `SQLiteStore._get_conn()` → `PRAGMA table_info(turn_stream)` 包含全部 11 个新列；原数据行可 `read_turn_stream_all`。
- **幂等**：同 (session,turn,seq) 同核心（含新列）重放返回 True 且不回抹 Fct/Hdl；核心不同（如 block_type 变）覆盖。
- **llm_calls**：同一 request_id 重放不产生双行（request_seq 递增由调用方控制）；usage_json 存全量 dict（含 total_tokens/cached_tokens 若传入）。
- **think_trace**：UNIQUE(session_id,turn,seq) 同键 INSERT OR REPLACE latest-wins；preview 为字符串且 `len≤160`；DB 行不含 reasoning 原文字段（只能通过 turn/seq 指针回读）。
- **OODA 映射零 LLM**：blocks.py 不 import embedding/requests/ollama；测试 monkeypatch 后行为不变。
- **hook 计数**：`ctx.register_hook` 调用 13 次（含 on_stream_start/on_stream_delta/on_stream_end/pre_api_request/api_request_error）。

## 3. 回归重点（既有基线 1008 passed）

- `tests/store/test_store_v5.py`：write/read 幂等路径（新列默认 NULL）。
- `tests/store/test_store_contract.py`：get_turn_ca_rows 7 列契约**不改**（新增独立 detailed reader）。
- `tests/stage/test_e_stage.py`：thought 行/占位行/usage/reasoning_content 优先级既有行为；如内容拆行导致 seq 断言变化，只改断言、不改契约语义。
- `tests/stage/test_f_stage.py`：LLM mock 下 Fct 写入全链路不受输入格式升级影响。
- `tests/plugin/test_plugin.py`：除 hook 计数契约外全部保持。

## 4. 验收门

```bash
cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler
python -m pytest -q                       # 全量，期望 1008 + 新增全绿
python -m pytest -q tests/unit/test_blocks.py tests/unit/test_meta_marker.py tests/unit/test_think_collect.py tests/store/test_store_v7_meta.py tests/stage/test_e_stage_v7.py tests/stage/test_f_stage_v7_frames.py
```
