---
title: 话题切换定级
slug: topic-grade-switch
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "tail-protection"]
depends_on: ["topic-segmentation", "storage-model"]
updated: 2026-06-22
---

## 问题

话题切换时，历史轮应使用什么 grade 替换？距离新话题越远的轮次，其 Elm 应越积极地被替换为 Fct；距离越近的轮次，应保留更多 Elm。需要一个量化公式。

## 决策

### 备选方案

1. **固定半径** — 一个阈值决定所有话题切换行为，不灵活
2. **按时间距离** — turn 序号差作为距离，不反映语义
3. **按级别硬编码** — 每级对应固定 grade，不做分级
4. **形心半径公式定级（选定）**

### 选定方案

`get_turn_grade(turn_index) → TopicGrade` 方法（`topic_manager.py:318`）：

```python
def get_turn_grade(self, turn_num: int) -> TopicGrade:
    \"\"\"按形心半径定级 ACT/REL/FAR\"\"\"
    topic_id = self._turn_to_topic.get(turn_num)
    if topic_id is None:
        return TopicGrade.ACT  # 保守：保留完整摘要
    return self._topic_grades.get(topic_id, TopicGrade.ACT)
```

等级由 `grade_on_switch()` 在话题切换时按形心半径公式计算并缓存：
- **内球**（q→形心 ≤ topic_radius）→ `TopicGrade.ACT`（密切关联）
- **外球**（q→形心 ≤ 2×topic_radius）→ `TopicGrade.REL`（关联）
- **远距离**（q→形心 > 2×topic_radius）→ `TopicGrade.FAR`（无关联）

`TopicGrade` 与 `Grade` 的映射在 A-stage 完成：

| TopicGrade | user/fin 行 | thought/tool 行（降一级） |
|---|---|---|
| ACT | Grade.ELM（原文保留） | Grade.FCT（完整摘要） |
| REL | Grade.FCT（完整摘要） | Grade.HDL（截断 150ch） |
| FAR | Grade.HDL（截断 150ch） | None（清空为"略"） |

**尾巴保护覆盖**：`protect_tail` 内的 turn 无论 topic_grade 如何，强制不替换（原文保留）。

## 数据验证

```python
# 查看各 turn 的 TopicGrade 分布
from topic_manager import TopicGradeManager

tgm = TopicGradeManager(...)
for turn in range(1, 50):
    grade = tgm.get_turn_grade(turn)
    print(f"Turn {turn}: grade={grade.name}")
```

## 优点

- 连续空间量化：半径公式输出连续值，边界平滑
- 三段阈值简单可调：25/100 可通过配置调整
- 尾巴保护覆盖确保尾部安全

## 约束 / 已知问题

- ACT/REL/FAR 三级阈值可通过 topic_manager 配置，不同对话模式可能需要不同配置
- 形心计算基于 topic Fct 向量嵌入，不反映纯位置距离
- tail 保护区全覆盖时，尾部 turn 即使属于 FAR 话题也不被替换
