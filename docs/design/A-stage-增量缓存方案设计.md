# A-stage 增量缓存方案

> **状态**：✅ 已实装（2026-06-17）。详见 [实装报告](A-stage-增量缓存实装报告.md)。
>
> 本文档保持为设计稿，供理解增量缓存的动机和设计决策。实际实现以实装报告和代码为准。

> 设计文档 · 2026-06-17

## 动机

当前 `_simple_mutation_mode_v5`（全量路径）在每轮 `pre_llm_call` 中遍历整个 `conversation_history`（100+ 轮时可达数千条消息），对每条非尾部的 thought/tool 行都执行一次 `get_turn_ca_rows` 查询 + 角色队列匹配 + 内容替换。当话题未切换时，稳定区的替换结果与上轮完全相同，重复工作是多余的。

增量缓存方案：缓存稳定区（尾部保护区之外）的替换结果，当话题未切换时仅处理 delta——从尾部滑入稳定区的 1~2 轮，其余直接复用缓存。

## 数据结构

```python
# 插件实例变量
_A_stable_cache: list[dict] | None
    # 稳定区缓存：已做 Fct/Hdl 替换的 conv_hist 片段。
    # 包含 assistant(thought)、assistant(fin)、tool 行 + 穿插的 system 消息。
    # 格式与 Hermes conversation_history 条目一致（role+content+...）。
    # None = 冷启动或话题切换后失效。

_A_cache_turns: int
    # 缓存中包含的 user 消息数（即"轮数"）。
    # 用于增量路径中 delta 处理的 turn 号起始值。

_A_cache_is_stale: bool
    # 缓存污染标记：Fct pending 导致部分 delta turn 无法替换时置 True。
    # True → 下次不走增量路径，走全量重建缓存。
```

## 主流程

```
pre_llm_call_v5()
  │
  ├─ 话题检测 detect()
  │    ├─ 切换 → _A_stable_cache = None → 全量
  │    └─ 无切换 → 继续
  │
  ├─ cache 状态判断
  │    ├─ cache is None → 全量
  │    ├─ _A_cache_is_stale → cache=None → 全量
  │    └─ cache 有效 → 增量
  │
  ├─ 全量路径: _full_mutation()
  │    ├─ 逐 turn 查表替换（与原 _simple_mutation_mode_v5 一致）
  │    ├─ 尾部保留 Elm
  │    └─ 写缓存: _A_stable_cache = conv_hist[0:tail_boundary]
  │
  └─ 增量路径: _incremental_mutation()
       ├─ 保存 _saved_history_snapshot（供 post_llm_call 恢复 Elm）
       ├─ Step 1: 计算尾部边界
       ├─ Step 2: 并行扫描缓存 → 覆盖稳定区（role 校验）
       ├─ Step 3: 收集 Delta 区（原尾部 → 现稳定）的 turn_rows
       ├─ Step 4: Delta 替换（含 Fct pending 防护）
       ├─ Step 5: 写缓存
       └─ debug dump（与全量一致）
```

## 增量路径细节

### Step 1: 尾部边界

与当前 `_simple_mutation_mode_v5` 一致：
- 从 conv_hist 尾部扫描倒数第 2 个 `role="user"` 的消息下标 → `tail_boundary`
- 不足 2 轮时 `tail_boundary = len(conversation_history)`（全量尾部）

### Step 2: 并行扫描缓存

```
cache_idx = 0
for i, msg in enumerate(conversation_history):
    if cache_idx >= len(cache): break

    role, c_msg = msg.get("role"), cache[cache_idx]
    if role == c_msg.get("role"):
        msg["content"] = c_msg.get("content", "")
        msg.pop("reasoning_content", None)
        msg.pop("tool_calls", None)
        cache_idx += 1
    elif role == "system":
        continue  # Hermes 新插入的 system，不消耗缓存
    else:
        return _full_mutation(conversation_history)  # 对齐失败 → 回退
```

### Step 3: Delta 收集

```python
current_turn = self._A_cache_turns  # ★ 从缓存覆盖的 user 计数开始
for i in range(len(cache), len(conversation_history)):
    msg = conversation_history[i]
    role = msg.get("role")
    if role == "system": continue
    if role == "user": current_turn += 1; continue
    if i >= tail_boundary: continue
    # → 按标准分类: thought / tool / fin
    turn_rows.setdefault(current_turn, []).append((i, row_type))
```

### Step 4: Delta 替换（含 Fct pending 防护）

```python
for turn_num, rows in turn_rows.items():
    ca_rows = get_turn_ca_rows(store, sid, turn_num)
    if not ca_rows: continue

    # 角色队列构建
    ca_thoughts, ca_tools = [], []
    has_pending = False
    for _seq, role_, fr, tc, fct in ca_rows:
        if role_ == "user": continue
        if role_ == "assistant" and fr == "stop": continue
        if fct is None:
            has_pending = True  # ★ Fct pending → 不入队列
            continue
        if role_ == "assistant": ca_thoughts.append(fct)
        elif role_ == "tool": ca_tools.append(fct)

    if has_pending:
        self._A_cache_is_stale = True

    # 1:1 角色匹配
    ti, tj = 0, 0
    for conv_idx, row_type in rows:
        if row_type == "fin": continue
        if row_type == "thought" and ti < len(ca_thoughts):
            conversation_history[conv_idx]["content"] = ca_thoughts[ti]
            ...
```

### Step 5: 写缓存

```python
self._A_stable_cache = [{**m} for m in conversation_history[0:tail_boundary]]
self._A_cache_turns = sum(1 for m in self._A_stable_cache if m.get("role") == "user")
self._A_cache_is_stale = False
```

## 边界条件

| 场景 | 处理 |
|---|---|
| **冷启动** (cache is None) | `_full_mutation` |
| **话题切换** (detect==True) | cache = None → 全量 |
| **Fct pending** (delta turn Fct=None) | `_A_cache_is_stale=True` → 保留 Elm → 下轮全量 |
| **缓存对齐失败** (role 不匹配) | 回退 `_full_mutation` |
| **不足 2 轮** | tail_boundary=len(conv_hist)，cache 空，增量不处理 |
| **无新轮** | Step 2 覆盖，Step 3 空，缓存不变 |
| **system 消息穿插** | Step 2 中 `role=="system"` 跳过消耗，透传 Elm |
| **后台轮进入稳定区** | 缓存已包含其 Fct，增量路径直接复用 |
| **后台轮进入 delta** | get_turn_ca_rows 拿到 E-stage 写好的 Fct，正常替换 |
| **session reset** | 插件重建 → cache=None → 冷启动 |

## 后台轮 E-stage 优化（第 2 步）

后台轮当前走：E-stage 写 Elm → F-stage daemon 读 Elm → 代码生成 Fct → 写回 Fct 列。

改为：在 `_on_pre_llm_call_v5` 中检测到 `bg=True` 时，直接同步写入 Fct 列（代码生成摘要，无需 LLM），跳过 F-stage 和 A-stage。

```python
if bg:
    _brief = (user_message or "")[:80].strip() or "后台审查"
    fct_data = {
        "changes": [{"stage_tag": "已实施", "core_change": _brief}],
        "core_change": _brief,
        "_assemble_status": 0,
    }
    write_turn_v5(
        ..., fct_text=json.dumps(fct_data), hdl_text=_brief[:100],
        biz_category='bg_review',
    )
    return None
```

清理 F-stage 中的 `bg_review` 分支。

## 缓存状态转换总图

```
冷启动 ──→ _A_stable_cache=None
              │
              ▼
         [pre_llm_call_v5]
              │
              ├─ detect()==True? ──→ cache=None → 全量 → 写缓存
              │
              ├─ cache is None? ──→ 全量 → 写缓存
              │
              ├─ _A_cache_is_stale? ─→ cache=None → 全量 → 写缓存
              │
              └─ cache 有效 ──→ _incremental_mutation
                                     │
                              ┌──────┴──────┐
                              │              │
                      对齐失败/        正常处理
                     Fct pending         │
                              │          │
                              ▼          ▼
                        回退全量    写新缓存
```
