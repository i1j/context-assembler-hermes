# biz_category 实施报告

**日期**: 2026-06-11
**类型**: 实装验证
**前置**: debug-20260611-bypass-design-evolution.md（设计锁定）
**会话**: 20260611（实施 session）
**commit**: `39f338a`

---

## 1. 改动总览

| 文件 | +行 | -行 | 内容 |
|------|----|----|------|
| `ca/store.py` | 39 | 11 | schema + 全链路参数贯通 + 查询方法 |
| `ca/__init__.py` | 136 | 60 | C-stage 写 / A-stage 消费 / 脆弱点修复 ×2 |
| `tests/test_c.py` | 4 | 4 | 断言更新（180K 修复语义变化） |
| **合计** | **179** | **75** | |

## 2. 改动详情

### 2.1 DB Schema（store.py）

**turn_cache** 加 `biz_category TEXT`，**turn_plan** 加 `biz_category TEXT`，均默认为 NULL。
索引用 `biz_category IS NOT NULL` 过滤 bg_review 轮。

### 2.2 write_turn 管线（store.py）

所有 3 处 INSERT（`write_turn` / `write_turns_batch` / `write_tool_group`）加 `biz_category` 列 + 参数。新增 `write_turn()` 参数签名 `biz_category: Optional[str] = None`。

### 2.3 read_turn_biz_categories（store.py 新增）

```python
def read_turn_biz_categories(self, session_id: str) -> Dict[int, str]:
    """返回 session 中所有对话轮的 {turn_index: biz_category}。"""
```

查询条件：`role='user' AND api_call_count=0 AND seq_index=0 AND biz_category IS NOT NULL`

### 2.4 TurnPlanEntry（ca/__init__.py）

加 `biz_category: Optional[str] = None` 字段，`as_dict()` 序列化。

### 2.5 C-stage 写入（ca/__init__.py _run_c_stage）

两处 `write_turn` 调用加 `biz_category="bg_review" if bg_review else None`：
1. 截断降级路径（line ~406）
2. 正常完成路径（line ~449）

不含 LLM 路径的 OODA JSON 解析分支——非 bg_review 轮 biz_category=NULL。

### 2.6 A-stage 消费（ca/__init__.py _compute_assemble_plan）

**tail_protected_turns 计算**（20K 保护区）：
```python
_biz_cats = self.store.read_turn_biz_categories(self._session_id)
_real_turns = [t for t in _dialogue_turns if t not in _biz_cats]
_bypass_skip = min(3, len(_real_turns))  # 只计真实对话轮
```

**biz_category 注入 plan**：
```python
for e in plan:
    if e.turn_type == "dialogue" and e.turn_index in _biz_cats:
        e.biz_category = _biz_cats[e.turn_index]
```

**bypass_turns 集**：
```python
_bypass_turns: Set[int] = set(_biz_cats.keys())  # bg_review 全量旁路
_real_plan_entries = [e for e in plan if e.turn_type == "dialogue" and e.turn_index not in _biz_cats]
for _entry in _real_plan_entries[-3:]:
    _bypass_turns.add(_entry.turn_index)
```

### 2.7 脆弱点修复

#### _format_tool_group_assembly

**改前**：`_found_group` 顺序计数 → 遍历 conversation_history，遇到 `assistant{tc}` 递增，匹配 `group_idx`。

**问题**：Hermes 截断历史后顺序计数偏移，永远找不到目标组。

**改后**：`_api_call_count` 直接匹配。参数签名 `group_idx: int` → `api_call_count: int`。历史中 `msg.get("_api_call_count")` 与参数值相等即命中。

调用处同步更新：
- `_build_aligned_outcomes`: `group_idx` → `entry.api_call_count`
- `_build_messages_from_plan`: `entry.tool_sub_index if hasattr(...)` → `entry.api_call_count`

#### _build_aligned_outcomes

**改前**：`current_turn` 顺序递增计数器，按 `seq_idx = current_turn - 1` 索引 `dialogue_seq[seq_idx]`。

**问题**：截断后顺序索引与 plan 偏移。

**改后**：按 `msg.get("_turn_index")` 查 `dialogue_by_turn[turn]`。保留顺序计数降级路径兼容旧测试数据。

#### _build_messages_from_plan

确认 engine 层不保留硬编码 bypass 逻辑。bypass_turns 全量从 `_AssemblePlanResult` 传入。

### 2.8 测试修复

`test_tc_c_015_background_review_skips_llm` 断言从硬编码「系统后台审查」改为 `user_message[:80]`（180K 修复引入的语义变化，`_run_c_stage` bg_review 路径不再写固定字符串）。

## 3. 验证结果

```
299 passed, 4 failed (既存), 13 skipped
```

与原生基线完全一致：**0 回归**。

4 个既存失败全部在 `test_a.py`（标签注入 `[~/N/0]` 模式），改动前后一致，非本次引入。

## 4. 设计决策追溯

| debug-20260611 设计 | 实装 |
|---------------------|------|
| `biz_category VARCHAR` 双表 | ✅ turn_cache + turn_plan |
| C-stage OODA JSON → 即时写入 | ✅ bg_review 路径写 "bg_review" |
| A-stage 反向扫 + skip | ✅ `_real_turns` 过滤 |
| `_bypass_skip` 动态 | ✅ `min(3, len(_real_turns))` |
| 一次 commit 批量实装 | ✅ commit `39f338a` |
| `_format_tool_group_assembly` 修复 | ✅ `_api_call_count` 匹配 |
| `_build_aligned_outcomes` 修复 | ✅ `_turn_index` key-based |
| `_build_messages_from_plan` 清理 | ✅ 确认干净 |
