---
title: 话题分割
slug: topic-segmentation
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "topic-picking-v46"]
depends_on: ["storage-model"]
updated: 2026-07-26
---

## 问题

长对话中用户输入会跨越多个话题（如从翻译切换为编程问答）。系统需要自动识别话题边界，以便：
- 每个话题独立进行 grade 判定
- 旧话题的 Fct 可以更积极地被替换
- 新话题保留更多 Elm

## 决策

### 备选方案

1. **LLM 判断话题边界** — 即时性好但不可控，LLM 的判断标准与系统不一致
2. **全量每轮重分割** — O(n²) 性能差，旧方案 `_compute_topic_groups()` 已删除
3. **水位压力分割** — 基于累积分隔短语计数，边界不稳定（已删除 v5.7）
4. **Jaccard + 强制短语 + 增量分割（选定）** — 当前实现

### 选定方案

`TopicGradeManager._segment_topics()` 负责增量话题分割（`topic_manager.py`, 495行）：

```python
class TopicGradeManager:
    def _segment_topics(self, turn: int) -> Optional[int]:
        \"\"\"检测话题边界，返回新话题起始 turn，无变化返回 None\"\"\"
        # 1. CJK 重叠检测：比较相邻 user 行的 token 集合 Jaccard 相似度
        # 2. 强制短语检测：扫描预定义的分隔短语列表
        # 3. 增量模式：只扫描新 turn，不重建旧话题
        ...
```

**实现路径**（简化）：

| 路径 | 方法 | 状态 |
|------|------|------|
| CJK 重叠 | 相邻 user 行 token 集合的 Jaccard 相似度 < 阈值 | 已实装 |
| 强制短语 | 扫描预定义分隔短语（如`话题：`、`换个问题`、`接着讲`） | 已实装 v5.5 |
| 水位压力 | 累积分隔打分，超过阈值触发分割 | 已删除 v5.7 |

**关键配置**：
- `CA_TOPIC_SEGMENT_ENABLED`：启用/禁用话题分割
- `CA_TOPIC_SIMILARITY_THRESHOLD`：Jaccard 相似度阈值

## 数据验证

```sql
-- 查看话题边界
SELECT turn, role, content, topic_id
FROM turn_stream
WHERE topic_id IS NOT NULL
ORDER BY turn;

-- 统计每个话题的 turn 数
SELECT topic_id, COUNT(DISTINCT turn) AS turns
FROM turn_stream
GROUP BY topic_id
ORDER BY topic_id;
```

## 优点

- 增量分割：只扫描新 turn，不重建旧话题，性能好
- 两路检测互补：CJK 重叠处理同话题渐进变化，强制短语处理显式话题切换

## 约束 / 已知问题

- CJK 重叠在中文单字场景下准确率有限（单个字的重叠可能误判为话题延续）
- 强制短语依赖预定义列表，无法覆盖所有话题切换语言
- 话题分割的边界可能不等于 LLM 感知的话题边界
- `TopicGradeManager` 现有 99 个单元测试覆盖（v5.10 补齐）
