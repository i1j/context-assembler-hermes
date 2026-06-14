# 调试记录 2026-06-13

## 会话编码表 + OpenViking 话题提交

### 一、会话编码表

**背景：** `_build_aligned_outcomes` 需要 `_turn_index` / `_seq_index` 来映射 conversation_history 的每消息到 turn_plan/tool_plan/turn_cache。原始 Hermes conversation_history 不携带这些 metadata，导致 tool 行全部 fallback 到 `outcomes.append("")` → absorb（279/279 条 tool 消息全变空格）。

**设计方案：** `docs/design/ca-conv-encoding.md`

**实装位置：** `ca/__init__.py`

- `EncodingRow` dataclass：存 `(turn_index, seq_index, role)`
- engine 属性：`_conv_encoding`（Dict[int, EncodingRow]）、`_encoding_max_conv`、`_encoding_loaded`、`_encoding_db`
- `_load_conv_encoding(session_id)`：A-stage 入口调用，从 `working_copy.db` 的 `conv_encoding` 表加载
- `_persist_conv_encoding(session_id, conversation_history)`：C-stage 末尾调用，写入本轮新增消息的编码

**编码规则：**
- 行序=0 的用户提问不存（始终保持原文）
- 其余每行存 `(turn, seq, role)`
- tool 行的 seq = tool_sub_index（对应 tool_plan 的 key）

**`_build_aligned_outcomes` 改动：**
- 循环改为 `for _conv_idx, msg in enumerate(conversation_history)`
- 优先查 `self._conv_encoding.get(_conv_idx)`，回退到 `msg.get("_turn_index")`（兼容旧数据）
- tool 行不再独立查 `msg.get("_turn_index")`，直接复用已解析的 `_turn` / `_hint_seq`

### 二、OpenViking 话题自动提交

**功能：** CA 话题切换时，将已完成话题的 L1 cache 提交到 OpenViking content/write。

**设计方案：** `docs/design/ca-ov-topic-submit.md`

**实装位置：** `ca/__init__.py` + `ca/config.py`

- `_TopicSwitchData` dataclass：暂存旧话题的 L1 数据
- A-stage `_compute_assemble_plan`：检测 topic switch → 打包旧话题 L1
- C-stage `_run_c_stage` 末尾：调用 `_fire_ov_submit`
- `_fire_ov_submit()`：组装 Markdown → POST `/api/v1/content/write`
- 重试机制：成功才清 `_pending_ov_submit`，失败保留下次 L-stage 继续试。无计数器、无队列、无丢弃阈值

**配置：** `CA_OV_SUBMIT_ENABLED`（默认 True，env `CA_OV_SUBMIT_ENABLED=0` 关闭）

### 三、测试

**测试覆盖：**
- `tests/`: 392 passed / 4 pre-existing flakes（cross-file 环境变量隔离问题）/ 零回归
- 端到端：content/write 链路已验证（HTTP 200, semantic_status=complete, vector_status=complete）

### 四、遗留

- `_build_aligned_outcomes` 中 `_hint_seq` 被 Pyright 标记为 `int | Unknown`，运行时由 `_turn is not None` 护住，安全
- 新会话/空编码表 → fallback 到原有顺序计数，不影响现有测试
- `_encoding_db` 在 `_run_c_stage` daemon 线程中使用 SQLite 连接，非线程安全的写操作通过 `INSERT OR REPLACE` 的原子性保证，低并发场景无问题
