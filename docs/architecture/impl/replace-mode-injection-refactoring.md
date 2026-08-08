> **⚠️ 历史存档 — v5.1 注入重构实现方案。`_build_aligned_outcomes`/`_format_tool_group_assembly` 已在 v5.10 移除，重构为 `_simple_mutation_mode_v5`。概念设计(行类型注射表)延续，函数名/实现方式已变。**

# Replace 模式注入重构方案（v5.0 → v5.1 修正版）

**文档版本**: v5.1
**编制日期**: 2026-06-10
**前置文档**:
- `design/ca-20k-dialogue-tail-redesign.md`（20K 对话保护区）
- `design/impl/`（本目录 — 实现方案）
- CA 插件 AGENTS.md（当前部署快照，`~/.hermes/profiles/tester/plugins/ca_assembler/AGENTS.md`）

---

## 0. 修订说明

基于 **系统审查报告**（2026-06-10 session）发现的 **2 项方案偏差、3 个遗漏项、1 个隐患**，对原 replace 模式注入方案做全面修正。

### 审查发现的偏差汇总

| # | 原方案主张 | 实际代码 | 修正方向 |
|---|-----------|---------|---------|
| S1 | 在 `_compute_turn_plan_v2` 加 bypass_turns 参数 | bypass 逻辑仅存在于 `_build_messages_from_plan`（append 模式路径），`_build_aligned_outcomes`（replace 模式路径）完全缺失 | 移至 `_compute_assemble_plan` 计算结果，通过 `_AssemblePlanResult` 传给两端 |
| S2 | 新建 `_format_tool_group_assembly` 替代 `_format_group_summary` | `_format_group_summary` 在 aligned_outcomes（L1897）+ messages_from_plan（L1605）两处使用 | 两处均换用新函数 |
| S3 | thought 80→100，硬截断→自然截断 | 确认 `_safe_truncate` 复用现有函数 | 不加改动 |
| S4 | 移除 tool 行分支 + `_merge_consecutive_tool_outcomes` | Fct tool 行（L1912-1920）仍在注入 `_format_single_tool`，×N 合并在后处理 | 删除整块，×N 合并移入 `_format_tool_group_assembly` |
| S5 | append 模式同步更新 | `_build_messages_from_plan` 中 `_format_group_summary`（L1605）仍未换 | 两路一起换 |
| S6 | 测试适配 | `test_bao_tool_l0_skips`（L358）断言 tool 行 `None` → 改为 `""` | 测试断言更新 |

---

## 1. 设计目标

### 1.1 目标

对 ContextAssembler v5.0 的 replace 模式注入路径（`_build_aligned_outcomes`）做结构化重构：

1. **消除独立工具行注入**：`tool` 行的 Fct 摘要不再作为独立行注入上下文，其信息合并到所在工具组的 `assistant{tc}` 行中
2. **统一 bypass 逻辑**：将 `_build_messages_from_plan` 已有的 bypass 计算上提至 plan 计算阶段，使两端一致
3. **×N 合并下沉**：同工具组连续相同工具的去重合并，从独立后处理函数移入工具组格式化函数内部

### 1.2 核心设计原则

| 原则 | 实现 |
|------|------|
| 与 plan 阶段解耦 | bypass_turns 由 `_compute_assemble_plan` 生产，两路消费 |
| 组内信息自包含 | 单个 `assistant{tc}` 摘要行 = 该组全部工具调用的完整摘要 |
| 前后兼容 | `_format_group_summary` 保留但标记 deprecated，指向新函数 |
| 行数稳定 | replace 模式 `outcomes` 列表与 `conversation_history` 1:1 对齐不变 |

---

## 2. 改动范围

### 2.1 文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `ca/__init__.py` | 修改 | 核心逻辑变更（4 个函数） |
| `AGENTS.md` | 同步 | 文档反映新架构 |
| `tests/test_aligned_outcomes.py` | 修改 | test_bao_tool_l0_skips 等断言更新 |
| 其他测试 | 只读确认 | 保证 `112 passed, 12 skipped, 0 failed` 基线不倒退 |

### 2.2 不涉及的文件

| 文件 | 原因 |
|------|------|
| `ca/tool_summarizer.py` | 工具摘要生成逻辑不变，仅是消费端重组 |
| `ca/store.py` | DB schema 不变 |
| `ca/config.py` / `settings.yaml` | 配置项不变 |
| `ca/post_process.py` | 解析器不变 |
| `plugins/ca_assembler/__init__.py` | 插件层无感知 |
| `ca/_build_messages_from_plan` | continue 分支已覆盖工具组 bypass |

---

## 3. 详细设计

### 3.1 S1: bypass_turns 上提至 Plan 阶段

#### 当前问题

`_build_messages_from_plan`（append/assembly 模式）内部硬计算 `_bypass_turns`（L1545-1548），但 `_build_aligned_outcomes`（replace 模式）**没有** bypass 感知。

`_build_aligned_outcomes` 中 assistant{tc} 的 bypass 检测依赖 `entry.target_level == "Elm"`（L1891），但工具组的 target_level 由 `dialogue_downgrade` 决定——不是 bypass 层级的语义。尾区的工具组即使应 bypass，target_level 也可能是 Fct，导致摘要替换而非原文保留。

#### 修正

在 `_AssemblePlanResult` 新增字段：

```python
@dataclass
class _AssemblePlanResult:
    plan: List[TurnPlanEntry]
    messages: List[Dict]
    stats: Any
    tokens_before: int
    bypass_turns: Set[int] = field(default_factory=set)  # 新增
    tail_protected_turns: Set[int] = field(default_factory=set)
```

`_compute_assemble_plan` 在计算 `tail_protected_turns` 后，同步计算 `bypass_turns`：

```python
# 在 _compute_assemble_plan 中 tail_protected_turns 计算之后追加
# 最后 2 个对话轮旁路
_dialogue_turns = sorted(set(e.turn_index for e in combined
                            if e.turn_type == "dialogue"))
bypass_turns = set(_dialogue_turns[-2:])
```

#### 消费端（_build_aligned_outcomes）

```python
def _build_aligned_outcomes(self, plan, conversation_history, bypass_turns=None):
    # ... 现有逻辑 ...
    for entry in plan:
        if entry.turn_index in (bypass_turns or set()):
            if entry.turn_type == "tool_group":
                # 强制 Elm：原文保留
                outcomes.append(None)
                continue
    # ...
```

#### 两路关系

| 路径 | bypass_turns 来源 | 行为 |
|------|------------------|------|
| `_build_aligned_outcomes` | `_AssemblePlanResult.bypass_turns`（D） | 工具组 entry target_level 覆盖为 Elm → None |
| `_build_messages_from_plan` | 已有内部计算（L1545）→ 用传参替代 | 继续走 continue 跳过，工具组被对话 bypass 全量注入覆盖 |

---

### 3.2 S2+S4: 新建 _format_tool_group_assembly

#### 当前问题

`_format_group_summary` 只输出一句话（"工具组：查文件→aaa（3个，ok）"），丢掉了单个工具调用的详细信息。

`_format_single_tool` + `_merge_consecutive_tool_outcomes` 在 aligned_outcomes 中作为独立步骤产生独立工具行——这些工具行占据了上下文槽位，但信息密度低（read_file: aaa (ok)）。

#### 新函数

```python
@staticmethod
def _format_tool_group_assembly(group_l1_json: str,
                                 conversation_history: List[Dict],
                                 turn_index: int,
                                 group_idx: int = 1) -> str:
    """格式化工具组为紧凑的 assistant{tc} 摘要行。
    
    输入：
      group_l1_json: {"group_intent":"查文件",
                       "group_result":"aaa;x.py",
                       "tool_count":3,
                       "state":"ok"}
      conversation_history, turn_index, group_idx: 用于从 history 提取
        各工具的 tool_name + hdl/fct 完成单个工具详情
    
    输出格式：
      【工具组:查文件→aaa;x.py(3个,ok)】
        search: *.py → 3 hits
        read_file: /tmp/test.py (60 lines) ×2
        read_file: /tmp/config.py (120 lines)
    
    组内 ×N 合并规则：
      - 连续相同 tool_name（按 `_merge_consecutive_tool_outcomes` 逻辑）
      - 去重后格式：`tool_name: detail ×N`
      - N=1 时不显示 ×1
    
    降级链：group_l1_json → tool_group_l0 → ""
    """
```

#### 替换关系

| 当前函数 | 替换为 | 使用位置 |
|---------|--------|---------|
| `_format_group_summary(fct)` | `_format_tool_group_assembly(fct, history, turn_idx, gidx)` | aligned_outcomes L1897 |
| `_format_group_summary(fct)` | `_format_tool_group_assembly(fct, history, turn_idx, gidx)` | messages_from_plan L1605 |
| `_format_single_tool(fct, hdl, content, name)` | ✅ 删除（信息并入 _format_tool_group_assembly） | aligned_outcomes L1912-1920 |
| `_merge_consecutive_tool_outcomes(outcomes, history)` | ✅ 删除（逻辑移入 _format_tool_group_assembly） | aligned_outcomes L1928 |

#### 行类型注入表（修正后）

| history 行 | Elm | Fct | Hdl |
|---|---|---|---|
| `user` | None（保留原文） | `_format_l1_for_display(fct)` | `l0_text` |
| `assistant{tc}` | None（保留原文） | `_format_tool_group_assembly(fct)` | `_format_tool_group_assembly(hdl)` → 紧凑格式 |
| `tool` | **""**（删除） | **""**（删除） | **""**（删除） |
| `assistant_fin` | None | None | None |

所有 `tool` 行无论注入级别均删除。工具信息完全由 `assistant{tc}` 行承载。

---

### 3.3 S5: Append 模式同步

`_build_messages_from_plan` 中 L1605：

```python
# 当前：
display = self._format_group_summary(fct) if fct else (hdl or "")

# 修正后：
display = self._format_tool_group_assembly(
    fct if fct else hdl,
    messages,
    entry.turn_index,
    entry.tool_sub_index if hasattr(entry, 'tool_sub_index') else 1
) if fct or hdl else ""
```

同样，新增函数需要注入足够信息（history + turn_index + group_idx）。

---

### 3.4 S6: 测试适配

**需要在 `test_aligned_outcomes.py` 中更新的断言**：

```python
# test_bao_tool_l0_skips (L358)
# 当前断言 tool 行返回 None：
assert outcomes[5] is None
# 修正后 tool 行返回 ""（空字符串将被删除）：
assert outcomes[5] == ""
```

**不涉及的测试**：
- turn_plan 逻辑不变的测试：全部 `test_v440.py`、`test_v460.py` 等地 plan 计算类测试
- aligned_outcomes 中 Elm bypass 测试：确认 entry.target_level==Elm 时 None 不变
- appended tool_group Fct 测试：不变

---

## 4. 实现步骤

### Step 1: _compute_assemble_plan — 新增 bypass_turns 字段

**操作**：
1. `_AssemblePlanResult` dataclass 新增 `bypass_turns: Set[int]`
2. `_compute_assemble_plan` 中，在 `tail_protected_turns` 计算后添加 `bypass_turns` 计算
3. 修改 return 语句，传入新字段

**验证**：`_AssemblePlanResult(plan=..., bypass_turns=bypass_turns)` 类型检查通过

---

### Step 2: 新增 _format_tool_group_assembly

**操作**：
1. 新建 `@staticmethod _format_tool_group_assembly(...)` 方法
2. 实现组内 ×N 合并（从 `_merge_consecutive_tool_outcomes` 移植核心逻辑）
3. 从 `conversation_history` 提取工具 rows 的 tool_name + hdl
4. 输出紧凑格式（含 group_intent + 各工具明细）

**接口**：
```python
@staticmethod
def _format_tool_group_assembly(
    group_l1_json: str,
    conversation_history: List[Dict],
    turn_index: int,
    group_idx: int,
) -> str:
```

---

### Step 3: _format_group_summary 标记 deprecated

**操作**：
1. 在 `_format_group_summary` docstring 末尾追加：
   ```
   已弃用 v5.1：由 _format_tool_group_assembly 替代。
   保留仅用于遗留测试引用。移除条件：全部 test_ 不再调用。
   ```
2. 从 `_build_aligned_outcomes` 和 `_build_messages_from_plan` 中删除引用

---

### Step 4: _build_aligned_outcomes — 重构工具行分支

**操作**：
1. 将 `role == "tool"` 分支（L1904-1922）整体替换为 `outcomes.append("")`
2. 删除末尾 `_merge_consecutive_tool_outcomes(outcomes, history)` 调用
3. 在 `role == "assistant" and tool_calls` 分支中（L1887-1902），将 `_format_group_summary(fct)` 替换为 `_format_tool_group_assembly(fct, conversation_history, entry.turn_index, group_idx)`
4. 接入 bypass_turns：在循环开始时判断 entry.turn_index in bypass_turns → 工具组 entry.target_level 覆盖为 Elm

---

### Step 5: _build_messages_from_plan — 同步更换

**操作**：
1. L1605 处 `_format_group_summary(fct)` → `_format_tool_group_assembly(fct, messages, entry.turn_index, sub_index)`

---

### Step 6: 测试更新

**操作**：
1. 更新 `test_aligned_outcomes.py` 中 tool 行为 `""` 的断言
2. 运行 `python -m pytest tests/test_aligned_outcomes.py -v`
3. 运行全基线：`tests/test_v440.py + test_v460.py + test_plugin.py`
4. 确认 112 passed, 12 skipped, 0 failed

---

### Step 7: AGENTS.md 同步

**操作**：
1. 更新「行类型注射规则」表：tool 行 Fct 从 `_format_single_tool` → `""`
2. 更新「三个格式化函数」表：新增 `_format_tool_group_assembly`，标注 `_format_group_summary` deprecated
3. 更新「×N 合并」说明：移至 `_format_tool_group_assembly` 内部
4. 更新「输出示例」：消除独立工具行

---

## 5. 输出示例对比

### 改前（v5.0）

```
# 对话轮 Fct
user: "查了文件系统\n  文件A"
# 工具组 Fct
assistant{tc}: "工具组：查文件→aaa；x.py（3个，ok）"
# 工具 Fct（3 个独立行）
tool: "search: *.py → 3 hit"
tool: "read_file: aaa (ok)"
tool: "read_file: x.py (ok)"  # ×N 合并为 ×2
# final assistant
assistant_fin: "文件内容已查到"
```

### 改后（v5.1）

```
# 对话轮 Fct
user: "查了文件系统\n  文件A"
# 工具组 Fct（内含全部工具详情 + ×N 合并）
assistant{tc}: "【工具组:查文件→aaa；x.py(3个,ok)】
  search: *.py → 3 hit
  read_file: aaa ×2"
# tool 行全部删除
# final assistant
assistant_fin: "文件内容已查到"
```

**效果**：
- 行数减少：4 行 → 2 行（user + assistant{tc}）
- 信息完整：工具详情从独立行移入 assistant{tc} 的附加行
- ×N 合并从后处理变为组内处理

---

## 6. 风险评估

### 6.1 annotation 模式兼容性

**结论**：无影响。

`_annotation_mode` 调用 `_build_aligned_outcomes` 后做 `parts = [o for o in outcomes if o]` 过滤：

| 改动后值 | 被 `if o` 过滤 | 效果 |
|---------|---------------|------|
| tool 行 → `""` | ✅ 过滤掉 | 正确（annotation 不需要空行） |
| assistant{tc} bypass → `None` | ✅ 过滤掉 | 正确（bypass 数据不进入 annotation） |
| assistant{tc} Fct/Hdl → 新格式文本 | ❌ 保留 | 正确 |

### 6.2 post_llm_call 快照恢复

**结论**：无影响。

Save/Restore 基于全量快照（`_saved_history_snapshot = [{**m} for m in conversation_history]`），按内容恢复，不按索引：

```python
# post_llm_call 恢复
conversation_history.clear()
conversation_history.extend(self._saved_history_snapshot)
```

行数变化（tool 行被删除）不影响恢复逻辑。

### 6.3 已知隐患 - 无

所有 6 项已纳入修正。

---

## 7. 归档信息

**保存位置**: OpenViking `resources/projects/context-assembler/design/impl/replace-mode-injection-refactoring.md`

**对应代码**: `~/.hermes/profiles/tester/plugins/ca_assembler/ca/__init__.py`
- `_AssemblePlanResult`（L223-228）
- `_build_aligned_outcomes`（L1850-1929）
- `_build_messages_from_plan`（L1540-1568）
- `_format_group_summary`（L1728-1796）
- `_format_single_tool`（L1799-1845）
- `_merge_consecutive_tool_outcomes`（L1940-1962）
