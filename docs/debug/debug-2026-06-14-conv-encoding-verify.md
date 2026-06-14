# 调试记录 2026-06-14 (更新)

## 会话编码表 — 端到端验证

### 发现的问题

**`_compute_tool_plan_v2` 未使用编码表。** 2026-06-13 仅在 `_build_aligned_outcomes` 中接了编码表，但 `_compute_tool_plan_v2`（A-stage 工具计划生成）仍用 `msg.get("_turn_index")` / `msg.get("_seq_index", 0)` 读取元数据——而这些元数据在原始 Hermes conversation_history 上从未设置。

结果：所有 tool 行的 `turn` 均为 `None` → `continue` → 工具计划为空 → `_tool_by_key` 空 → `_build_aligned_outcomes` 中所有 tool 行 fallback 到 `outcomes.append("")` → mutation 模式下变为 `content="  "`（absorb）。

### 修复（2026-06-14）

`ca/__init__.py:1647-1663` `_compute_tool_plan_v2()`：

```python
# 修复前：
for msg in messages:
    if msg.get("role") != "tool":
        continue
    turn = msg.get("_turn_index")       # None → 跳过所有
    seq = msg.get("_seq_index", 0)
    if turn is None:
        continue

# 修复后：
for conv_idx, msg in enumerate(messages):
    if msg.get("role") != "tool":
        continue
    _enc = self._conv_encoding.get(conv_idx)   # 优先查编码表
    if _enc:
        turn = _enc.turn_index
        seq = _enc.seq_index
    elif msg.get("_turn_index") is not None:   # 回退到 metadata
        turn = msg.get("_turn_index")
        seq = msg.get("_seq_index", 0)
    else:
        continue
```

### 端到端验证结果

| 检查项 | 结果 | 说明 |
|--------|------|------|
| conv_encoding 表写入 | ✅ | `20260614_120409_b3744d` session 中有 41 条编码记录 |
| 编码表内容 | ✅ | 5 条：assistant{tc}(seq=0) + 3×tool(seq=0,1,2) + assistant_fin(seq=0) |
| 工具计划生成 | ✅ | 3 个工具行正确产出 3 条 tool_plan 条目（sub=0,1,2） |
| tool_sub_index 正确 | ✅ | 0, 1, 2 与 tool_cache 的 seq_index 对齐 |
| 目标级别正确 | ✅ | parent L1 → tool L0 |
| 回退兼容 | ✅ | 无编码表时 fallback 到 metadata |
| 全量测试 | ✅ | 397 passed / 20 skipped / 0 regression |

### 未实装（OV 话题提交）

`_fire_ov_submit` 方法和话题切换检测尚未实现。当前只有 `_TopicSwitchData` dataclass 和 `_pending_ov_submit` 属性占位。

### 已提交

```
97a3590 feat: 会话编码表 + _compute_tool_plan_v2 接入编码表
  ca/__init__.py   | +164  (编码表 + tool_plan 修复 + OV 占位)
  ca/config.py     | +1    (CA_OV_SUBMIT_ENABLED)
  tests/test_config.py | +9/-15 (test_tc_cf_002 污染修复)
```
