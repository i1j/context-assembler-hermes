# 实现技术方案 v2: 话题块取代固定 Token 尾区保护

## 1. 实现策略

将 `_compute_assemble_plan` 中的尾区保护机制从**固定 Token 累计**（`PROTECT_TAIL_TOKENS`）改为**话题块边界**保护，保留 10K 安全阀防止超长单话题撑爆预算。

**核心思路**：不再从最新轮反向累计 Token，而是利用已有的 `turn_to_topic`（话题分割结果）找到当前话题块，将其所有轮无条件设为 L2。如果当前话题块总 Token 超过 `PROTECT_TAIL_TOKENS`，退回到 Token 边界截断（仅最新 10K 为 L2，同话题超前部分进话题分级）。

**关键设计决策**（基于多视角评审）：
- bypss_turns 保持原位计算（plan 的 dialogue_entries[-3:]），不做提前
- 将 `_compute_current_topic_turns()` 提取为独立方法，可单元测试
- 安全阀回退逻辑精简版：复用 `_token_estimate` 循环，不做两套实现
- `_available_budget` 传入话题块轮集合，标注预算影响

## 2. 文件级改动清单

| 操作 | 文件路径 | 改动内容 |
|------|---------|---------|
| 修改 | `ca/__init__.py` — `_compute_assemble_plan` | 移除 L995-1010 `_tail_protected_turns` Token 累计逻辑；替换为独立方法调用 |
| 新增 | `ca/__init__.py` — `_compute_current_topic_turns()` | 提取为独立方法 |
| 修改 | `ca/__init__.py` — `_compute_turn_plan_v2` 传参 | 传参语义换（`_current_topic_turns`），签名不改变量名 |
| 修改 | `ca/__init__.py` — `_compute_turn_plan_v2` 函数体 | 决策理由 `"tail"` → `"current_topic"`；更新 docstring |
| 修改 | `ca/__init__.py` — `_available_budget` 传参 | 传 `_current_topic_turns`（语义不同但计算逻辑不变） |
| 修改 | `ca/__init__.py` — L1049 日志 | `"tail_protected=%d"` → `"current_topic=%d"` |
| 不修改 | `ca/config.py` — `PROTECT_TAIL_TOKENS` | 保留做安全阀配置（注意：实际默认值 10000 非 20000，YAML→env 映射断链历史问题，不做本方案范围） |
| 不修改 | `ca/__init__.py` — `_compute_tail_start()` | 已弃用但非本方案范围 |
| 不修改 | `ca/__init__.py` — `_compute_tool_plan_v2` | 不受影响（只依赖 plan 中的 `target_level`，不直接使用保护轮集合） |
| 修改 | `AGENTS.md` | 更新 Zone 描述和 `_compute_turn_plan_v2` 决策规则 |
| 修改 | 测试文件 | 回归适配（4 处 `decision_reason="tail"` → `"current_topic"`）|
| 新增 | 测试（话题块保护专用） | 见 §6 测试计划 |

## 3. 接口设计

### 3.1 新增方法签名

```python
def _compute_current_topic_turns(
    self,
    turn_to_topic: Dict[int, int],
    bypass_turns: Set[int],
    messages: List[Dict],
    threshold: int = 10000,
) -> Set[int]:
    """计算当前话题块受保护轮集合。

    从最新非bypass轮开始，找到其话题ID，该话题ID下所有非bypass轮组成保护集。
    如果保护集总Token超过threshold，仅保留最新threshold内的轮（按多退少补截断）。

    Returns:
        受保护轮集（空集表示无保护）
    """
```

### 3.2 数据流

```
_compute_assemble_plan 内部:

旧流程:
  messages → Token累计 → _tail_protected_turns
  l1_texts → 话题分割 → turn_to_topic → 话题分级 → topic_grades
  _tail_protected_turns + topic_grades → _compute_turn_plan_v2 → plan

新流程:
  messages → 无 Token 累计
  l1_texts → 话题分割 → turn_to_topic → 话题分级 → topic_grades
               ↓
             话题分割 + 话题分级之后（bypass_turns 计算前）
               ↓
             assemble() → plan → _dialogue_entries[-3:] → bypass_turns
               ↓
             _compute_current_topic_turns(turn_to_topic, bypass_turns, messages, threshold)
               ↓
  _current_topic_turns + topic_grades → _compute_turn_plan_v2 → plan
               ↓
  plan + bypass_turns → _AssemblePlanResult
```

**关键**：bypass_turns 保持原位计算（L1069 不变），`_compute_current_topic_turns` 在 plan 之后、传给 `_compute_turn_plan_v2` 之前调用。

### 3.3 预算影响

新逻辑下 `_available_budget` 传入的 `_current_topic_turns` 可能比旧 `_tail_protected_turns` 包含更多轮（整个话题块 vs 10K Token）。计算逻辑不变（仍按 `turn in set` 累计 Token），结果是 **Zone ③ 升级预算可能减少**。定量影响：

| 场景 | 旧尾区 Token | 新话题块 Token | Zone ③ 减少 |
|------|------------|---------------|------------|
| 单话题 < 10K | 全部保护 | 全部保护 | 无变化 |
| 多话题，当前话题 < 10K | 10K（含部分上一话题） | 当前话题块（可能 3-8K） | **减少 2-7K** |
| 超长单话题 > 10K | 10K | 10K（安全阀截断） | 无变化 |

Zone ③ 减少 2-7K 时，升级预算仍 > 20K（当前对话窗口默认 50K × 0.95 - bypass ≈ 40K+），**风险可接受**。

## 4. 关键实现细节

### 4.1 _compute_current_topic_turns 实现

```python
def _compute_current_topic_turns(
    self,
    turn_to_topic: Dict[int, int],
    bypass_turns: Set[int],
    messages: List[Dict],
    threshold: int = 10000,
) -> Set[int]:
    """计算出话题块受保护轮集合（独立方法，可单元测试）。"""
    if not turn_to_topic:
        return set()

    # 1. 找最新非bypass轮 → 确定当前话题ID
    _non_bypass = [t for t, tid in turn_to_topic.items() if t not in bypass_turns]
    if not _non_bypass:
        return set()
    _latest_turn = max(_non_bypass)
    _current_topic_id = turn_to_topic.get(_latest_turn)
    if _current_topic_id is None:
        return set()

    # 2. 该话题下所有非bypass轮
    _topic_turns = {
        t for t, tid in turn_to_topic.items()
        if tid == _current_topic_id and t not in bypass_turns
    }
    if not _topic_turns:
        return set()

    # 3. 安全阀：超threshold则截断（仅最新threshold内的轮）
    _total_tk = sum(
        self._token_estimate(m.get("content", ""))
        for m in messages
        if m.get("_turn_index", -1) in _topic_turns
        and m.get("role") not in ("system", "tool")
        and "tool_calls" not in m
    )
    if _total_tk <= threshold:
        return _topic_turns

    # 超限：从最新轮逆序累加，不超过threshold
    _result: Set[int] = set()
    _acc = 0
    for _turn in reversed(sorted(_topic_turns)):
        _tk = sum(
            self._token_estimate(m.get("content", ""))
            for m in messages
            if m.get("_turn_index", -1) == _turn
            and m.get("role") not in ("system", "tool")
            and "tool_calls" not in m
        )
        if _acc + _tk > threshold:
            break
        _acc += _tk
        _result.add(_turn)
    return _result
```

### 4.2 _compute_assemble_plan 中调用

```python
# 移除 L995-L1010 旧 Tail 累计逻辑

# 确定 bypass_turns（原位计算，在 plan 之后）
# ... 话题分割、检索、分级等不变 ...
# plan = self._compute_turn_plan_v2(...) ← 此时 _current_topic_turns 已传入

# bypass_turns 计算（保持 L1069 不变）：
_dialogue_entries = [e for e in plan if e.turn_type == "dialogue"]
_bypass_turns: Set[int] = {e.turn_index for e in _dialogue_entries[-3:]}

# 新增：_compute_current_topic_turns（在话题分割+分级之后，plan之前调用）
_current_topic_turns = self._compute_current_topic_turns(
    turn_to_topic, _bypass_turns, messages, Config.PROTECT_TAIL_TOKENS,
)
```

### 4.3 _compute_turn_plan_v2 决策逻辑

```python
# 旧 (L1486-1488):
# if in_tail:
#     entry.target_level = "L2"
#     entry.decision_reason = "tail"

# 新:
if turn in _current_topic_turns:
    entry.target_level = "L2"
    entry.decision_reason = "current_topic"
```

### 4.4 其他代码更新

```python
# L1049 日志：
logger.info(
    "[CA] budget_remaining=%d < 20K: upgrade budget critically low, "
    "context_length=%d current_topic=%d",
    _budget, context_length, len(_current_topic_turns),
)

# _compute_turn_plan_v2 docstring (L1444-1457)：
# "turn in tail_protected_turns → L2 (tail)" → "turn in current_topic_turns → L2 (current_topic)"
# 工具组规则同理

# _compute_tail_start 弃用注释 (L2167)：
# 更新为 "由 turn_cache 路线取代——见 _compute_assemble_plan 中 current_topic_turns 计算"
```

### 4.5 bypass_turns 与 _available_budget 的时序

```
话题分割 + 分级 (保持不变)
    ↓
_current_topic_turns = _compute_current_topic_turns(turn_to_topic, set(), messages)  # 第一次：bypass为空
    ↓
_budget = _available_budget(..., _current_topic_turns, ...)
    ↓
plan = _compute_turn_plan_v2(..., _current_topic_turns, ...)
    ↓
_bypass_turns = {e.turn_index for e in _dialogue_entries[-3:]}  # 原位计算
    ↓
_current_topic_turns = _compute_current_topic_turns(turn_to_topic, _bypass_turns, ...)  # 第二次：排除bypass
    ↓
返回 _AssemblePlanResult(plan, ..., bypass_turns=_bypass_turns, ...)
```

**时序注意**：`_compute_current_topic_turns` 被调用两次：
1. **第一次**（在 `_available_budget` 和 `_compute_turn_plan_v2` 之前）：bypass_turns 尚为空，计算包含 bypass 轮在内的"原始"当前话题块，用于预算扣除（因为 bypass 轮原文在 budget 中不计入，扣掉整话题块更保守安全）
2. **第二次**（在 `_bypass_turns` 计算之后）：排除 bypass 轮后的纯净当前话题块，用于返回 `_AssemblePlanResult`

### 4.6 边界条件处理

| 条件 | 处理 |
|------|------|
| 全部轮都在 bypass 中（≤3 轮） | `_non_bypass` 为空 → `_current_topic_turns` 为空 → 全 bypass，无话题保护 |
| 话题分割失败（空 `turn_to_topic`） | `_current_topic_id` 为 None → `_current_topic_turns` 为空 → 全话题分级 |
| 最新非 bypass 轮属于 biz_cats（bg_review） | biz_cats 轮不在 topic_data 中 → turn_to_topic 中没有它们 → 自动跳过 ✓ |
| 当前话题块恰好等于 bypass（新话题刚起步，仅 bypass 中 3 轮） | `_non_bypass` 取到上一话题最后一轮 → 当前话题 = 上一话题，新话题首轮无保护 |
| PROTECT_TAIL_TOKENS=0 | 安全阀 threshold=0 → `_acc + _tk > 0` 永远成立（`_tk` ≥ 0 且首轮 ≥ 0，min token 估计通常 ≥ 1）→ `_result` 为空 → 全话题分级 |
| 安全阀激活后话题内保护 + 不保护部分 | Topic grade 对话题内所有轮相同。保护部分→L2，不保护部分→同 grade（大概率 L1/L0） |

## 5. 接口契约（供测试线）

| 接口 | 签名 | 预期行为 |
|------|------|---------|
| `_compute_current_topic_turns(turn_to_topic, bypass_turns, messages, threshold)` | `(Dict[int,int], Set[int], List[Dict], int) → Set[int]` | 返回受保护轮集；安全阀激活时截断；可单元测试 |
| `_compute_assemble_plan(user_message, context_length)` | 签名不变 | 内部状态同 v1，`plan` 的决策理由 `"tail"` → `"current_topic"` |
| `_compute_turn_plan_v2(...)` | 签名不变，传参集合语义换 | 检查名从 `in_tail` → `turn in _current_topic_turns` |
| `_available_budget(...)` | 签名不变 | 传入集合变大 => 预算减少，幅度可控 |

## 6. 测试计划

### 6.1 新增单元测试（`test_compute_current_topic_turns`）

| 测试用例 | 输入 | 预期 |
|---------|------|------|
| 单话题 < 10K | `turn_to_topic={1:1,2:1,3:1}`, bypass=空, threshold=10000 | 返回 {1,2,3} |
| 单话题 > 10K 安全阀激活 | `turn_to_topic={1:1,2:1}`, 消息 2 轮各 8K token | 仅最新 1 轮在返回集 |
| bypass 在最新话题中 | `turn_to_topic={1:1,2:1,3:1,4:2}`, bypass={3,4} | 返回 {1,2}（最新非 bypass=2→topic=1） |
| 空 turn_to_topic | `turn_to_topic={}` | 返回空集 |
| 全 bypass | 全部 turn 在 bypass 中 | 返回空集 |

### 6.2 回归测试适配

| 文件 | 行 | 改动 |
|------|----|------|
| `tests/test_a.py:256` | `"tail"` → `"current_topic"` | 断言变化 |
| `tests/test_aligned_outcomes.py:339,343` | `decision_reason="tail"` → `"current_topic"` | 构造数据变化 |
| `tests/test_v440.py:291` `test_TC_A_020_tail_protection` | `PROTECT_TAIL_TOKENS=50` 适配 | 在新逻辑下话题保护覆盖当前话题块，需构造对应数据 |
| `tests/test_v440.py:311` `test_TC_A_021_topic_boost_tools` | `PROTECT_TAIL_TOKENS=100000` 适配 | 尾轮不再吸收 tail，当前话题块整体 L2 |
| `tests/test_v460.py:TestComputeTurnPlanV2` | **取消 ** 该类的 `pytest.mark.skip`，适配 `tail_protected_turns` 参为 `current_topic` | 核心单元测试恢复 |

### 6.3 集成测试场景

| 场景 | 构造方法 | 验证 |
|------|---------|------|
| 话题块跨多轮保护 | 5 轮同话题，设 L1 JSON 同 topic_id | 全部 decision_reason="current_topic" |
| 安全阀激活 | 5 轮同话题每轮 3K token、threshold=3000 | 仅最新 1-2 轮受保护，其余进分级 |
| 话题切换保护 | 3 轮话题 A + 3 轮话题 B | 话题 B 全保护，话题 A 进分级 |

## 7. 实施步骤

1. **新增 `_compute_current_topic_turns` 方法** — 按 4.1 实现 — `python -c "import ast; ast.parse(open('ca/__init__.py').read())"`
2. **修改 `_compute_assemble_plan`** — 移除 L995-L1010 Token 累计；插入调用两次 `_compute_current_topic_turns` — 语法检查
3. **修改 `_compute_turn_plan_v2` 传参** — L1052-1057 传 `_current_topic_turns` — 语法检查
4. **修改 `_compute_turn_plan_v2` 内部** — 决策理由 `"tail"` → `"current_topic"` — 语法检查
5. **修改 `_available_budget` 传参** — L1043 — 语法检查
6. **更新日志/docstring/注释** — L1049 日志、L1444-1457 docstring、L2167 弃用注释 — 语法检查
7. **跑测试** — `python -m pytest tests/test_v460.py tests/test_pr3_injection.py tests/test_aligned_outcomes.py tests/test_v440.py tests/test_a.py -v -p no:cacheprovider -o "addopts="`
8. **更新 AGENTS.md**

## 8. 回退方案

- `git checkout -- ca/__init__.py` 恢复
- `PROTECT_TAIL_TOKENS` 配置兼容留存（即使不启用也不影响旧逻辑回退）

## 9. 多视角审查处理记录

### A 完整性视角 — 开发线自查

| # | 意见 | 处置 | 理由 |
|---|------|------|------|
| 1 | **bg_review 轮混入 bypass_turns** — _dialogue_turns 含 biz_cats | **采纳** | bypass_turns 保持原位计算（plan 的 dialogue_entries[-3:]），天然过滤 bg_review |
| 2 | tool_plan 交互未提及 | **采纳** | 已补充 §2 确认不受影响 |
| 3 | _available_budget 预算影响未评估 | **采纳** | 已补充 §3.3 定量评估 |
| 4 | "话题块仅1轮在bypass中"边界缺失 | **采纳** | 已补充 §4.6 第 4 行 |
| 5 | 测试策略缺失 | **采纳** | 已补充 §6 完整测试计划 |
| 6 | AGENTS.md 具体内容未给出 | **采纳** | 已在实施步骤中注明 |
| 7 | PROTECT_TAIL_TOKENS 默认值 10000 非 20000 | **采纳** | 注明历史问题，不放方案范围 |
| 8 | _compute_tail_start 弃用方法未清理 | **不采纳** | 非本方案范围 |

### B 设计合理性视角 — 开发线自查（含 codegraph 对照源码）

| # | 意见 | 处置 | 理由 |
|---|------|------|------|
| 1 | bypass_turns 计算结果可能差异（含 bg_review 时） | **采纳** | 见 A-1，保持原位计算解决 |
| 2 | logger.warning 未纳入修改 | **采纳** | §4.4 已补充 |
| 3 | docstring 未更新 | **采纳** | §4.4 已补充 |
| 4 | _compute_tail_start 弃用注释 | **采纳** | §4.4 已补充 |
| 5 | _biz_cats 处理不一致 | **采纳** | 被 A-1 覆盖 |

### C 必要性视角 — 开发线自查（含 web_search）

| # | 意见 | 处置 | 理由 |
|---|------|------|------|
| 1 | 安全阀回退重复实现旧代码 | **采纳** | 改为精简截断（§4.1 第 3 步），不重写两套逻辑 |
| 2 | _available_budget 隐式语义变更 | **采纳** | §3.3 已标注影响 |
| 3 | bypass_turns 提前计算不必要 | **采纳** | 改回原位计算（§3.2 数据流图修正） |
| 4 | TestComputeTurnPlanV2 skip 未启用 | **采纳** | §6.2 已列入取消 skip |

### D 可测试性视角 — 测试线评审

| # | 意见 | 处置 | 理由 |
|---|------|------|------|
| 1 | 话题块判定依赖不可控的话题分割 | **采纳** | 提取 `_compute_current_topic_turns` 独立方法，可传入 mock 的 `turn_to_topic` |
| 2 | 安全阀边界难以隔离 | **采纳** | 独立方法后可直接 mock threshold 参数 |
| 3 | bypass+话题块混合需多轮构造 | **采纳** | §6.1 已补充对应的集成测试场景 |
| 4 | 5 个回归测试需适配 | **采纳** | §6.2 已列出 |

## 自检

- [x] 所有需求点有对应实现方案
- [x] 接口设计完整（新增 `_compute_current_topic_turns` 独立方法）
- [x] 边界条件和错误处理有方案（6 种边界全覆盖）
- [x] 实施步骤可执行（8 步）
- [x] 多视角评审 4 视角已闭环
- [x] 回退方案有兜底
