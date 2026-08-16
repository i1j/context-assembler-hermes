---
title: select_context 激活线上消息契约修复任务书
status: 已实现并验证（2026-08-16，全量 1034 passed / 1 skipped / 1 xfailed）
version: v1.0
source_files: [__init__.py, ca/a_stage.py, ca/e_stage.py]
decisions: [direction-b, ce-shell-registration]
acceptance_ref: §2
spec_id: /home/i1j/.hermes/profiles/tester/plugins/ca_assembler/wf-state.json
review_ledger: /home/i1j/.hermes/profiles/tester/plugins/ca_assembler/wf-state.json
---

# Codex 实现任务书：select_context 激活线上消息契约修复

> 需求来源：wf-state.json task_line「检查 CA 插件当前代码（CE 壳 select_context 激活等未提交改动）是否符合设计要求，修正发现的 bug」+ 本次审计结论。
> 本任务书只修审计确认的 4 类 bug，不重构其他链路。

## §0 任务摘要

现状：CE 壳 select_context 已激活（未提交改动），`select_context()` 每轮用 `ca/a_stage.py::_build_conv_history_v6` 重建的消息列表在宿主 Hermes 中**绕过**其正常路径的线上字段清理，可能把 schema 外字段发给 provider；且一个防御边界可丢弃当前 user。

目标：
1. 重建消息符合 OpenAI Chat Completions 线上消息 schema（无 `finish_reason`、tool 行无 `name`、`tool_calls[].function.arguments` 为 str）。
2. E-stage 写库时保留 arguments 的 JSON string 形态（新数据根治）。
3. select_context 在重建结果无 user 时 fail-open 返回 request_messages（不丢当前用户消息）。
4. 同步过时注释。

- **验收门槛**：§2 验收映射全过（新增 6 测试转绿）+ 全量 pytest 无新增失败 + §7 tester 验收清单
- **测试基线**：改动前（含本任务新增测试前的代码）`pytest -q -c tests/pytest.ini tests` = 1027 passed, 1 skipped, 1 xfailed（2026-08-16 实测）；新增 6 测试当前红灯（预期）
- **改动前快照**（sha256，2026-08-16 实测，测试文件为测试线已改后的值）：
  - `__init__.py` 0b16543215449b13c699bdfed73dbe3a995c724114085bef4948e5c90cc3d861
  - `ca/a_stage.py` 4ba96b136b605f873575413fb67fa7096e3dbfc99abf16012c3ea263be9829b9
  - `ca/e_stage.py` 7f971510231d1c3d39bf7ba66a60196b36af86ae7a34c0ea411fabd845128462
- **工作区基线**：存在未提交改动（CE 壳激活 + 文档 + 测试），保留不动；本任务只改授权文件。改动前 `git status` 核对。

## §1 部署快照

| 项 | 现状 | 位置 |
|---|---|---|
| select_context 入口 | 重建后仅校验 dict+role，无 user 存在性校验；R10 用 request_messages 反向找 user | `__init__.py` L310-L386 |
| select_context docstring | 声称「尾部无 user 消息（工具轮）时跳过替换」与实际代码不符（代码会在工具轮找到更早的 user） | `__init__.py` L320-L327 |
| pre_llm_call docstring | 仍写「mutation 由 compress 中的 _build_conv_history_v6 替代」 | `__init__.py` L1034-L1040、L1144 |
| _row_to_message | tool 行写 `msg["name"]`；thought 行写 `msg["finish_reason"]`；tool_calls 反序列化后 arguments 可能为 dict | `ca/a_stage.py` L209-L231 |
| _on_api_response_v5 | 把 provider 的 arguments JSON string `json.loads` 成 dict 再存库 | `ca/e_stage.py` L65-L75、L124 |

## §2 需求点 → 验收映射

| ID | 需求 | 验收（断言公式 + 检查时机） |
|---|---|---|
| R1 | thought 行重建的 assistant 消息不得携带 `finish_reason` 字段 | `test_thought_message_omits_finish_reason` 通过；`select_context` 工具轮结果中 `"finish_reason" not in thought` |
| R2 | tool 行重建消息使用 Hermes 内部字段 `tool_name`，不得使用 schema 外 `name` | `test_tool_has_call_id_and_tool_name` 通过；`result[2]["tool_name"]=="f1"` 且 `"name" not in result[2]` |
| R3 | `tool_calls[].function.arguments` 在 A-stage 输出中必须是 str（JSON string），兼容历史 dict 行 | `test_tool_call_arguments_normalized_to_json_string` 通过；`isinstance(raw_args, str)` 且 `json.loads(raw_args)=={"x":1,"y":"二"}` |
| R4 | E-stage 存库的 `tool_calls_json` 中 arguments 保持 str（合法 JSON string 原样，非法 JSON 或非 str 归一化为 `"{}"`） | `test_tool_calls_arguments_stored_as_json_string` 通过；解析 DB 行后 `isinstance(arguments, str)` |
| R5 | select_context 重建结果不含 user 消息时必须 fail-open 返回 request_messages（防 bg-only DB 丢掉当前用户） | `test_T16_bg_only_rows_fallback_to_request_messages` 通过；`result is request_messages` |
| R6 | 工具轮（request 尾部是 tool 消息、user 不在尾部）R10 仍替换当前轮 user content 注入；重建消息满足 R1-R3 | `test_T15_tool_round_wire_shape_and_current_user` 通过 |
| R7 | 注释与实现一致（select_context 驱动，不是 compress 驱动） | grep `__init__.py` 无「由 compress 从 DB 重建」等旧表述；docstring 描述与代码分支一致 |

## §3 接口契约

### 3.1 函数/数据结构

- `CAContextEngine.select_context(request_messages, *, conversation_messages=None, incoming_message=None, budget_tokens=0)` 签名不变；返回语义增加一条：重建结果非法（空 / 非 dict 元素 / 缺 role / **无任何 role=="user" 消息**）→ 返回 `request_messages`（同一对象）。
- `AStageMixin._row_to_message(row, topic_grade, in_tail)` 输出消息字段变更：
  - assistant thought：`{"role":"assistant","content":<selected>,"tool_calls":[...]}`（**无 finish_reason**）
  - tool：`{"role":"tool","content":<selected>,"tool_call_id":...}` 或加 `"tool_name":...`（**无 name**）
  - `tool_calls` 每项内 `function` 为 dict 时**无条件**设置 `function.arguments` 为 str：非 str → `json.dumps(arg, ensure_ascii=False)`；`None`/缺键 → `"{}"`；已有 str 须 `json.loads` 合法才原样保留，解析失败或 strip 后为空 → `"{}"`（select_context 返回值绕过宿主 pre-selection 的 `sanitize_tool_call_arguments`；缺键会触发宿主 `_canonicalize_api_tool_calls` KeyError）。
- `EStageMixin._on_api_response_v5` 写库字段：`tool_defs[].function.arguments` 必须为 str；`tc.arguments` 为合法 JSON string → 原样；非法 JSON string → `"{}"`；非 str 非 None → `json.dumps(arg, ensure_ascii=False)`；None → `"{}"`。

### 3.2 字段清单

| 字段 | 类型 | 含义 | 默认 |
|---|---|---|---|
| `msg["finish_reason"]` | 禁止出现在重建 assistant 消息 | 宿主正常路径会在发 provider 前 pop | 无 |
| `msg["name"]`（tool 行） | 禁止 | Chat Completions tool 消息 schema 无此字段 | 无 |
| `msg["tool_name"]`（tool 行） | str | Hermes 内部字段，transport 发送前 strip | 有值才写 |
| `function.arguments` | str | JSON 编码的工具参数 | `"{}"` |

### 3.3 错误语义 / 降级

- 历史 DB 行 arguments 为 dict → A-stage 输出时转 JSON string（不删行，保守）。
- select_context 重建结果无 user → 静默 fail-open 返回 request_messages（记 warning）。
- 其余 select_context 错误语义不变（见现 docstring）。

## §4 实现步骤

| 步 | 文件/函数 | 改成什么 | 该步验收 |
|---|---|---|---|
| 1 | `ca/e_stage.py` `_on_api_response_v5` L65-L75 | arguments 保留/归一化为 str（§3.1） | `pytest -q -c tests/pytest.ini tests/stage/test_e_stage.py::TestOnApiResponseV5::test_tool_calls_arguments_stored_as_json_string` 通过 |
| 2 | `ca/a_stage.py` `_row_to_message` L209-L231 | tool 行 `name` → `tool_name`；删除 thought 行 `finish_reason` 输出；tool_calls 反序列化后对 `function.arguments` 做 str 归一化（可加模块级小 helper，不改公开签名） | `pytest -q -c tests/pytest.ini tests/stage/test_build_conv_history_v6.py::TestToolRowFields` 3 个用例全过 |
| 3 | `__init__.py` `select_context` L368-L386 | ⑥ 校验追加 `not any(m.get("role")=="user" for m in new_conv)` → fail-open；⑦ 注释改为「request_messages 中最后一条 user」（工具轮仍替换，不误导）；docstring 同步 | `pytest -q -c tests/pytest.ini tests/unit/test_ce_shell.py::TestSelectContext::test_T15_tool_round_wire_shape_and_current_user tests/unit/test_ce_shell.py::TestSelectContext::test_T16_bg_only_rows_fallback_to_request_messages` 通过 |
| 4 | `__init__.py` L1034-L1040、L1144 | 注释改为 select_context 驱动（不涉及行为） | `grep -n "compress" __init__.py` 中 A-stage 驱动注释无旧表述；`python -m py_compile __init__.py ca/a_stage.py ca/e_stage.py` 通过 |
| 5 | 全量回归 | 跑 `pytest -q -c tests/pytest.ini tests`，1027+6 全过 | 无新增失败 |

## §5 范围边界（DoD）

- ✅ 允许：修改 `__init__.py`、`ca/a_stage.py`、`ca/e_stage.py`（仅 §4 所述行为与注释）。
- ❌ **禁止**：修改测试文件（测试线由主笔维护，已完成）；修改其他业务文件；改 hook 注册；改 should_compress；改 topic/F-stage/reality 链路。
- ❌ **禁止**：**验证阶段禁止 apply 真实环境/权威源，落盘动作由 tester 执行**（不写 ~/.hermes 其他 profile、不重启 gateway、不改 OV）。
- ❌ 禁止：git commit / push（提交由主笔执行）；网络访问；删除/rename 任何文件。

## §6 风险与已知坑

- `_row_to_message` 的 tool_calls 反序列化结果被直接返回；历史 DB 中 arguments 是 dict（`~/.hermes/profiles/tester/ca_cache/20260810_183712_654710.db` 已实证）→ 必须兼容历史行，不能只修 E-stage。
- Hermes 正常路径在 `conversation_loop.py:1920` pop `finish_reason`，但 select_context 替换发生在 `:2054`，返回列表**不再经过**该 pop；transport 只 strip `tool_name`/timestamp/codex 等，不 strip `finish_reason`/`name` → 不修会送到 provider。
- 同理，宿主 `sanitize_tool_call_arguments`（conversation_loop.py:1787）在 select_context **之前**运行，重建列表的非法 JSON arguments 不会被修复 → A-stage 必须自行把非法/空 str 归一化为 `"{}"`。
- 宿主 `_canonicalize_api_tool_calls`（conversation_loop.py:973-1015，select_context 之后运行）在 function 缺 `arguments` 键时 try/except 两次直接取键都会 KeyError → A-stage 必须无条件补 `"{}"`。
- 测试 `tests/stage/test_build_conv_history_v6.py` 的旧 fixture 有缺 `type`/`function` 的 tool_calls（`[{"id":"c1"}]`），归一化时不要给这种残缺项新增字段，避免破坏旧契约（宿主 sanitizer 会兜底 repair）。

## §7 tester 验收清单

- [ ] 任务书合规：`taskbook-check.py docs/fix-task-20260816-select-context-wire.md` 通过
- [ ] 真实环境全量 pytest 通过（对比基线 1027 passed + 6 新测试）
- [ ] 变更文件 ⊆ §4 授权列表（`git diff --stat`）
- [ ] 测试文件未被编码 subagent 修改（sha256 与主笔改动前快照一致：test_ce_shell.py 62f6a1dc…、test_build_conv_history_v6.py 704bc3e0…、test_e_stage.py f2e3505e…）
- [ ] 真实 Hermes 插件加载复验：`PYTHONPATH=/home/i1j/.hermes/hermes-agent HERMES_HOME=/home/i1j/.hermes/profiles/tester python3 -c "...PluginManager... engine= ca_assembler hooks 8"`
- [ ] 编码 subagent 工具白名单审计通过
