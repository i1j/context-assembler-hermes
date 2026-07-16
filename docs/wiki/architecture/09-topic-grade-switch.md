---
title: 话题切换定级
slug: topic-grade-switch
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "tail-protection"]
depends_on: ["topic-segmentation", "storage-model"]
updated: 2026-06-23
source_files: ["topic_manager.py", "ca/grade.py"]
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

`get_turn_grade(turn_num) → TopicGrade` 方法（`topic_manager.py` `TopicGradeManager`）：

```python
def get_turn_grade(self, turn_num: int) -> TopicGrade:
    \"\"\"按形心半径定级 ACT/REL/FAR\"\"\"
    topic_id = self._turn_to_topic.get(turn_num)
    if topic_id is None:
        return TopicGrade.ACT  # 保守：保留完整摘要
    return self._topic_grades.get(topic_id, TopicGrade.ACT)
```

等级由 `grade_on_switch()` 在话题切换时按形心半径公式计算并缓存：
- **内球**（d ≤ r/2）→ `TopicGrade.ACT`（密切关联）
- **外球**（d ≤ r）→ `TopicGrade.REL`（关联）
- **远距离**（d > r，且不在检索升级集）→ `TopicGrade.FAR`（无关联）
- **检索升级**：FAR 话题若在 `retrieved_topics` 中 → 提升为 REL

`TopicGrade` 与 `Grade` 的映射在 A-stage 完成：

| TopicGrade | user 行 | fin 行 | thought/tool 行（降一级） |
|---|---|---|---|
| **ACT** | Elm（原文保留） | Elm（原文保留） | Fct（完整摘要） |
| **REL** | Fct（完整摘要） | Fct（完整摘要） | Hdl（截断 150ch） |
| **FAR** | Hdl（截断 150ch） | Hdl（截断 150ch） | 略（语义省略标记） |

**降一级规则**：thought/tool 输出行摘要等级 = 话题等级 - 1（ACT→Fct, REL→Hdl, FAR→略）。user/fin 不降级。

**等级枚举转换**：`TopicGrade` → `Grade` 映射由 `ca/grade.py` 的 `Grade.from_topic_grade()` 完成，供 A-stage 组装时选择对应列（`Fct` 或 `Hdl`）。

**尾巴保护覆盖**：倒数第 2 个 user 消息之后的所有行强制原文保留，不受 topic_grade 影响。

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
- 三级阈值基于形心距离，语义相关
- 尾巴保护覆盖确保尾部安全

## 测试覆盖

- 话题定级测试 — `tests/unit/test_topic_manager.py`（`TestGradeOnSwitch`、`TestGradeTopicsByRadius`）

## 约束 / 已知问题

- ACT/REL/FAR 三级阈值可调，不同对话模式可能需要不同配置
- 形心计算基于 topic Fct 向量嵌入，不反映纯位置距离
- tail 保护区全覆盖时，尾部 turn 即使属于 FAR 话题也不被替换
