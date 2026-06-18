# 调试记录 2026-06-15 — F-stage fallback 写死占位符

**提交**: `dbe9e8e`
**日期**: 2026-06-15 12:23

## 问题

F-stage 崩溃/截断路径的 fallback 写死 `core_change="本轮无新内容"`、`_assemble_status=1`，
生成的 Fct 对话题分割毫无价值。

## 根因

崩溃路径的 fallback 未考虑消费端需求。该 Fct 的唯一消费端是话题分割
（`_compute_topic_groups`），占位符字符串不提供任何区分度。

## 修复

- 改为复制 `user_elm`（用户本轮原始消息前 100 字）作为 `core_change` 和 `hdl`
- 改 `_assemble_status=0`（非降级——确实是可用的内容信号）
- 同步修复 `cache.add_turn` 的 `hdl` 参数，传有意义的值而非空串

## 文件变更

```
ca/__init__.py | 9 +++++----
```

## 验证

话题分割不再因占位符 Fct 产生错误的分裂/合并决策。
