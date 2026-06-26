---
title: 话题分割
slug: topic-segmentation
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["topic-grade-manager", "topic-picking-v46"]
depends_on: ["storage-model"]
updated: 2026-06-23
source_files: ["topic_manager.py"]
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

`TopicGradeManager._assign_topic()` 负责增量话题分配（`topic_manager.py`）：

```python
class TopicGradeManager:
    def _assign_topic(self, turn, ca_rows, user_msg, total_tokens=0):
        """增量话题分配 —— Jaccard + 水位压力与当前话题累积 Fct 文本匹配。
        
        策略：
          1. 强制短语 → 新话题
          2. 第一个 turn → 话题 1
          3. 当前轮 Fct 与 current_topic 累积文本做 Jaccard + 水位压力扣减
             - ≥ CHAIN (0.04) → 同话题
             - ≥ ENTRY (0.02) → 弱匹配，同话题
             - 否则 → 新话题
        """
```

**TopicGradeManager.detect()** 是外部调用入口，每次 `pre_llm_call` 调用一次：
- 只在 `turn > _last_processed_turn` 时执行增量逻辑
- 返回 `bool`：是否发生话题切换

**实现路径**（简化）：

| 路径 | 方法 | 状态 |
|------|------|------|
| CJK 重叠 | 相邻 user 行 token 集合的 Jaccard 相似度 < 阈值 | 已实装 |
| 强制短语 | 扫描预定义分隔短语（如`换话题`、`聊点别的`、`另一个`） | 已实装 v5.5 |
| 水位压力 | conv_hist 总 Token 驱动 Jaccard 柔性扣减（`_apply_water_pressure`） | 已实装 |

**关键配置**：
- `CA_TOPIC_JACCARD_ENTRY`（默认 0.02）：弱匹配 Jaccard 阈值（新话题接入）
- `CA_TOPIC_JACCARD_CHAIN`（默认 0.04）：强匹配 Jaccard 阈值（链内延续）
- `CA_TOPIC_PEAK_TOKEN`（默认 20000）：水位满压 Token 阈值

## 数据验证

```sql
-- 查看每轮的话题分配（通过日志分析）
SELECT turn, role, content
FROM turn_stream
WHERE role='user'
ORDER BY turn;
```

话题分割结果存储在 `TopicGradeManager._turn_to_topic`（内存 Dict，不落盘）。

## 优点

- 增量分割：只扫描新 turn，不重建旧话题，性能好
- 两路检测互补：CJK 重叠处理同话题渐进变化，强制短语处理显式话题切换

## 约束 / 已知问题

- CJK 重叠在中文单字场景下准确率有限（单个字的重叠可能误判为话题延续）
- 强制短语依赖预定义列表，无法覆盖所有话题切换语言
- 话题分割的边界可能不等于 LLM 感知的话题边界
- `TopicGradeManager` 现有 99 个单元测试覆盖（v5.10 补齐）
