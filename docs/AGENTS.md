# CA Assembler Plugin — 开发指南

> **首读** → [`docs/wiki/INDEX.md`](wiki/INDEX.md)（架构关系图 + 全量索引）
> 双维文档体系：`architecture/`（空间：当前系统组件）+ `decisions/`（时间：决策树）

## 项目结构

```
plugins/ca_assembler/
├── __init__.py           # 插件入口：hook 注册 + CE + 插件类
├── plugin.yaml           # 插件元信息
├── ca/
│   ├── __init__.py       # ContextAssembler (SessionManager)
│   ├── a_stage.py        # A-stage: _build_conv_history_v6（方向 B）
│   ├── e_stage.py        # E-stage: 写即落盘 (5 hooks)
│   ├── f_stage.py        # F-stage: 异步 LLM 摘要
│   ├── store.py          # turn_stream SQLite 存储 (v5.10)
│   ├── config.py         # 配置系统
│   ├── topic_manager.py  # 话题检测 + 等级定级
│   ├── grade.py          # Grade / TopicGrade 枚举
│   └── ...               # 辅助模块
├── docs/
│   ├── wiki/             # 技术方案文档 — 首读 INDEX.md
│   │   ├── INDEX.md      # ← ★ 方案入口：关系图 + 全量索引
│   │   ├── architecture/ # 空间维度：系统组件
│   │   └── decisions/    # 时间维度：决策树
│   └── AGENTS.md         # ← 当前文件
├── tests/                # pytest 测试
└── tools/                # 工具函数
```

## 核心设计：state.db 完整性

CA 不操作 Hermes 的 state.db。CA 维护独立的 `ca_cache/{session_id}.db`（turn_stream 表）。

### 双写漏洞的两次发现

**① CE 管线双写（已停用，2026-06-28 清理）**

旧设计中 `should_compress() → True` 配合 `_last_compress_aborted` flag
的 abort 机制，会导致 `compress_context` 先调 `compress()` 做原地 mutation
再检查 abort 返回 → `conversation_history_after_compression → None`
→ 后续 `_persist_session` 因 `history_ids=set()` 无法识别新 append 的
dict → state.db 出现重复 user 消息行。

已在 v6.0 通过停用 CE 管线消除。

详见 `docs/wiki/decisions/31-ce-shell-registration.md`。

**② Gateway 缓存路径双写（当前，v6.1 on_session_finalize 清理）**

CE 停用后，Web UI 会话中仍出现 state.db user 双写。根因不在 CA 而在 Hermes Gateway：

```
Gateway 新 turn → _last_flushed_db_idx = 0
  → _flushed_db_message_ids = set()  （身份追踪被清空）
  → _finalize_shutdown_agents 无 conversation_history 二次 flush
  → 所有 user dict 被重新写入 state.db
```

CA 无法预防（无法接触 agent 内部 `_flushed_db_message_ids`/`_last_flushed_db_idx`），
因此注册 `on_session_finalize` hook 做事后 cleanup。

详见 `docs/wiki/decisions/32-state-db-user-dedup.md`。

### 清理 SQL

```sql
DELETE FROM messages WHERE id IN (
    SELECT b.id FROM messages a
    JOIN messages b ON b.id = a.id + 1
    WHERE a.session_id=? AND b.session_id=?
    AND a.role='user' AND b.role='user'
    AND (a.content = b.content OR (a.content IS NULL AND b.content IS NULL))
)
```

仅删除相邻（gap=1）且内容完全相同的 user 行，保留第一次写入。

## 关键约束

- **不写 state.db** — CA 任何代码路径不调用 `SessionDB`、`replace_messages`、`append_message`
- **不硬编码 `~/.hermes` 路径** — 使用 `get_hermes_home()` 获取 CA cache 目录
- **术语唯一** — Elm/Fct/Hdl（禁用 L0/L1/L2）
- **E-stage 写即落盘** — 每条消息立即写入 turn_stream，不经 buffer
