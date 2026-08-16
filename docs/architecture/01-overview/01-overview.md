---
title: 系统概览
slug: overview
category: architecture
version_introduced: v6.0
status: 已实装
decisions: [design-philosophy, three-stage-arch, naming-unification]
depends_on: []
updated: 2026-08-13
source_files: ["ca/__init__.py", "plugins/ca_assembler/__init__.py"]
---

## 问题

Hermes 内置 ContextEngine 使用被动 LLM 压缩（一条 Markdown 摘要替代历史），对话变长后上下文质量下降。需要独立、可追溯的上下文管理体系。

## 系统架构

CA 插件通过 8 个 Hermes Hook 插入对话生命周期，数据流：

```
Hook 接收消息 → E-stage 写即落盘 → F-stage 异步摘要 → A-stage 装配 conv_history
```

### 核心术语

| 术语 | 含义 | 示例 |
|------|------|------|
| **Elm** | 原始消息文本 | `"请帮我搜索xxx"` |
| **Fct** | 结构化摘要 JSON（OODA 四段：现象/背景/决策/后续） | `{"changes":[{"stage_tag":"已实施","core_change":"..."}],"new_materials":[...],"objective_facts":[...],"consensus":[...],"todo":[...]}` |
| **Hdl** | 一句话标题 | `"搜索xxx结果"` |

### 三个阶段

| 阶段 | 职责 | 源文件 |
|------|------|--------|
| **E-stage** | 写即落盘：5 hooks 各写 (turn,seq) | `ca/e_stage.py` |
| **F-stage** | 异步 LLM 摘要生成（fin 粒度） | `ca/f_stage.py` |
| **A-stage** | 从 DB 重建 conv_history（方向 B；当前经 CE 壳路径未激活，见方向 B 修订注） | `ca/a_stage.py` |
| **L-stage** | 引擎生命周期管理（reset/wait_for_pending；补全线程已由 F-stage 取代） | `ca/lstage.py` |
| **L4 精炼** | 空闲期自我维护管线（IdleRefinementDaemon，realities 归并审查） | `ca/refinement.py` |

### 话题等级（topic grade）

| 等级 | 含义 | 处理策略 |
|------|------|----------|
| **ACT** | 活跃话题 | user+fin=Elm, thought/tool=Fct |
| **REL** | 相关话题 | Fct+Hdl，保留关键信息 |
| **FAR** | 无关话题 | user+fin=Hdl, thought/tool=删除 |

### 方向 B（v6.0）

`_build_conv_history_v6` 从 turn_stream DB 直接构造 conv_history 列表，**不修改 Hermes 消息**。
> 修订（2026-08-15）：`_build_conv_history_v6` 由 CE 壳 `CAContextEngine.select_context()`
> 每轮调用（`conversation_loop.py` 调用点），8 个 hooks 负责 E-stage 写入 / F-stage 摘要 /
> 话题检测 / recall 注入。A-stage 重建生产已激活，`should_compress()` 恒 False。

### CE 壳定位（用户设计定论，2026-08-15 修订）

- **CA 注册 ContextEngine（`context.engine: ca_assembler`）是「替代」内置 ContextCompressor 的占位**，不是让 Hermes 跑 compress_context 流程。
- **CA 不触发 Hermes compress_context 的 archive/rotation**：CE shell 的 `should_compress` 恒返回 False，`compress()` 仅作为手动 /compress 回退路径。
- **CE 壳注册已恢复（1 参签名）**：`__init__.py` register() 使用 `ctx.register_context_engine(_ce_engine)`（Hermes `hermes_cli/plugins.py:1898`）。旧 2 参写法会抛 TypeError 并使 Hermes dispose 全部 registration（08-13 停摆），**勿再恢复旧 2 参写法**。
- **A-stage 组装（`_build_conv_history_v6`）与 8 个 hooks 的分工**：hooks 驱动 E-stage 写入（pre_llm_call 写 seq 0 → 话题检测 → wiki recall；post_llm_call 写 fin → F-stage 异步摘要）；`select_context()` 每轮调用 `_build_conv_history_v6` 完成 conv_history 重建。
- 判定 CA 工作正常的标准 = hook 链路（E-stage 写库 / F-stage 摘要 / 话题检测 / wiki recall）数据完整，且 `select_context()` 每轮从 turn_stream DB 重建 conv_history。
