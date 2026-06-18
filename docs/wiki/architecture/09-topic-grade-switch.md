---
title: 话题切换定级
slug: topic-grade-switch
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "tail-protection"]
depends_on: ["topic-segmentation", "storage-model"]
updated: 2026-06-18
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

`get_turn_grade(turn_index) → Grade` 方法（`topic_manager.py`）：

```python
class Grade(enum.IntEnum):
    ELM = 2   # L2：保留 Elm 原文
    FCT = 1   # L1：替换为 Fct 全文
    HDL = 0   # L0：截断 150ch

def get_turn_grade(self, turn_index: int) -> Grade:
    \"\"\"按形心半径定级\"\"\"
    radius = self._compute_radius(turn_index)
    if radius <= 25:
        return Grade.ELM   # 近区：保留原文
    elif radius <= 100:
        return Grade.FCT   # 中区：摘要替换
    else:
        return Grade.HDL   # 远区：截断
```

**形心半径公式**：计算指定 turn 到当前话题形心的距离。
- 形心 = 话题内所有 user turn 的平均向量位置（基于 turn 序号加权）
- radius = abs(turn - centroid) / topic_spread

**尾巴保护覆盖**：`protect_tail` 内的 turn 无论 radius 如何，强制返回 `Grade.ELM`。

## 数据验证

```python
# 查看各 turn 的 grade 分布
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

- 25/100 阈值为经验值，不同对话模式可能需要不同配置
- 形心计算基于 turn 序号，不反映实际语义距离
- radius 极端值（>500）时所有历史都走 HDL，丢失细节
