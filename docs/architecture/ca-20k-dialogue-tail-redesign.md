> **⚠️ 历史存档 — v4.4/v5.1 预算-尾区设计方案。v5.10 已改用 topic-aware 替换 + `_simple_mutation_mode_v5` (plugin)。详见 TP-002。**

# CA 20K 对话保护区重构方案

**日期**: 2026-06-10（最终修订）
**前置文档**: `ca-budget-resummary-redesign.md`（预算-再摘要体系设计约束）

---

## 1. 背景

CA 当前尾部保护机制存在多处断裂：

| # | 问题 | 状态 |
|---|------|------|
| 1 | **配置多层定义** — `settings.yaml` 10K、`config.yaml` 20K、`plugin.yaml` 10K，各说各的 | ✅ 已修：仅 `settings.yaml` 为唯一源，`protect_tail_tokens: 20000` |
| 2 | **`tool_tail_turns` 冗余** — 独立于三区模型的又一套工具保护 | ✅ 已定：移除，Zone ② 内工具统一降一级 |
| 3 | **`_available_budget()` 断接** — 正确计算的预算从未接入 plan 决策 | ⚠️ 待接入：`_compute_assemble_plan()` 调用点绑定 |
| 4 | **尾部计数方案过时** — `_compute_tail_start()` 逐消息遍历跳 tool，应该从 turn_cache 取 `l2_tokens` 在 turn 级直接累加 | ✅ 已定：缓存查询方案 |

---

## 2. 消息尾部三区模型

```
← 最新                              最旧 →
┌──────────────┬──────────────┬──────────────────────┐
│  Zone ①      │  Zone ②      │  Zone ③              │
│  旁路区       │  对话保护区   │  更早区               │
│              │              │                      │
│ 最后 2 个    │  从倒数第 3  │  话题定级              │
│ dialogue     │  dialogue    │  对话 Fct/Hdl          │
│ turn         │  turn 起     │  工具低一级           │
│ Raw bypass   │  累计 ≤ 20K  │                      │
│ _original_   │  dialogue    │                      │
│ messages     │  token       │                      │
│              │              │                      │
│ 对话: raw    │  对话: Elm    │  对话: 话题分级       │
│ 工具: raw    │  工具: Fct    │  工具: 低一级         │
│              │              │                      │
│ 不计入 20K   │  只算 dialogue│                     │
│ 预算         │  token       │                      │
└──────────────┴──────────────┴──────────────────────┘
```

### Zone ① 旁路区（不改）

- 锁定最后 2 个 dialogue turn
- 从 `_original_messages` 快照全量注入（Hermes 原始数据，CA 未改动的副本）
- **不计入 20K 预算**
- 其工具轮随对话轮旁路全量附带

### Zone ② 对话保护区（重构目标）

- **起点**: 从倒数第 3 个 dialogue turn 开始向前累加
- **数据源**: `turn_cache.l2_tokens` — 只查 `turn_type='dialogue'` 的行
  - 工具轮（`turn_type='tool_group'`）被 SQL 过滤，不参与计数
  - = 天然只算 dialogue token，不需要逐消息跳过判断
- **预算**: 累加 `l2_tokens ≤ 20K`
- **边界规则**: 整轮进出。每轮累加完再判断是否超过 20K：
  - 累计 ≤ 20K → 该轮加入 `tail_protected_turns`
  - 累计 > 20K → 该轮及更早全部退到 Zone ③

```
示例（对话轮 l2_tokens）：
T-3  8K  → 累加 8K   (≤ 20K) → Zone ②
T-4  9K  → 累加 17K  (≤ 20K) → Zone ②
T-5  5K  → 累加 22K  (> 20K) → Zone ③ ← T-5 及更早
T-6  7K  → (不进了)          → Zone ③
```

- **级别分配**:
  - 对话 → `target_level = Elm`, `decision_reason = "tail"`
  - 工具 → `target_level = Fct`, `decision_reason = "dialogue_downgrade"`

### Zone ③ 更早区（不改）

- 话题定级决定对话级别（Fct / Hdl）
- 工具永远比所属对话轮低一级（Fct → Hdl, Hdl → skip）

---

## 3. 涉及文件与改动

### 3.1 核心逻辑 — `ca/__init__.py`

| 位置 | 改动 | 类型 |
|------|------|------|
| `_compute_assemble_plan()` L911-1040 | 在 cache snapshot（L954）后新增：从 turn_cache 查 dialogue turn 的 `(turn_index, l2_tokens)`，跳过最后 2 轮，累加得 `tail_protected_turns: set[int]` | 新增 |
| | 将 `tail_protected_turns` 传入 `_compute_turn_plan_v2()` 替代旧的 `tail_start` + `tool_tail_turns` | 重构 |
| | `budget=0` 硬编码（L1014）→ 改用 `_available_budget()` 返回值 | 接入 |
| `_compute_tail_start()` L1983-1996 | **废弃** — 原函数逐消息遍历计数的方案已不再使用。可删除或标记废弃 | 清理 |
| `tool_tail_turns` L964-968 | **移除** — `TOOL_TAIL_TURN_COUNT` 及相关计算全部删除 | 清理 |
| `_compute_turn_plan_v2()` L1419-1467 | 用 `turn_index in tail_protected_turns` 替代 `_tail_start_turn` 判断 | 同步 |
| `_compute_turn_plan_v2()` L1492-1512 | 工具降级分支去掉 `tool_tail_turns` 引用，Zone ② 内工具统一走 `dialogue_downgrade` | 同步 |
| `_available_budget()` L1022-1062 | 确认 TAIL_TOKEN_BUDGET 语义与新的 20K 预算兼容（预算 = context_length - overhead - TAIL_TOKEN_BUDGET） | 适配 |

### 3.2 配置统一

| 文件 | 操作 |
|------|------|
| `ca/settings.yaml` | `protect_tail_tokens: 10000 → 20000` | ✅ 已改 |
| `plugin.yaml` | `protect_tail_tokens` 段删除 | ✅ 已改 |
| `~/.hermes/profiles/tester/config.yaml` | `protect_tail_tokens: 20000` 行删除 | ✅ 已改 |

**配置数据流**:
```
settings.yaml: protect_tail_tokens: 20000
       ↓（代码加载为 env var 默认值）
ca/config.py: os.environ.get("CA_PROTECT_TAIL_TOKENS", "20000")
       ↓（如果设置了 env var 会覆盖）
Code: Config.PROTECT_TAIL_TOKENS
```

---

## 4. 不改的部分

- `_build_messages_from_plan()` 旁路注入逻辑（L1562-1587）— Zone ① 和 Zone ② 的边界在 plan 阶段已确定，注入阶段只按 plan 执行
- `_build_messages_from_plan()` 的 dialogue_downgrade 分支 — 已是工具降一级的正确逻辑
- topic grading 函数 — Zone ③ 逻辑不变
- 数据库 schema（turn_cache / turn_plan）— 无变更
- `turn_plan` 的 `TurnPlanEntry` 结构 — `tail_protected_turns` 是本函数内计算的局部变量，不改变 plan entry 结构

---

## 5. 测试要点

| 测试场景 | 预期 |
|----------|------|
| 短会话（< 20K dialogue tokens） | 旁路区之后的全部 dialogue 轮进入 Zone ② |
| 长会话（>> 20K） | 20K 内 dialogue Elm，之后按话题定级 |
| tool 密集会话 | tool_group 的 l2_tokens 不计入 20K，Zone ② 内工具全 Fct |
| bypass 区正好 2 个 dialogue turn | 第 3 个 turn 开始累加 20K |
| bypass 区不足 2 个 turn（新会话） | 余量也旁路，从 bypass 区前一个开始累加 20K |
| 20K 边界穿越 | 整轮进出：累加 > 20K 的那一轮本身不进 Zone ② |
| `protect_tail_tokens` 变更 | 只改 settings.yaml 一处生效，其余位置无覆盖 |
| `tool_tail_turns` 移除后 | 不再有工具获得 Zone ② 内的 Elm 特权 |
| `_compute_tail_start()` 废弃后 | 旧函数不再被任何调用点引用 |

---

## 6. 验证方法

1. **单测** — 跑现有测试套件确认不改的部分无回归
2. **新测试** — 构造 `turn_cache` mock：
   - `turn_type='dialogue'` + `l2_tokens` 已知值
   - `turn_type='tool_group'` 行不参与计算
   - 验证不同 token 量下的 `tail_protected_turns` 边界
3. **全量模拟** — 用历史 session 数据（如 20260605_210329 的 4019 行 DB）模拟新算法，对比旧算法的保护范围差异
4. **部署后观察** — ca_cache `turn_plan` 表的 `target_level` 分布，确认 Zone ② 对话全 Elm、工具全 Fct

---

## 7. 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-10 | 初版 — 消息遍历方案 |
| v2 | 2026-06-10 | 改用 turn_cache.l2_tokens 方案。`_compute_tail_start()` 废弃。配置统一为 settings.yaml 唯一源 |
