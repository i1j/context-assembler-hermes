# bg_review 后台内容清空 (2026-06-12)

## 触发

用户观察到 bg_review 行在发给 LLM 的 `conversation_history` 中始终原文照抄，CA 不做任何处理。要求将 bg_review 轮（含其工具轮）的 content 清空。

## 需求演变

1. 用户：「先把 mutation 中后台对话轮（包括它的工具轮）的内容都写为空」
2. 实现：在 `_mutation_mode` 中加第二遍扫描，查 turn_cache 的 biz_category 识别 bg_review turn，清空 content
3. 用户指出问题：**尾区（最后 2 对话轮 + 当前 Q）中夹杂的 bg_review 行也被清空了**，违反尾区保护原则
4. 修正：不再用 `bypass_turns` Set 匹配，改按位置计算尾区边界

## 最终方案

### 尾区边界计算

从后往前数**非 bg_review 的 user 轮**，到第 3 个时标记保护起点：

```python
_dialogue_count = 0
for i in range(len(conv_h) - 1, -1, -1):
    msg = conv_h[i]
    if msg["role"] == "user":
        _turn = msg.get("_turn_index")
        if _turn is not None and _turn not in _bg_ts:
            _dialogue_count += 1
            if _dialogue_count >= 3:
                _tail_boundary = i + 1
                break
```

`_tail_boundary` 之后的 messages 全部保留原始内容（含 bg_review）。

### 主逻辑合并

不再用第二遍扫描。直接在 mutation 循环的 `outcome is None` 分支判断：

```python
if outcome is None:
    if _bg_ts and msg not in system and _turn in _bg_ts and i < _tail_boundary:
        msg["content"] = " "    # bg_review 在尾区之外 → 清空
    # else → continue (保留原文，含尾区)
```

### 发给 LLM 的内容

非尾区 bg_review 行发：

```json
{"role": "user", "content": " "}       # 8 tok
{"role": "assistant", "content": " "}  # 8 tok
{"role": "tool", "tool_call_id": "...", "content": " "}  # 32 tok
```

每行 8/32 tok，零信息，但骨架保留（不可删，共享 dict 引用限制）。

## 性能影响

一次 `store.read_turn_biz_categories()` 查询 + 一次正向扫描 conv_h + 一次反向扫 user 轮计数。O(n) 量级，与现有 mutation 同阶。

## 变更文件

- `~/.hermes/profiles/tester/plugins/ca_assembler/__init__.py`
  - 移除 `_mutation_mode` 中的"4. bg_review 清空"独立段（原约 25 行）
  - 合并为主循环中的条件分支（约 15 行净增，但消除了单独扫描的开销）
- `ca/__init__.py`：未改动

## 已知限制

- 依赖 `_turn_index` 在 message dict 上的存在性；fallback 使用顺序计数，如果 bg_review 的 user 消息未被 CA 分配 turn_index，计数可能错位
- state.db 通过 snapshot 恢复原始内容，不受清理影响（与现有机制一致）
- 工具行 32tok 的 JSON 骨架开销（其中 tool_call_id 占 19tok）在次迭代未解决，需后续抉择
