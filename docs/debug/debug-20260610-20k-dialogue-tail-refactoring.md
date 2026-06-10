# 20K 对话保护区重构实装报告

**日期**: 2026-06-10
**类型**: 实装验证
**前置文档**: `design/ca-20k-dialogue-tail-redesign.md`

---

## 1. 重构内容

将 CA 尾部保护机制从「消息遍历 + `tool_tail_turns`」迁移到「turn_cache 路线 + `tail_protected_turns`」。

### 改前（v4.x + pr3）

```
_compute_assemble_plan()
  ├─ _compute_tail_start(messages)      ← 逆序遍历消息，跳过 role=tool
  │                                        累加 10K token → tail_start（消息索引）
  ├─ tool_tail_turns                    ← 最后 TOOL_TAIL_TURN_COUNT 个对话轮
  │                                        的独立工具保护集
  └─ _compute_turn_plan_v2(messages, ..., tail_start, tool_tail_turns, ...)
       ├─ 对话轮: turn >= _tail_start_turn → L2 (tail)
       ├─ 工具组: turn_idx in tool_tail_turns → L2 (tail)
       └─ 工具组: 否则 dialogue_downgrade
```

### 改后（v5.0）

```
_compute_assemble_plan()
  ├─ 从 l1_texts 取对话轮序，跳过最后 2 个旁路轮
  │    从倒数第 3 个向前累加 l2_tokens（仅用户+assistant 对话内容）
  │    整轮进出，累计 > 20K 的轮退到 Zone ③
  │    → tail_protected_turns: Set[int]
  ├─ _available_budget() 计算 Zone ③ 预算
  └─ _compute_turn_plan_v2(messages, ..., tail_protected_turns, ...)
       ├─ 对话轮: turn in tail_protected_turns → L2 (tail)
       └─ 工具组: dialogue_downgrade（统一，不再区分 tail/非tail）
```

---

## 2. 三区模型（最终代码级实现）

| 区 | 判定方式 | plan 级别 | assembly 处理 |
|---|----------|-----------|---------------|
| ① 旁路区 | 最后 2 个 dialogue turn，**不计入** tail_protected_turns | 对话 L2，工具走 dialogue_downgrade | `_original_messages` 旁路覆盖 |
| ② 延续区 | 从倒数第 3 个起，l2_tokens 累计 ≤ 20K 的 turn | 对话 L2 (tail)，工具 L1 | 正常 CA cache 注入 |
| ③ 更早区 | 超载的轮及更早 | 话题定级 + 工具低一级 | 正常 CA cache 注入 |

---

## 3. 改动清单

### ca/__init__.py

| 位置 | 改动 |
|------|------|
| L959 处 | **移除** 旧 `_compute_tail_start(messages)` + `tool_tail_turns` 计算 |
| | **新增** tail_protected_turns 计算：从 l1_texts 取 dialogue turn 序，跳过最后 2 个旁路轮，累加 l2_tokens（`.get("role") not in ("system","tool") and "tool_calls" not in m`），整轮进出 |
| L1011-1026 | **TODO 移除**，budget=0 硬编码→ `_available_budget()` 调用 |
| L1027 | `_available_budget()` 签名：`tail_start, tool_tail_turns` → `tail_protected_turns: Set[int]` |
| L1028-1057 | 内部逻辑简化：移除 tool_key_map 迭代，改为 `turn in tail_protected_turns` 判断 |
| L1397 | `_compute_turn_plan_v2()` 签名：`tail_start, tool_tail_turns` → `tail_protected_turns` |
| L1418-1426 | **移除** tail_start→turn 转换逻辑（`_tail_start_turn` 已不用） |
| L1433 | `in_tail = turn in tail_protected_turns`（原本 `turn >= _tail_start_turn`） |
| L1487-1504 | 工具组：移除 tool_tail_turns 分支，统一 dialogue_downgrade |
| L1964 | `_compute_tail_start()` docstring 标记弃用 |

### 配置统一

| 文件 | 操作 |
|------|------|
| `ca/settings.yaml` | `protect_tail_tokens: 10000 → 20000` |
| `plugin.yaml` | `protect_tail_tokens` schema 段删除 |
| `~/.hermes/profiles/tester/config.yaml` | `protect_tail_tokens: 20000` 行删除 |

**配置数据流**: `settings.yaml` → `os.getenv("CA_PROTECT_TAIL_TOKENS", "20000")` → `Config.PROTECT_TAIL_TOKENS`

### 测试适配

| 测试 | 改动 |
|------|------|
| `test_TC_A_021_topic_boost_tools` | 移除 `TOOL_TAIL_TURN_COUNT=0` patch，更新断言匹配新架构（对话 L1+ 话题基础，工具 dialogue_downgrade） |

---

## 4. 验证结果

```
tests/test_v440.py + test_v460.py + test_plugin.py
112 passed, 12 skipped, 0 failed  ✓
```

---

## 5. 已知遗留

| 条目 | 说明 |
|------|------|
| `_compute_tail_start()` | 标记弃用但未删除，保留至全部测试迁移 |
| `Config.TOOL_TAIL_TURN_COUNT` | 保留在 config.py 中以防外部引用，使用已清零 |
| `test_v460.py` 的 `TestComputeTurnPlanV2` | 9 个旧架构测试全部 skip（标注 `旧 per-tool 架构测试`），待清理 |
| `_available_budget()` | 已接入 plan 决策，但 budget_remaining 仅记录不计策——需待预算-再摘要体系重构时进一步改造 |
