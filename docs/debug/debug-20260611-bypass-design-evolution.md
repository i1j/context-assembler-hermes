# bypass 设计演进 + biz_category 实施路径

**日期**: 2026-06-11
**前置**: debug-20260610-20k-dialogue-tail-refactoring.md 尾区三区模型
**会话**: 20260611（前 30+ 轮设计讨论）

---

## 一、设计演进路线

### 起点：engine 级 bypass（v5.1 实装）

```
_compute_assemble_plan() 内
  → _dialogue_plan_entries[-2:] → _bypass_turns: Set[int]
  → 传给 _build_aligned_outcomes / _build_messages_from_plan
  → 消费端做分支判断
```

**问题**：
1. `[-2:]` 只保护 1 轮完整对话 + 当前 Q（`[-3:]` 修后变 2 轮 + 当前 Q）
2. bypass 集混了 bg_review 轮 → 吃了 3 个配额中的名额
3. engine 层恒有 bypass 分支 → 代码复杂度

### 第一次演进：plugin 层切分（已讨论但放弃）

```
pre_llm_call 中：
  反向扫 conversation_history → 数 3 个 user → 切上块/下块
  上块走 CA 管线
  下块原文 bypass
```

**放弃原因**：bg_review 在 conversation_history 上没有标记 → 切分器无法区分，纯位置切法会把 bg_review 计入 3 轮配额（见下面示例）

### 第二次演进：C-stage 写 biz_category（锁定 + 待实装）

参与 session `20260610_122644_bbb0b5` 设计决策（msg 22171-22172）：

```
biz_category VARCHAR
  位置：turn_cache + turn_plan 两表
  取值：bg_review:memory / bg_review:skill / bg_review:combined
  写入：C-stage process_turn_async() / write_turn()
  消费：A-stage _compute_assemble_plan() 读 biz_category
```

### 最终流程

```
pre_llm_call
  │
  ├─ _compute_assemble_plan()
  │     ├─ rebuild_messages_from_cache()
  │     ├─ 反向扫 l1_texts.keys()
  │     │    skip biz_category.startswith("bg_review")
  │     │    数满 N 个 real turn → bypass_turns
  │     │
  │     └─ 现有: topic grading + tail 保护 + plan 构建（只缩减 messages 列表）
  │
  ├─ _build_aligned_outcomes()（正向遍历 plan → 写入）
  │
  └─ 返回 None（replace 模式 mutate）
```

**方向分离原则**：判断（反向扫）≠ 写入（正向）。

---

## 二、bg_review 配额侵占场景

```
conversation_history 底部：
  T(n-3) 用户提问          ← real，需要保护
  T(n-2) bg_review user   ← biz_category="bg_review:memory"
  T(n-1) bg_review user   ← biz_category="bg_review:skill"
  Tn     用户提问          ← 当前 Q

[-3:] = T(n-2), T(n-1), Tn → 3 个中有 2 个 bg_review，只保护 1 个 real 轮 ❌

biz_category 感知后：
  反向扫：跳过 bg_review → 数 Tn(1), T(n-1)(skip), T(n-2)(skip), T(n-3)(2), T(n-4)(3-stop)
  bypass_turns = {T(n-4), T(n-3), T(n-1), Tn} → 保护 2 个 real 轮 ✅
```

---

## 三、实施路径（两阶段独立，一次 commit）

### 核心：双向嵌入 biz_category

```
turn_cache / turn_plan schema: + biz_category VARCHAR (默认 NULL)
C-stage: OODA JSON 输出中判定 bg_review → 即时写入
A-stage: bypass_turns 计算时反向扫 + skip biz_category
_bypass_skip: 从固定 min(3) 改为 len(bypass_turns)（动态）
```

### 连带修复（已知脆弱点）

| 脆弱点 | 修复 |
|--------|------|
| `_format_tool_group_assembly` 内部 `_found_group` 顺序计数 | 改为 per-turn `_api_call_count` mapping |
| `_build_aligned_outcomes` 的 `current_turn` 递增计数器 | 改为 `msg._turn_index` key-based lookup |
| `_build_messages_from_plan` bypass 分支 | 确认清理（engine 层不保留 bypass 逻辑） |

---

## 四、已决策事项（2026-06-11 会话收口）

| 问题 | 决策 |
|------|------|
| bg_review 判定信号来源 | C-stage L-stage 的 OODA JSON 输出（对话轮走 LLM，提取 JSON 字段判定）。非对话轮为代码规则生成，不走 LLM |
| biz_category 写入时机 | process_turn_async() 拿到 JSON 后即时解析写入，即「什么时候识别什么时候写」|
| 非 bg_review 轮 biz_category 值 | **NULL（不写）**。查询时 `biz_category IS NOT NULL` 过滤 |
| 阶段一 + 阶段二合并 | **一次 commit 批量实装**，便于调试 |
| `_build_aligned_outcomes` 脆弱点 | 列入实施计划，同步修复 |
| `_format_tool_group_assembly` 脆弱点 | 列入实施计划，同步修复 |

---

## 五、相关设计记录

| 记录 | 位置 |
|------|------|
| biz_category 命名与字段设计 | session `20260610_122644_bbb0b5` msg 22171-22172 |
| 已锁定清单 | session `20260610_122644_bbb0b5` msg 22172（12 项已锁定） |
| 三区尾保护 + 工具推导定理 | debug-20260610-20k-dialogue-tail-refactoring.md |
| bypass_turns `[-2:]→[-3:]` 代码改动 | 本会话 ca/__init__.py + recent-turns-bypass.md |
| OODA JSON 作为 C-stage 输入 | 本会话收口：OODA 输出 JSON → 解析 → biz_category |
| 已知脆弱点已列入计划 | `_format_tool_group_assembly` + `_build_aligned_outcomes` |
