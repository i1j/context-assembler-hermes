---
title: 增量缓存（⚠️ v6 方向 B 已废弃）
slug: incremental-cache
category: architecture
version_introduced: v5.8
version_deprecated: v6.0
status: 废弃
decisions: []
depends_on: ["storage-model", "a-stage-role-match"]
updated: 2026-06-28
---

## 废弃原因

v6 方向 B 完全摒弃了 mutation 模式，改用 `_build_conv_history_v6` 从 turn_stream DB
直接重建 conv_history。因此增量缓存不再需要。

### 替代方案

`_build_conv_history_v6` 每次从 turn_stream DB 读取完整的 turn→grade 映射和 Fct/Hdl 数据，
通过 SQLite 查询构造 conv_history。由于 CA 的 turn 数一般 < 1000，全量重建性能可接受。

### 迁移注意

- `_A_stable_cache`、`_A_cache_turns`、`_A_cache_is_stale` 实例变量保留但不会被写入
- Fct pending 防护不再需要 — `_build_conv_history_v6` 遇到 None Fct/Hdl 时自动回退到 Elm
- 旧测试中依赖增量缓存的用例需要更新

---

## 历史文档（v5.8 原始内容，仅保留供引用）

### 实例变量

| 变量 | 类型 | 语义 |
|------|------|------|
| `_A_stable_cache` | `list[dict] \| None` | 稳定区已替换 Fct 的 conv_hist 片段。None=冷启动或话题切换后 |
| `_A_cache_turns` | int | cache 中的 user 消息数（用于增量 Step 3 的 turn 计数起点） |
| `_A_cache_is_stale` | bool | Fct pending 标记。True→下轮不进增量，走全量修复 |

### 调度逻辑

```
pre_llm_call_v5()
  ├─ 话题检测 detect()
  │    ├─ 切换 → _A_stable_cache = None → 全量
  │    └─ 无切换 → 继续
  ├─ cache 状态判断
  │    ├─ cache is None → 全量
  │    ├─ _A_cache_is_stale → cache=None → 全量
  │    └─ cache 有效 → 增量
  └─ 全量路径: _full_mutation() (alias of _simple_mutation_mode_v5)
       ├─ 逐 turn 查 turn_stream + 角色队列匹配 + grade 驱动替换
       ├─ 尾部保留 Elm (倒数 2 个 user)
       └─ 写缓存: _A_stable_cache = conv_hist[0:tail_boundary]
  └─ 增量路径: _incremental_mutation()
       ├─ Step 1: 尾部边界（与全量一致）
       ├─ Step 2: 并行扫描 cache → 覆盖稳定区 conv_hist 的 content
       ├─ Step 3: Delta 区收集 — cache 后到 tail_boundary 之间的 thought/tool/fin
       ├─ Step 4: Delta 替换 — 从 turn_stream 读 Fct，1:1 角色队列匹配
       │          ⚠️ Fct pending 防护：fct=None → 不入队列 → _A_cache_is_stale=True
       └─ Step 5: 写缓存 → 更新 _A_stable_cache/_A_cache_turns
```

### Fct pending 防护（P0）

增量 delta 替换中，若 `get_turn_ca_rows` 返回的 Fct 为 None（F-stage daemon 线程尚未写完），则：
- 不将该 Fct 加入角色队列（避免 `""` 固化到缓存）
- 标记 `_A_cache_is_stale = True`
- 下次走全量路径修复

### 回退条件

| 场景 | 处理 |
|------|------|
| 冷启动 (cache is None) | `_full_mutation` |
| 话题切换 (detect==True) | cache=None → 全量 |
| Fct pending (delta Fct=None) | `_A_cache_is_stale=True` → 保留 Elm → 下轮全量 |
| 缓存角色对齐失败 | 回退 `_full_mutation` |
| 不足 2 轮 | tail_boundary=len(conv_hist)，全部跳过 |
| session reset | 插件重建 → cache=None → 冷启动 |
