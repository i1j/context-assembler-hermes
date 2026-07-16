---
title: CA-OV 话题提交
slug: ca-ov-topic-submit
category: architecture
version_introduced: v5.5
status: 已迁移到插件层（功能仍活跃，`_fire_ov_submit` 在 v5.10 从 `lstage.py` 迁至 `plugins/ca_assembler/__init__.py`）
decisions: ["ca-ov-topic-submit", "topic-grade-manager"]
depends_on: ["topic-segmentation", "topic-grade-switch"]
updated: 2026-06-23
source_files: ["plugins/ca_assembler/__init__.py", "topic_manager.py"]
---

## 问题

话题分割后的结构在 session 结束后丢失。如果要跨 session 持久化话题边界和级别信息，需要一个外部存储。OpenViking 作为 Hermes 的长期记忆平台，适合存储话题数据。

## 决策

### 备选方案

1. **同步提交** — 阻塞用户路径，不可接受
2. **不持久化 OV** — session 重启丢失话题信息
3. **每次话题更新都提交** — 频率过高，OV 写压力大
4. **fire-and-forget 异步提交 + 话题切换时触发（选定）**

### 选定方案

```python
def _topic_submit_worker(self, topic_data: dict):
    \"\"\"fire-and-forget 线程提交话题到 OV\"\"\"
    try:
        openviking.add_resource(
            path=f"viking://resources/hermes/ca/topics/{session_id}/{topic_id}",
            description=topic_data["title"],
        )
    except Exception as e:
        logger.warning(f"[CA] topic submit failed: {e}")
```

**提交时机**：话题切换时（`TopicGradeManager` 检测到新话题时触发）

**提交内容**：
- 话题标题（从 user 输入提取）
- 形心向量（topic centroid）
- turn 范围（起始 turn ~ 结束 turn）
- 话题级摘要（可选）

**线程管理**：
- 独立的 daemon 线程
- fire-and-forget 不等待结果
- 失败日志记录，不重试
- 启动时 `_retry_failed_ov_submits()` 自动重试 session 中断残留的未提交话题

## 数据验证

```bash
# 在 OV 中搜索已提交的话题
viking_search("topic hermes ca topic")
# 或查看特定 session 的话题
viking_list("viking://resources/hermes/ca/topics/<session_id>/")
```

## 优点

- 非阻塞：用户路径不受影响
- 持久化：session 重启后话题信息可恢复
- 失败安全：提交失败不影响主流程

## 测试覆盖

- 话题提交占位测试 — `tests/unit/test_topic_manager.py`（`pytest.skip` 标记，因 OV 依赖）
- 话题提交功能在 `plugins/ca_assembler/__init__.py` 中

## 约束 / 已知问题

- OV 不可用时话题信息丢失（不降级，主流程继续）
- 提交频率控制依赖话题切换频率，不会过于频繁
- 当前无重试机制，临时网络抖动可能导致丢失


### 变更记录

- **v5.10** (2026-06-23)：`_fire_ov_submit` 和相关代码已从 `lstage.py` 删除
- 对应的占位测试已在 `test_topic_manager.py` 中添加（`pytest.skip` 标记）
