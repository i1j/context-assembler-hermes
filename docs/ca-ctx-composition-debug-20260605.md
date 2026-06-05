# CA 上下文组成调试报告

**日期**: 2026-06-05
**Session ID**: 20260605_190032_27f68c
**CA Schema**: v3 (v4.4.0)
**分析方式**: sqlite3 直读 ca_cache DB + 渲染格式交叉验证

---

## 1. 概述

当前 session 共 11 轮对话，CA 组装后上下文总估计 ~62K tokens，远超 CA 预算 30,400（`CA_CONTEXT_LENGTH 32000 × 0.95`），超出约 31.5K（206%）。

## 2. 发现的问题

### 2.1 问题 A：跨对话轮工具序列重复（严重）

前 17 个工具轮（T1-17）在每轮对话中都作为独立记录存在，且 BM25 检索在每轮都选出相同的工具轮。

**具体模式**：
```
Dialogue[1] → 工具 T1-T11: search_files×6 → read_file → patch → terminal×3
Dialogue[2] → 工具 T1-T11: search_files×6 → read_file → patch → terminal×3  ← 完全相同
Dialogue[3-5] → 工具 T1-T15: 同上 + 额外 4 个终端查询
...
```

**根因**：CA 的 BM25 预选检索以当前对话轮 L1 为 query，在**全部历史工具轮索引**中检索。相似对话（"改 compression.target_ratio"）产生相似的 L1 摘要，BM25 命中相同工具轮。A‑stage 拣选时择优升级，重复升级同一批工具轮。

**影响**：285 个工具轮摘要中，大量内容是重复的。冗余覆盖的 token 达到数万。

### 2.2 问题 B：109/285 工具轮摘要为"无返回数据"（严重）

| 工具名 | 无返回次数 |
|--------|-----------|
| search_files | 频繁 |
| read_file | 频繁 |
| terminal（部分） | 偶发 |
| skill_view | 偶发 |

**根因**：CA 对每个工具调用独立记录 L1 摘要。返回空结果的调用（文件不存在、路径错误、搜索无匹配）被规则引擎准确标记为 `result_summary: "无返回数据"`。这些摘要信息量为零，但占用了大量预算。

**影响**：38% 的工具轮摘要的检索价值为零，却参与了 BM25 排序，可能污染升级候选质量。

### 2.3 问题 C：CA 预算严重超支（严重）

| 指标 | 值 |
|------|-----|
| CA_CONTEXT_LENGTH | 32000 |
| 预算上限（×0.95） | 30400 |
| 实际估计 token | ~62000 |
| 超出比例 | +206% |

**当前效果**：虽然 CA 论文中"预算耗尽快速短路"逻辑应跳过检索，但实际上下文保留全部 11 轮 + 285 工具摘要。可能的解释：
- Tail 保护区 `CA_PROTECT_TAIL_TOKENS` 设为较大值（当前配置未显式设置，可能用默认 20000）导致 Head+Tail 直接占满预算
- 或 budget 计算错误（回归了 bug 报告 t_b022f3a9 的问题 #2？）

### 2.4 问题 D：去重未跨对话轮边界折叠

全指纹去重 (`_deduplicate_messages`) 在同一轮组装的消息列表中去重。由于每轮对话的工具轮摘要被 Assembler 插入到不同位置（`[~/1/1]` vs `[~/2/1]`），它们不构成"相同消息"，因此去重不生效。

### 2.5 问题 E：patch 失败摘要全部保留

所有 12 次 `patch: 失败` 均保留在上下文中（每轮 T8），patch 因 `config.yaml` 为保护文件被拒绝编辑。这些工具轮每次都一样，但跨轮保存。

---

## 3. 诊断数据

### 3.1 对话轮 L1 摘要大小分布

| Turn | L1 Bytes | 工具数 | 工具总 Bytes | L1 预览 |
|------|----------|--------|-------------|---------|
| 1 | 256 | 11 | 4017 | 压缩比例升至30% |
| 2 | 225 | 11 | 4017 | 无 |
| 3 | 242 | 15 | 5387 | 目标比例升至30% |
| 4 | 199 | 17 | 6258 | 目标比例分profile调整 |
| 5 | 121 | 21 | 9085 | 系统后台审查 |
| 6 | 322 | 22 | 7835 | CA插件三阶段上下文引擎 |
| 7 | 290 | 23 | 8214 | 新增agents.md测试执行指南 |
| 8 | 121 | 31 | 11439 | 系统后台审查 |
| 9 | 296 | 28 | 9509 | 发现CA Plugin实际未生效 |
| 10 | 266 | 50 | 24169 | CA插件状态正常运行 |
| 11 | 121 | 56 | 27357 | 系统后台审查 |

### 3.2 工具名分布 (Top 8)

| 工具 | 次数 | 占比 |
|------|------|------|
| terminal | 121 | 42.5% |
| search_files | 79 | 27.7% |
| read_file | 49 | 17.2% |
| patch | 12 | 4.2% |
| skill_view | 9 | 3.2% |
| execute_code | 6 | 2.1% |
| skill_manage | 7 | 2.5% |

### 3.3 电路断路器状态：健康（failures=0）

---

## 6. 最新数据快照（2026-06-05 19:09+）

14 轮对话，463 工具轮，超预算 +281%

```
[~/12] 上下文层级细化至Mid-Head
  [~/12/1] search_files: {"total_count": 8, ...}
  [~/12/2] search_files: [EMPTY] 无返回数据
  [~/12/7] read_file: {"content": "1|model:..."}
  [~/12/8] patch: [FAILED] 失败
  [~/12/16] terminal: ✓ Set compression.target_ratio = 0.25...
  [~/12/23] read_file: {"content": "1|# ContextAssembler AI 测试代理指南..."}
  [~/12/48] execute_code: Tables: [('turn_cache',),...]
  [~/12/49] execute_code: === Turn type distribution ===   dialogue: 9   tool: 179...
[~/13] 新增调试报告与ctx查看技能
  [~/13/8] patch: [FAILED] 失败 (重复)
  [~/13/56] write_file: [EMPTY] 无返回数据
  [~/13/58] skill_manage: Skill 'ca-ctx-inspect' created.
[~/14] 系统后台审查 [short]
  [~/14/8] patch: [FAILED] 失败 (重复)
---
summary:
  dialogue turns: 14
  tool turns:     463
  estimated tok:  ~116K
  over budget:    +85K (281%)
  [EMPTY]  50/178 (28%)  (最近3轮)
  [FAILED] 8/178
```

## 7. 配套工具

`ca-ctx-inspect` skill 已创建，位于：
```
~/.hermes/profiles/tester/skills/testing/ca-ctx-inspect/
├── SKILL.md
└── scripts/ca_ctx_inspect.py
```
支持参数：`-n N`（最近 N 轮）、`--summary-only`、`-p PROFILE`、`-d DB`。

---

## 4. 建议（改进优先级）

以下问题在本次诊断中确认，按优先级排列：

| 优先级 | 问题 | 状态 | 说明 |
|--------|------|------|------|
| P0 | 预算超支（问题 C） | 待修复 | 实际 ~116K vs 预算 30,400（+281%） |
| P0 | 空摘要排除检索（问题 B） | 待修复 | 38% 工具摘要 `result_summary="无返回数据"`，零信息量占预算 |
| ~~P1~~ | ~~跨轮工具摘要去重（问题 A + D + E）~~ | **已修复** | 指纹计算增加 `[~/N]` 标签剥离，跨轮相同内容仅保留最后一次出现 |
| P2 | 基于 turn_plan 的拣选组装 | 规划中 | 不再重建全量消息列表，性能提升 |

### 已修复：跨轮去重标签剥离

**根因**：`_deduplicate_messages` 对整条消息（含 `[~/N]` / `[~/N/M]` 标签）计算 SHA256 指纹。不同轮次的相同摘要因标签不同而不被视为重复。

**修复**（2026-06-05）：新增 `_CA_TAG_RE` 正则剥离标签前缀，提取 `_msg_fingerprint` 方法，改为两遍扫描——第一遍建指纹→最后出现索引，第二遍仅保留最后出现的那条。更靠近 tail 保护区且 LLM 更关心。

**覆盖**：
- 同一 `search_files: 无返回数据` 跨 5 轮 → 保留 1 条（最后轮）
- 同一 `patch: 失败` 跨 12 轮 → 保留 1 条（最后轮）
- 同一 `系统后台审查` L1 跨 3 轮 → 保留 1 条（最后轮）
- 不同 `core_change` 的对话轮 → 各自保留

### 待修复详细分析

**预算超支（问题 C）**：
- v4.4.1 已将 `PROTECT_TAIL_TOKENS` 从 20000 降至 10000，但 Tail + Head + 系统消息仍可能占满预算。
- `_compute_tail_start` 仅跳过 `role=tool` 消息，不跳过 CA 注入的 `[~/N]` 摘要消息——这些摘要本身即消耗 tail 预算。
- 预算计算 `_available_budget` 正确返回 ≤0 时跳过检索，但 `_build_final_messages_v4` 仍保留了 Middle 区的 L0 摘要——这些 L0 摘要虽小但累积量大（463 工具轮 × ~30 chars = ~14K chars ≈ 7K tokens）。
- 建议：(1) budget ≤0 时跳过 Middle 区 L0 生成；(2) `_compute_tail_start` 也跳过 CA 注入摘要；(3) 确认 Hermes 实际传入的 `context_length` 是否使用了新默认值。

**空摘要排除检索（问题 B）**：
- 109/285 个工具轮 `result_summary="无返回数据"`，信息量为零。
- 建议：在 `_upgrade_selection` 阶段降低优先级或直接排除出检索候选集。

## 5. 关联文档

- Bug 报告: `tests/docs/test-report.md` (#t_b022f3a9)
- QA 状态: `tests/docs/qa-bug-report.md`
- 技术方案: `docs/technical-plan.md`
- 前次复盘: `docs/archive/ca-v4.3.2-incident-report.md`
