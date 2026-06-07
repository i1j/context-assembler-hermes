# CA 调试报告修复验证工作流

> 2026-06-05 实测模式 — 验证上轮调试报告中发现的问题是否已通过代码提交修复

## 触发条件

- 完成后对当前工作区进行 git 提交后
- 用户要求「查验调试报告中发现的问题是否已经解决」
- 或从源项目同步修复后、部署验证前

## 核心原则

1. **代码验证必要但不充分**。用户明确纠正过「光验证代码不解决问题。后面再观察吧」——找到修复代码不等于问题已解决。真正的验证需要运行/观察：跑测试、查日志、看运行时行为。代码还原只是起点，行为验证才是终点。

2. **双文件文档结构**：CA 插件目录同时存在 `AGENTS.md`（大写，部署总览/变更日志/改进记录）和 `agents.md`（小写，技术手册/Hook API/存储结构/运行时命令）。验证前两个都读一遍，获取互补视图。AGENTS.md 给 what changed，agents.md 给 how it works。

3. **交叉验证（≥2 源一致）**：每个结论必须基于 ≥2 个证据源（见第4节）。

## 工作流

### 1. 前置读取

先读当前插件目录下的入口文档（agents.md / AGENTS.md），获取：
- 当前版本号
- 已知修复列表
- 调试报告存放位置

**铁律**：先读 agents.md，再读调试文档。用户明确纠正过「你先把插件目录下的agents.md读一遍也好啊」。

**关键陷阱：调试报告快照退化**。调试报告中的「遗留项目录」/「已知遗留问题」状态表会迅速过时——它记录的是上次验证时的事实，但后续迭代（ToolSummarizer 改进、指纹去重实现等）可能已闭环而文档未更新。**验证时不能照搬这些表做当前状态**，必须逐条与当前代码交叉核对。

典型信号：条目标注"待实现/待修复"但对应方法已在代码中正常实现。发现此类过时条目，先标记「已过时」再继续验证，不重复验证。

（学习来源：2026-06-14 用户指出 `debug-post-fix-review.md` 中指纹去重标为"待实现"但代码已实现 `_deduplicate_messages()`。）

### 2. 编译问题清单

读 `docs/` 下所有调试/报告文档（`debug-*.md`、报告文件），提取每份文档中列出的问题：

| 问题 | 来源文档 | 优先级 | 修复状态(文档称) |
|------|---------|--------|-----------------|

注意文档中可能已有部分标注为「已修复」——这些仍需**代码验证**，不信任文档自述。

### 3. 逐条代码验证

对每条问题执行：

```
grep / search_files / read_file              → 验证当前代码（read the actual code）
python -c "import sqlite3; conn = ..."        → 查运行时 DB 数据验证行为
```

#### 验证要点

| 问题类型 | 验证方法 |
|---------|---------|
| **配置类** (context_length/env) | `ca/config.py:context_length_for_model()` → 查 context_length 管线 |
| **并发/竞态** (executor shutdown) | `ca/cache.py:_submit_rebuild()` 查 `_destroyed` 守卫 |
| **去重/逻辑** (dedup) | `_deduplicate_messages()` / `_msg_fingerprint()` 当前代码 |
| **检索/过滤** (empty summary) | `retrieval.py` 查过滤逻辑 + `tool_summarizer.py` 空摘要生成 |
| **摘要/降级** (LLM fallback) | `_run_c_stage` 查 fallback 路径 + 系统消息处理 |
| **预算计算** | `_available_budget()` 实测运行时值（查 DB token_offset + assemble_status） |

### 4. 交叉验证（≥2 源一致）

每个结论必须基于 ≥2 个证据源：

- 代码内容（当前文件实际是什么）
- 配置值（config.py/env 默认值）
- 运行 DB 数据（turn_cache、turn_plan 的 token_offset/assemble_status）
- 运行日志（CA_STAGE 日志行）

### 5. 输出报告

按状态表输出：

| 状态 | 含义 |
|------|------|
| ✅ 已修复 | 代码核实 ≥2 源一致 |
| ⚠️ 部分修复 | 核心场景修复，有残余风险 |
| ❌ 未修复 | 无任何相关修复提交或逻辑 |

每行附**具体代码位置**（文件名+行号）以支持后续追踪。

## 关键管线代码位置

### context_length 计算链

| 步骤 | 代码位置 | 说明 |
|------|---------|------|
| context_length_for_model() | `ca/config.py:100-141` | 三级回退：Hermes 运行时 → 自有查表 → 默认值 150K |
| Hermes 运行时屏蔽 | `ca/config.py:117` | `if False` 调试屏蔽，强制走自有查表 |
| 自有查表 | `ca/config.py:126-131` | 精确匹配 → 子串匹配 → 无匹配走兜底 |
| 兜底 | `ca/config.py:134-136` | 未知模型走 `CONTEXT_LENGTH=150000`（ca/config.py:64） |
| 压缩预算 | `ca/config.py:138` | `result = int(window * COMPRESSION_THRESHOLD)`，默认阈值 0.50 |
| 插件层设置 | `plugins/__init__.py:226-228` | 从 Hermes hook 的 `model` 参数获取模型名调用 `context_length_for_model()` |
| Hermes 可能覆盖 | `plugins/__init__.py:262` | `context_length = kwargs.get("context_length", self._context_length)` — 若 Hermes 传入则用其值 |
| 引擎接收 | `ca/__init__.py:569-571` | `assemble(user_message, context_length)` — None 时用默认 |

### _system_overhead 测量链（已弃用 — 测量代码于 2026-06-14 移除）

| 步骤 | 代码位置 | 说明 |
|------|---------|------|
| 默认值 | `ca/__init__.py:205` | `self._system_overhead = 20000` — 仅保留作保守缓冲区 |
| ~~动态测量~~ | ~~`plugins/__init__.py:324-332`~~ | ~~已移除。~~ Hermes 不暴露 tool schemas 等非消息开销，测量范围过窄且反作用 |
| `set_system_overhead()` | `ca/__init__.py:715-718` | 已标记弃用，保留方法签名以防外部调用 |
| 预算消耗 | `ca/__init__.py:712` | `used = system_tokens + head_tokens + tail_tokens + self._system_overhead` |

> 详见 `docs/system-overhead-measurement-analysis.md`。

### 预算计算管线

```python
# ca/__init__.py:674-713
def _available_budget(self, context_length, messages, ...):
    # 只计：system + head(L1摘要) + tail(L2原文) + system_overhead
    # 不计：middle L0、tool middle L0
    used = system_tokens + head_tokens + tail_tokens + self._system_overhead
    return max(0, int(context_length * 0.95) - used)

# ca/__init__.py:630-632 — 预算仅约束检索升级
budget = self._available_budget(...)
dial_upgrades = retriever.retrieve(...) if budget > 0 else []
tool_upgrades = retriever.retrieve_tools(...) if budget > 0 else []
```

### 各 Bug 代码位置

| Bug | 修复位置 |
|-----|---------|
| Executor shutdown 竞态 | `ca/__init__.py:246` destroy() 和 `ca/__init__.py:1152` reset() 改用 cache.destroy() |
| Head 保护区方向 | `ca/__init__.py:667` `[:HEAD_AUTO_L1_COUNT]` |
| terminal 错误标记 | `ca/tool_summarizer.py:183-191` `_ERROR_RE` |
| search_files 目录 | `ca/tool_summarizer.py:371-386` `Counter.most_common(4)` |
| 空结果合并 ×n | `plugins/__init__.py:280-299` 插件层相邻去重 |

## 修复选位决策：消费端 vs 生产端

数据管线场景下，修复/去重有两个候选位置：

| 维度 | 消费端（注入前/读取时） | 生产端（写入 DB 时） |
|------|-----------------------|-------------------|
| 位置 | 插件层 `pre_llm_call()` | C-stage `_run_c_stage()` |
| 风险 | 低，不动核心引擎 | 中，需同步修改 DB/cache/L-stage |
| 代码量 | ~10 行 | ~50+ 行，涉及三层同步 |
| 副作用 | 注入编号（`[~/N/M]`）与 DB 索引错位 | 无，源头干净 |

**消费端修复的偏移补偿**：当在消费端合并条目时（如连续相同 CA 摘要合并），保留 `×n` 计数标记在被保留条目的末尾。例如 `search_files: 0 hits ×3` 告知 LLM 这个结果重复了 3 次——既避免重复行，又保留基数信息。

**评估公式**：消费端优先，除非：① 生产端改动在同一次迭代中已计划，或 ② 消费端导致信息丢失无法用标记补偿。

（学习来源：2026-06-14 2.1 修复，讨论消费端 vs 生产端。最终选择消费端 + ×n 计数。）

## 预算实测验证（2026-06-14 快照）

运行时查 DB 验证预算状态：

```python
# DB: turn_cache 所有行 _assemble_status=0（无降级）
# CONTEXT_LENGTH=150000, COMPRESSION_THRESHOLD=0.50
# budget = 150000 × 0.50 = 75000
# available = 75000 × 0.95 = 71250
# used = system_tokens + head_tokens + tail_tokens + 20000(default)
# estimate: ~32K → 71250 - 32000 = ~39K 正预算
# 实测 18 轮 719 行，全是 _assemble_status=0（成功）
# budget=0 只跳过检索升级（retriever.retrieve() 不执行）
# Middle L0 永远生成，不受预算影响
```

**结论**：预算从未耗尽。`_available_budget()` 在 18 轮对话中都返回正值，检索升级正常执行。CA 的预算系统只约束「多少 tokens 可用于 L0→L1 升级」，Middle 区 L0 输出不计入预算消耗——这是设计，不是 bug。

## 已知未修复项（按快照时间分层）

以下条目来自不同时间点的快照。验证前请确认使用最近的文档版本。

### 2026-06-05 快照

- **空摘要 (问题 B)**：`result_summary="无返回数据"` 的工具轮未被排除出 BM25 检索/升级候选
- **系统消息降级 (t3)**：仅 `background_review` ContextVar 路径已修复，其他系统触发消息（如技能库维护 prompt）仍可能被 LLM 误判为「无有效增量」
- **预算超支（Middle 区 L0，极端长会话）**：budget ≤ 0 时跳过检索但仍生成 Middle L0 摘要。**经 2026-06-14 实测，本会话中 budget 从未耗尽**（72,000 budget vs ~32K used，正预算 ~39K）。即使耗尽也是正常行为——Middle L0 就是永远生成的，不受预算约束

### 2026-06-14 快照：已闭环

- 2.1 连续同工具空结果合并 — 已修复（插件层消费端去重 + ×n 计数，`plugins/__init__.py:280-299`）
- 指纹去重 — 已实现（`_deduplicate_messages()` `ca/__init__.py:1016`，`_msg_fingerprint()` `ca/__init__.py:1043`）

### 2026-06-06 快照：调试修复

| 发现 | 修复 | 说明 |
|------|------|------|
| **10 个 structured handler 全部崩溃回退** | `summarize()` 入口加 `arguments` JSON→dict 适配层（8 行） | 根因：OpenAI API 标准中 `function.arguments` 是 JSON string，所有 handler 的 `args.get("key")` 在 string 上调用时抛出 `AttributeError`，`try/except` 回退到通用逻辑。修复后 6,762 条旧数据通过 `scripts/backfill_tool_summaries.py` 离线回填 |

### 2026-06-06 快照：待改进项（非 bug）

## L0 低密度基线数据（改进前）

> 以下为 2026-06-18 会话 `20260606_183116_2121f2` 的 L0 实时快照。所有工具轮摘要的信息密度问题触发了 terminal handler 改进——增加关键输出行内联。本节作为对比基线。

### 改进前 L0 全量记录

```
[~/1/1] search_files: AGENTS.md → 0 hits
[~/1/2] search_files: agents.md → 1 hits
[~/1/3] read_file: /home/i1j/.hermes/profiles/tester/plugins/ca_assembler/AGENTS.md
[~/2/4] terminal: grep -i 'CA plugin started for session' ~/.hermes/profiles/t (1 lines)
[~/2/5] search_files: .ca_assembler_state_* → 1 hits
[~/2/6] terminal: ls -lh ~/.hermes/profiles/tester/ca_cache/ 2>/dev/null | hea (1 lines)
[~/2/7] [ERROR] terminal: grep -i 'CA\|ca_assembler\|context.assembler' ~/.hermes/prof (1 lines)
[~/2/8] terminal: cat ~/.hermes/profiles/tester/.ca_assembler_state_12942.json (1 lines)
[~/2/9] terminal: ls -lt ~/.hermes/profiles/tester/ca_cache/*.db 2>/dev/null | (1 lines)
[~/2/10] terminal: grep -A5 'ca_assembler\|plugins.enabled' ~/.hermes/profiles/ (1 lines)
[~/2/11] terminal: grep '20260606_021824_192ad7' ~/.hermes/profiles/tester/logs (1 lines)
[~/2/12] terminal: python3 -c "\nimport sqlite3, os\ndb = '/home/i1j/.hermes/pr (1 lines)
[~/2/13] terminal: grep -c '20260606_183116_2121f2' ~/.hermes/profiles/tester/l (1 lines)
[~/2/14] terminal: ls -la ~/.hermes/profiles/tester/ca_cache/*.db | wc -l (1 lines)
[~/2/15] terminal: du -sh ~/.hermes/profiles/tester/ca_cache/ (1 lines)
[~/2/16] terminal: grep '20260606_183116_2121f2' ~/.hermes/profiles/tester/logs (1 lines)
```

### 问题统计

| 指标 | 值 | 说明 |
|------|-----|------|
| 总工具轮数 | 19 | |
| terminal 工具轮 | 14 | 占 74% |
| 仅 `(1 lines)` 无内容 | 13/14 | 92% 的 terminal 摘要无输出信息 |
| `[ERROR]` 误报 | 1 | `[~/2/7]` — grep 输出含 `[ERROR]` 字符串，非真正错误 |
| read_file 无内容信息 | 1 | `[~/1/3]` — 只显示路径，无文件大小/行数 |
| search_files 较好 | 2 | 格式 `pattern → N hits` 已含结果信息 |

### 改进

已在 `ca/tool_summarizer.py` 中修改：
- **terminal L0**（L225-231）：从 `terminal: cmd_short[:60] (N lines)` 改为 `t:cmd_part[:30] → key_lines[0][:50]`
- **execute_code L0**（L234-238）：加 `tool_label="exc"` 参数，不再用 `l0.replace()` 拼接
- **read_file L0**（L344-348）：从纯路径改为 `…{parent}/{fname} (N lines)`

以下两项已在 v4.5.1 中修复：

| 改进项 | 修复 | 状态 |
|--------|------|------|
| **去重方向** | `_deduplicate_messages()` 改为留最先+原位指向标记。首次出现保留不动，后续重复替换为 `(同[~/N/0])`/`(同[~/N/m])`，指向首次出现位置。首次出现位置永远不变 → 前缀稳定。 | ✅ 已修复 |
|| **Head 区移除** | 移除 `HEAD_AUTO_L1_COUNT` 和 `dialogue_head` 机制。所有对话轮平等走拣选（tail→L2 / upgrades+L1→L1 / middle→L0）。无固定 Head/Middle 边界，不会因边界翻转破坏缓存。 | ✅ 已修复 |

---

### 2026-07-01 快照：read_file 行数、措辞优化、水位接口

| 改进项 | 修复 | 状态 |
|--------|------|------|
| **read_file L0 行数取 JSON total_lines** | `json.dumps` 转义 `\n` 为 `\\n` 导致 split 永远 1。修复：`json.loads(c)["total_lines"]` | ✅ 已修复（磁盘，进程重启后生效） |
| **"无有效增量" → "本轮无新内容"** | 内部术语改为自然语言，同步 Prompt + 测试 + 文档 5 文件 | ✅ 已修复 |
| **CA_CONTEXT_LENGTH 升至 100K** | `.env` 环境变量覆盖，代码默认 50K 不变 | ✅ 已部署 |
| **debug_token_budget()** | 纯只读 Token 水位查询，`store.get_max_token_offset()` + `engine.debug_token_budget()` | ✅ 已部署 |
| **`_build_messages_from_plan` 用 `if l1:` 而非 `_is_valid_summary(l1)`** | 设计意图——退化摘要透传给 LLM 比吞掉更高效，非 bug | ✅ 设计确认 |
