# 调试记录：会话 mqgmwc2 上下文爆炸根因 — tail 硬编码、两套死代码、skill_view 空壳 Fct

**日期：** 2026-07-18
**Session：** `mqgmwc2i3kgj5m`（话题切割机制已彻底失效）
**追加 Session：** `mqgoe1053rhwh9`（当前会话，验证修正）
**改动范围：** 本次仅分析不修改

---

## 0. 现象

会话 9 轮（user 消息），输入 token 达到 370K，输出 token 87K。**几轮就突破 200K**。

| API call | 阶段 | input tokens | 说明 |
|:--------:|:----:|:------------:|------|
| #1 | turn 2 | 108,885 | 首次 api，已含 skill_view raw |
| #13 | turn 3 | 54,947 | 新 segment 重开 |
| #69 | turn 5 | 93,732 | A-stage 替换已启动 |
| #70 | turn 5 | 71,627 | 替换生效后的最低点 |
| #155 | turn 9 | 175,198 | 新工具调用累积 |
| bg_review | — | 324,295 → 369,582 | 最终全量上下文 |

---

## 1. 三源验证

### 1.1 state.db messages — 原文分布

| role | 数量 | 总 chars | 平均 chars |
|------|:----:|:--------:|:---------:|
| user | 8 | 290 | 36 |
| assistant | 134 | 8,661 | 65 |
| **tool** | **140** | **481,790** | **3,441** |

**top 5 tool 行（全部 skill_view）：**

| state.db id | tool | chars | 占比 |
|:-----------:|------|:-----:|:----:|
| 35824 | skill_view ca-development | 94,299 | 19.6% |
| 35548 | skill_view ca-development | 87,829 | 18.2% |
| 35550 | skill_view ca-triple-source-verify | 31,859 | 6.6% |
| — | skill_view ca-development (turn 3) | 89,416 | 18.6% |
| — | skill_view ca-triple (turn 6) | 31,859 | 6.6% |

skill_view 共 **6 次调用**，累计 ~335K chars ≈ 45% 的 tool 总内容。

### 1.2 CA turn_stream — Fct 覆盖率

**工具行 Fct 覆盖率 100%（186/186），0 占位符。** 但 skill_view 的 Fct 是空壳：

```json
{
  "tool_name": "skill_view",
  "tool_args": {"name": "ca-development"},
  "result_summary": "skill_view: ca-development — 1 lines",
  "implicit_knowledge": []
}
```

```
skill_view seq=2: content=87,829 chars → Fct=214 chars（-99.8%，但信息量=0）
skill_view seq=3: content=31,859 chars → Fct=232 chars（-99.3%，信息量=0）
```

对比其他工具（read_file 至少保留路径和行范围，search_files 保留文件名和匹配数），skill_view 的 `_summarize_skill_view`（`tool_summarizer.py:495`）只提取了名字和行数，**原始 skill 内容完全丢失**。

### 1.3 agent.log — A-stage 替换记录

```
20:45:28 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)    ← turn 1
20:52:03 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)    ← turn 3
21:01:26 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)    ← turn 5
21:04:43 [CA_v5] simple_mutation: replaced=34 + skipped=12 (tail=55, turns=1) ← 首次替换
21:08:12 [CA_v5] simple_mutation: replaced=142 + skipped=12 (tail=184, turns=2)
21:13:45 [CA_v5] simple_mutation: replaced=142 + skipped=12 (tail=186, turns=3)
21:18:55 [CA_v5] simple_mutation: replaced=142 + skipped=12 (tail=188, turns=4)
21:24:26 [CA_v5] simple_mutation: replaced=224 + skipped=12 (tail=284, turns=5)
```

前 3 次 A-stage 调用全部 `replaced=0`。第 4 次（21:04:43）才首次替换。

---

## 2. 根因分析

### 根因 1（P0）：两套 A-stage 代码，只有 `__init__.py` 生效

`/ca/` 目录下有两套独立实现，**互不连通**：

```
实际运行：
__init__.py:292-398  _simple_mutation_mode_v5()
  └─ 自包含的 turn 划分 + 角色队列匹配
  └─ 尾区保护：硬编码 tail=3（line 312 注释 "最后 3 个 user turn 不处理"）
  └─ 直接替换 conversation_history[i]["content"]

死代码（从未被调用）：
ca/a_planner.py      compute_plan()
ca/a_injector.py     inject()
  └─ 7月18日曾修改 TAIL_PROTECT_TURNS = 2 → 1
  └─ 该修改无任何效果
```

### 根因 2（P1）：硬编码 `tail=3` 导致前 3 个 user 全部保护

`__init__.py:312-320`：

```python
# 尾巴保护：最后 3 个 user turn 不处理
tail_boundary = 0
_user_count = 0
for i in range(len(conversation_history) - 1, -1, -1):
    if conversation_history[i].get("role") == "user":
        _user_count += 1
        if _user_count >= 3:
            tail_boundary = i
            break
```

保护效果：

| conv_hist 中 user 数 | 保护区 | 可替换轮次 | 对应 simple_mutation |
|:---:|:---:|:---:|:---:|
| 1 | U1 | 无 | replaced=0 |
| 2 | U1-U2 | 无 | replaced=0 |
| 3 | U1-U3 | 无 | replaced=0 |
| **4** | U2-U4 | **U1** | **replaced=34** |

前 3 次对话，turn 1-3 的 skill_view tool 行（87K+31K+89K chars）全部以原文送入 LLM。

### 根因 3（P2）：`_summarize_skill_view` 输出空壳 Fct

`tool_summarizer.py:495-529`：

```python
def _summarize_skill_view(self, tool_call_msg, tool_responses):
    """skill_view 结构化摘要：技能名 + 文件大小/结构概览"""
    name = args.get("name", "?")
    # output 取的是 tool_responses — F-stage 传递的参数，可能没传 content
    output = ""
    for resp in tool_responses:
        c = resp.get("content", "")
        if c:
            output = str(c)
            break
    
    total_lines = len(output.split("\n")) if output else "?"
    result_summary = f"skill_view: {name}{fp_short} — {total_lines} lines"
    # ↕ 只记录名字和行数，skill 的元数据/行为规则/配置段完全丢弃
```

**原因推测：** `tool_responses` 在 F-stage 回调中没有正确传递 skill_view 的 tool 输出 content，导致 `output = ""` → `split("\n")` → `[""]` → `1 lines`。

对比：

| tool | Fct 内容价值 |
|------|------------|
| read_file | `"path: xxx, lines 1-200, content length 1234"` — 记录了路径和行范围 |
| search_files | `"pattern: xxx, matches: [...]"` — 保留了匹配项 |
| skill_view | `"skill_view: ca-development — 1 lines"` — **零信息量** |

### 根因 4（P3）：替换时机过晚，新内容持续累积

即使 turn 4 开始替换：
- 前 3 轮已污染上下文
- 175 次 tool 调用持续产生新内容
- 保护区 (last 3 turns) 内的 raw 数据累积到 55→284 条消息
- 最终 bg_review 调用时上下文已达 324K tokens

---

## 3. 待清理项

| # | 问题 | 文件 | 操作 |
|:--:|------|------|:----:|
| 1 | `tail=3` 硬编码 | `__init__.py:312-320` | 提取为顶层常量，遵从 `a_planner.py` 的 `TAIL_PROTECT_TURNS` |
| 2 | 两套 A-stage 代码 | `__init__.py` vs `ca/a_planner.py`+`ca/a_injector.py` | 删死代码或重构为单一路径 |
| 3 | `_summarize_skill_view` 空壳 Fct | `tool_summarizer.py:495-529` | 修复 tool_responses 传参，或直接从 turn_stream 读取 content 做摘要 |
| 4 | `a_planner.py` `TAIL=1` 的误改 | `a_planner.py:26` | 清理死代码时自然解决 |

---

## 4. 验证数据（当前会话 mqgoe1053rhwh9）

当前会话同样触发了空壳 skill_view Fct：

```
skill_view seq=2: content=87,829 → Fct=214 → "skill_view: ca-development — 1 lines"
```

A-stage 日志：

```
21:27:05 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)
21:35:30 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)
21:40:33 [CA_v5] simple_mutation: replaced=0 + skipped=0 (tail=0, turns=0)
21:41:57 [CA_v5] simple_mutation: replaced=48 + skipped=2 (tail=54, turns=1)
21:43:30 [CA_v5] simple_mutation: replaced=54 + skipped=2 (tail=63, turns=2)
21:45:01 [CA_v5] simple_mutation: replaced=56 + skipped=2 (tail=68, turns=3)
21:46:13 [CA_v5] simple_mutation: replaced=62 + skipped=2 (tail=77, turns=4)
```

同样的 tail=3 延迟，同样的 `replaced=0` 前 3 次。问题稳定复现。
