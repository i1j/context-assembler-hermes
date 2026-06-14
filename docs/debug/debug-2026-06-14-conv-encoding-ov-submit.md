# 调试记录 2026-06-14 — 会话编码表 + OV 话题提交

## 一、会话编码表

### 背景

`_build_aligned_outcomes` 需要 `_turn_index` / `_seq_index` 来映射 conversation_history 的每消息到 turn_plan/tool_plan/turn_cache。原始 Hermes conversation_history 不携带这些 metadata，导致 tool 行全部 fallback 到 `outcomes.append("")` → absorb。

### 设计

`docs/design/ca-conv-encoding.md`

### 实装

| 改动 | 位置 | 说明 |
|------|------|------|
| `EncodingRow` dataclass | ca/__init__.py | (conv_idx, turn_index, seq_index, role) |
| `_persist_conv_encoding()` | C-stage finally 末尾 | 写入 conv_encoding 表（INSERT OR REPLACE） |
| `_load_conv_encoding()` | A-stage `_compute_assemble_plan` 开头 | 加载到 `_conv_encoding` dict |
| `_build_aligned_outcomes()` | 循环改为 enumerate + 查 `_conv_encoding.get()` | 替代 `msg.get("_turn_index")` |
| `_compute_tool_plan_v2()` | **修复：** 也用 encoding 表 | 原用 `msg.get("_turn_index")` = None → 空计划 |

### 端到端验证

session `20260614_120409_b3744d` DB 中 `conv_encoding` 表 41 行，结构正确：

```
conv_idx=1: turn=1 seq=0 role='assistant'   # asst{tc}
conv_idx=2: turn=1 seq=0 role='tool'         # tool_1
conv_idx=3: turn=1 seq=1 role='tool'         # tool_2
conv_idx=4: turn=1 seq=2 role='tool'         # tool_3
conv_idx=5: turn=1 seq=0 role='assistant'    # asst_fin
```

独立脚本验证 `_compute_tool_plan_v2()` 修复后正确生成 3 个工具计划条目（sub=0,1,2）。

## 二、OpenViking 话题自动提交

### 设计

`docs/design/ca-ov-topic-submit.md`

### 实装

| 改动 | 位置 | 说明 |
|------|------|------|
| `_TopicSwitchData @dataclass` | ca/__init__.py | 修复缺失的 `@dataclass` 装饰器 |
| 话题切换检测 | `_compute_assemble_plan` topic segmentation 后 | 比较 `_last_topic_id` vs 当前话题，打包旧话题 L1 |
| `_fire_ov_submit()` 调用 | `_run_c_stage` finally 末尾 | 编码表持久化之后，try/except 保护 |
| `_fire_ov_submit()` 方法 | 文件末尾 | Markdown 组装 → POST `/api/v1/content/write` |
| `CA_OV_SUBMIT_ENABLED` | ca/config.py | 默认 True，env 关闭 |

### 数据流

```
A-stage _compute_assemble_plan:
  turn_to_topic → 检测 _current_topic_id != _last_topic_id
  → 打包 topic_data[last_topic_id] 为 _TopicSwitchData
  → 设置 _pending_ov_submit

C-stage _run_c_stage finally:
  _fire_ov_submit(_pending_ov_submit)
  → HTTP 200 → _pending_ov_submit = None
  → 失败 → 保留下次 L-stage 重试
```

### 提交历史

```
97a3590 feat: 会话编码表 + _compute_tool_plan_v2 接入编码表
4bd0190 feat: OpenViking 话题自动提交
```

### 测试

396/397 passed / 1 pre-existing flake（test_tc_c_T1 孤立通过 / Config 交互污染）

## 三、遗留

- OV 话题提交的端到端验证需要重启 Hermes 后跑含话题切换的对话
- 验证入口：重启 Hermes → `/new` → 多轮对话触发话题切换 → 查 `/api/v1/content/write` 日志
