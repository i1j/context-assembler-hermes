---
title: A-stage 角色等级装配（v6 方向 B）
slug: a-stage-role-match
category: architecture
version_introduced: v6.0
status: 已实装
decisions: ["topic-grade-manager", "tail-protection", "bg-review-sync"]
depends_on: ["storage-model", "e-stage-write-protocol"]
updated: 2026-06-28
source_files: ["ca/a_stage.py", "topic_manager.py"]
---

## 问题

A-stage 负责将历史上下文装配为最终的 prompt 文本。v5 使用 topic-aware 三级替换（`_simple_mutation_mode_v5`），
在 Hermes 的 messages 列表上**原地修改**（mutation），存在以下问题：

- mutation 破坏了 Hermes 原始消息，需要 `_full_backup` / `_saved_history_snapshot` 来回退
- 增量缓存（`_A_stable_cache`）增加了复杂度且与话题切换联动脆弱
- Fct pending 防护需要额外的 stale 标记和回退逻辑

v6 方向 B 的解决：从 CA 自有 `turn_stream` DB 重建 conv_history，**完全不接触 Hermes 消息列表**。
旧方向 A 的 `_simple_mutation_mode_v5` / `_incremental_mutation` 及增量缓存（`_A_stable_cache` 等）已于 2026-06-28 正式清理。

### CE 管线已停用（2026-06-28）

v6 初期的 `CAContextEngine.compress()` 负责从 turn_stream 重建 `new_conv` 后原地拷贝回 Hermes 消息列表，
利用 Python identity 去重保持 state.db 完整性。但该方案存在双写风险：

```
should_compress() → True → compress_context 先调 compress() 做原地 mutation
  → compress() 的 append() 产生新 dict（新 identity）
  → abort 后 conversation_history=None → history_ids=set()
  → 新 dict 不被 flushed_ids 覆盖 → state.db 重复行
```

已于 2026-06-28 **彻底停用 CE 管线**：
- `should_compress()` → False（不触发 Hermes compress_context）
- `compress()` → no-op（返回 messages 不变）
- conv_history 由 `_build_conv_history_v6` 从 CA DB 重建，**不经过 CE 管线**

### Gateway 缓存路径双写（v6.1 发现）

CE 停用后，Web UI 会话中仍出现 state.db user 双写。根因在 Hermes Gateway 而非 CA：

```
Gateway 新 turn → _last_flushed_db_idx = 0 → _flushed_db_message_ids = set()
  → _finalize_shutdown_agents 无 conversation_history 二次 flush
  → 所有 user dict 被重新写入 state.db
```

CA 无法预防，注册 `on_session_finalize` hook 做事后清理（见 decision 32）。

**区分两个双写漏洞**：

| 维度 | CE 管线双写（已关闭） | Gateway 缓存双写（当前） |
|------|----------------------|------------------------|
| 根因 | `should_compress → abort → history_ids=set()` | `_last_flushed_db_idx=0 → _flushed_db_message_ids=set()` + 无 conv_history flush |
| CA 可控 | 是（CE 管线是 CA 注册的） | **否**（Gateway 内部机制） |
| 修复 | 停用 CE 管线 | `on_session_finalize` 事后 cleanup |

## 决策

### 选定方案：_build_conv_history_v6

核心实现：`ca/a_stage.py` 的 `AStageMixin._build_conv_history_v6(topic_mgr, system_message=None)`。

流程：
1. 8 个 plugin hook 向 `turn_stream` DB 写入消息（E-stage 写即落盘）
2. `_build_conv_history_v6` 从 `turn_stream` DB 读取全量数据，按三区模型逐 turn 构造 OpenAI 格式消息列表
3. 返回新列表作为 conv_history 替换 Hermes 消息列表（方向 B）

**CA 不再通过 CE 管线修改或重建 Hermes 消息，也无 state.db 写入依赖。**

### 三区模型 + 行类型降级

```
conv_history 结构（三区）：
  ┌─ 远区（FAR 话题）    → user/fin:Hdl,    thought/tool:删除
  ├─ 中区（REL 话题）    → user/fin:Fct,    thought/tool:Hdl
  ├─ 中区（ACT 话题）    → user/fin:Elm,    thought/tool:Fct
  └─ 尾部保护区（最后 2 轮 user） → 全 Elm（不降级）
```

| 位置（区域） | user/fin | thought/tool |
|---|---|---|
| 尾部保护区 | Elm（原文） | Elm（不降级） |
| ACT 区 | Elm | Fct（降一级） |
| REL 区 | Fct | Hdl（降一级） |
| FAR 区 | Hdl | 删除 |

**降一级规则**：thought/tool 行的内容等级比 user/fin 低一级。
尾部保护区内**不执行降级**，所有行保持 Elm。

**尾巴保护优先**：protect_tail 内的 turn（最后 2 个 user 轮）强制走 Elm，不受 grade 影响。

### 实现要点

- 数据源：`turn_stream` 表的 Fct/Hdl 列，纯 DB 读取
- 话题等级查询：`topic_mgr.get_turn_grade(turn)` 返回 `TopicGrade.{ACT,REL,FAR}`
- tail_boundary：扫描 conv_history 找倒数第 2 个 user turn 之后的所有行
- 保护区外：按区域定级 + thought/tool 行降一级，FAR thought/tool 行删除
- system_message：从 Hermes messages[0] 保留，拼接在 conv_history 头部
- **`_build_conv_history_v6` 是独立函数，不经过 CE 管线**
- 无 `replace_messages`、无额外 state.db 连接、无 identity 跟踪依赖

### Fct=NULL 告警（v6.0.1）

当 topic_grade=REL（user/fin→Fct）或 ACT→Fct（thought/tool）时，若 `turn_stream` 中对应行的 Fct 列为 NULL 或空字符串，
`_build_conv_history_v6` 会写 `logger.warning`：

```
[CA_v5] Fct is empty/NULL for grade Fct, turn=N role=assistant, falling back to Elm
```

内容级行为不变（仍回退到 Elm 原文），但运维可见性显著提升——避免静默 token 浪费。

### Tool Fct 空字段过滤（v6.0.3）

`_select_content` 中当 `target_grade=Grade.FCT` 且 `row["role"]="tool"` 时，对工具行的 Fct JSON
做空字段(null/[]/""/0)剔除：

```python
cleaned = {k: v for k, v in fct_dict.items()
           if v is not None and v != [] and v != "" and v != 0}
```

**动机**：单位 Token 互信息最大化。工具 Fct 中 ~84%~100% 行的 `error/implicit_knowledge/next_action_hint/_assemble_status`
为默认空值，共占 Tool Fct 总体积的 25%（约 650 tok/会话）。LLM 不因这些空字段获得额外信息。

**实现位置**：`_select_content`（`a_stage.py`），仅影响 ACT 区 tool 行进入 LLM 上下文时的序列化输出，
不影响 DB 存储（F-stage/E-stage 写入不变）。

## 数据验证

```bash
# 查看 turn_stream 的各行 Fct/Hdl 覆盖率
SELECT turn, role,
       CASE WHEN Fct IS NOT NULL AND Fct != '' THEN '有 Fct' ELSE '缺 Fct' END AS fct_status,
       CASE WHEN Hdl IS NOT NULL AND Hdl != '' THEN '有 Hdl' ELSE '缺 Hdl' END AS hdl_status
FROM turn_stream
ORDER BY turn, seq;
```

## 优点

- turn_stream DB 是单源真相，每次重建重新计算
- `_build_conv_history_v6` 是纯 DB 读取函数，不依赖 Hermes 消息列表格式
- CA 不通过 CE 管线接触 Hermes 消息，无 state.db 写入或 identity 跟踪
- 无需 `_full_backup` / `_saved_history_snapshot` 备份恢复
- 无需增量缓存和 Fct pending 防护
- conv_history 结构可控（只含需要的字段）
- 零额外 SQLite 连接、零写入冲突
- **CE 管线全面停用**：`should_compress() → False`，`compress() → no-op`，消除双写漏洞

## 测试覆盖

- A-stage 装配测试 — `tests/stage/test_a_stage.py`（`TestAAssembly`、`TestAReversibility`）
- A-stage 话题感知测试 — `tests/stage/test_a_stage_topic_aware.py`（`TestTopicGradeDriven`）

## 缺点

- 每次重建都完整遍历 turn_stream DB（性能可接受，因 turn 数<1000）
- `_build_conv_history_v6` 需要 topic_mgr 有完整的 turn→topic 映射（否则退化到 ACT 保守策略）
