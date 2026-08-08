
> **⚠️ 已废弃 — ContextEngine 管线已在 v5.10 移除。CA 不再通过 `_compress_context` 接管 Hermes 消息构造。详见 P-004 (8 hooks)。**

# CE 管线：空壳→实装 技术方案

## 1. 现状

`_ca_compress` 当前是空壳：

```python
def _ca_compress(messages, system_message, ...):
    _saved_originals[sid] = [dict(m) for m in messages]
    assembled = (messages, system_message or "")
    _saved_short_len[sid] = len(assembled[0])
    return assembled
```

输出 = 原始 messages 透传。需要替换为 CE 的输出格式。

## 2. 输出格式

```
messages_out = [
    {"role": "system", "content": system_msg},
    {"role": "user", "content": "━━━ 摘要文本 ━━━"},
    ...tail 原文（完整对话轮）...
]
```

### 摘要文本结构

每段对应一个**对话轮**，按时间序排列。工具不独立成段。

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
检查服务器状态                          ← 对话轮入口行
  uptime负载0.5正常                     ← Fct materials（单工具折叠）

查磁盘和内存                            ← thought（对话轮 Elm）
  df -h→/data 80%                      ← 子工具展开
  free -m→mem 12/16G
  lsblk→vda 40G

看nginx日志                             ← Fct
  tail error.log→upstream timeout | timeout=30s

磁盘检查                                ← thought
  mysqladmin status→Uptime 30d
  show processlist→3 slow queries

怎么优化                                ← Hdl
  调大timeout | 加索引

写脚本                                  ← thought
  optimize.sh→67 lines
  monitor.sh→45 lines ×2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 对话轮入口行数据来源

| 对话轮 level | 入口行 | 来源 |
|-------------|--------|------|
| Elm | thought（assistant.content） | Hermes 原始消息的 assistant[tc].content |
| Fct | `core_change` | turn_cache `l1_text.core_change` |
| Hdl | `l0_text` | turn_cache `l0_text` |

### 工具展示规则

| 对话轮 level | 工具 level | 工具展示 |
|-------------|-----------|---------|
| Elm | **Fct** | 子工具行展开（_format_single_tool + ×N 合并） |
| Fct | **Hdl** | 折叠进 `new_materials`，不单独出现 |
| Hdl | skip | 不出现 |

### 工具组（组内多工具）

入口行 = thought。子工具逐行展开，`_format_single_tool(l1_text, l0_text, content)` 输出。

×N 合并：连续相同 `tool_name + detail` → `tool: detail ×N`。

### 单工具（对话轮内只有一个工具）

折叠进 Fct 的 `new_materials`。不单独成行。

### 尾区

最后 **2 个对话轮**作为原文 bypass。从原始 `messages` 中取对应消息追加到摘要块后。

确保不截断 `assistant(tool_calls)→tool` 链：以完整对话轮为单位（从 user 消息开始，到最后一个 tool/assistant 结束）。

### 无头区

`protect_first_n = 0`。system prompt 保留，其余所有非 bypass 轮次进摘要块。

### 无 English 声明

去掉原版 `[CONTEXT COMPACTION — REFERENCE ONLY]...` 大段前缀。

## 3. 实现

### 3.1 数据流

```
_ca_compress(messages, system_message)
  │
  ├── ① 获取当前 session 的 CA 引擎
  │       → plugin._engine (ContextAssembler 实例)
  │
  ├── ② 从 turn_cache 读取所有对话轮数据
  │       → store.read_turn_texts() 逐轮读 elm/fct/hdl
  │       → 需要 extension: 读取 group 数据、thought
  │
  ├── ③ 计算 level 分配（复用 _compute_assemble_plan）
  │       → 输入：context_length, user_message
  │       → 输出：plan（每轮 target_level）
  │
  ├── ④ 确定尾区边界
  │       → 从 messages 末尾向前数 2 个完成对话轮
  │       → 签界：回退到首条 tool 消息以前
  │
  ├── ⑤ 构建摘要文本块
  │       → 遍历 plan，对每个对话轮：
  │         - 取入口行（thought / core_change / l0_text）
  │         - 若工具 level=Fct：追加缩进子工具行
  │         - 若对话 Fct：追加缩进 new_materials
  │         - 折叠单工具工具行到 new_materials
  │         - 行间隔：对话轮间空一行
  │       → output: str
  │
  ├── ⑥ 组装 messages_out
  │       → system + user(摘要正文) + tail messages
  │
  └── ⑦ 返回 (messages_out, system_message)
```

### 3.2 关键接口

```python
def _ca_compress(self, agent, session_id, messages, system_message, ...):
    engine = self._engines.get(session_id)  # CA 引擎
    if not engine:
        return (messages, system_message)

    plan = engine._compute_assemble_plan(current_user_msg, context_length)
    tail_turns = _select_tail(plan, TAIL_TURN_COUNT=2)

    summary = _build_summary(engine, plan, tail_turns)
    tail_msgs = _extract_tail(messages, tail_turns)

    return ([
        {"role": "system", "content": system_message},
        *([{"role": "user", "content": summary}] if summary else []),
        *tail_msgs,
    ], system_message)
```

### 3.3 新增/修改函数

| 函数 | 位置 | 职责 |
|------|------|------|
| `_build_summary(engine, plan, tail_idx)` | ce 层 | 遍历 plan → 调用 formatter 拼接文本 |
| `_select_tail(plan, n)` | ce 层 | 确定最后 N 个完整对话轮的用户入口索引 |
| `_extract_tail(messages, tail_set)` | ce 层 | 从 messages 取 tail 消息 |
| `_get_thought(messages, turn_index)` | ce 层 | 从原始消息中取 assistant[tc].content |
| `_format_l1_for_display()` | **复用** ca/__init__.py L1738 | 不变 |
| `_format_single_tool()` | **复用** ca/__init__.py L1848 | 不变 |
| `_format_tool_group_assembly()` | **调整** ca/__init__.py L1880 | 去掉 `【工具组:】` header 和 `（N个,ok）`，只输出子工具行 + ×N |

### 3.4 `_format_tool_group_assembly` 修改

当前输出：
```
【工具组:查磁盘（3个,ok）】
  df -h→/data 80%
  free -m→mem 12/16G
```

修改为：
```
  df -h→/data 80%
  free -m→mem 12/16G
  lsblk→vda 40G
```

仅输出缩进子工具行（×N 合并保留）。group header 由 CE 用 thought 自行提供。

## 4. 里程碑

| 阶段 | 任务 | 产出 |
|------|------|------|
| M1 | `_format_tool_group_assembly` 修掉 header | 工具组只输出子工具行 |
| M2 | `_select_tail` + `_extract_tail` | 尾区选择逻辑 |
| M3 | `_get_thought` | 从助理消息提取 reasoning |
| M4 | `_build_summary` | 主拼接逻辑 |
| M5 | `_ca_compress` 接入 | 替换空壳透传 |
| M6 | 集成测试 | 完整链路验证 |

## 5. 未解决问题

1. **`_compute_assemble_plan` 复用**：当前需要 `user_message` 和 `context_length`。CE 的 `_ca_compress` 没有 user_message 参数——从 messages 最后一条取 `role=user` 的 content。

2. **thought 获取**：`assistant.content`（reasoning）在原始 messages 中存在，但 turn_cache 没有这个字段。需要从 pass-through 的 `_saved_originals[sid]` 中读取。

3. **level 计算与 CA 现有逻辑差异**：现有 CA 的 level 分配基于 token budget + topic retrieval + tool boost。CE 初期可以简化为固定 Fct（默认）或 `Fct + 尾区` 二分法。
