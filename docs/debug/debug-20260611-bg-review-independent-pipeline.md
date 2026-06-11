# bg_review 独立管道实装

**日期**: 2026-06-11
**类型**: 实装报告
**前置**: debug-20260611-bypass-design-evolution.md（bypass 设计演进）, debug-20260611-biz-category-implementation.md（biz_category 实装）
**会话**: 20260611（设计确认 + 实装 session）
**commit**: 未提交（ca/__init__.py ±8 行）

---

## 背景

bg_review 轮（后台审查）原本在 CA 的 A-stage 中进入完整的话题分级/检索/预算分配管线，最终被标记 topic_bg → L0 → bypass（不动）。这浪费了计算资源且污染了 topic 索引。

设计锁定（session `20260610_122644_bbb0b5` msg 22172）要求：
- bg_review 轮不走话题分级/预算/plan
- 以原文透传（不经过 CA 压缩）
- 不进入历史对话表（turn_plan 不含 bg_review）

## 方案

**核心思路**：在 `_compute_assemble_plan` 中，读取 `biz_category` 后立即从 `l1_texts/l0_texts/tool_group_*_texts` 索引中移除 bg_review 轮。此后所有下游逻辑（topic grading, retrieval, plan building, bypass）自然看不见它们。

**内容保护**：依赖已有的 A-stage gate（`__init__.py:122`）——bg_review 轮在 conversation_history 中的 content 始终是原始消息（未被 CA 突变）。plan 中无此 turn → `_build_aligned_outcomes` 给 outcome=None → 保留原文。

## 改动

### 文件：`ca/__init__.py`

**改动 1 — 过滤 bg_review 摘要索引**

```python
# ── 从摘要索引中移除 bg_review 轮 ──
if _biz_cats:
    _bg_ts = set(_biz_cats.keys())
    l1_texts = {t: v for t, v in l1_texts.items() if t not in _bg_ts}
    l0_texts = {t: v for t, v in l0_texts.items() if t not in _bg_ts}
    tool_group_l1_texts = {k: v for k, v in tool_group_l1_texts.items() if k[0] not in _bg_ts}
    tool_group_l0_texts = {k: v for k, v in tool_group_l0_texts.items() if k[0] not in _bg_ts}
```

位置：`_biz_cats = self.store.read_turn_biz_categories(...)` 之后，tail protection 计算之前。

**改动 2 — 简化 bypass_turns**

旧代码将全部 bg_review 轮加入 `_bypass_turns`。改动后 bg_review 不在 plan 中，无需特判：

```python
_dialogue_entries = [e for e in plan if e.turn_type == "dialogue"]
_bypass_turns = {e.turn_index for e in _dialogue_entries[-3:]}
```

## 数据流（改动后）

```
bg_review 轮在 CA 中：
  C-stage: 写 turn_cache + biz_category="bg_review" + l2_text（原文）
            → 正常积累数据，不变

A-stage _compute_assemble_plan:
  ├─ get_snapshot_data() → l1_texts/l0_texts（含 bg_review）
  ├─ 读取 _biz_cats
  ├─ 从 l1_texts/l0_texts/tool_group_*_texts 移除 bg_review
  ├─ topic grading / retrieval → 看不见 bg_review
  ├─ budget / plan → 不含 bg_review
  ├─ turn_plan → 不含 bg_review
  └─ bypass_turns → 仅最后 3 个真实对话轮

_build_aligned_outcomes:
  ├─ bg_review 无 plan entry → entry=None
  └─ outcome=None → content 保持原文

LLM 看到：bg_review 的完整原始消息
```

## 验证

| 测试套件 | passed | failed | skipped | 回归 |
|----------|--------|--------|---------|------|
| 活跃测试 | 315 | **4**（既存 test_a.py） | 20 | **0** |
| bg_review C-stage 专用测试 | ✅ 通过 | — | — | — |

## 与尾区保护的吻合

| 机制 | 对象 | 方式 |
|------|------|------|
| 尾区 bypass | 最后 3 个真实对话轮 | plan entry → L2 → 原文透传 |
| bg_review 独立管道 | biz_category 标记的轮 | 不入 plan → 原文透传 |
| 关系 | 完全独立管道，不共享数据流 |

## 后续

- [ ] 验证真实对话流一例（有 bg_review 轮的 session）
- [ ] 确认无误后合并到设计文档（design/impl/）
- [ ] 更新决策树（P-003 子节点）
- [ ] 更新 changelog
