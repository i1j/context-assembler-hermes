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

`_build_conv_history_v6` 从 turn_stream DB 直接构造 conv_history 列表，**不修改 Hermes 消息**。CE 管线已于 2026-06-28 停用。
> 修订（2026-08-13）：代码事实——`_build_conv_history_v6` **仅**由 CE 壳 `compress()` 调用
> （`__init__.py:352`），8 个 hooks 从不调用它；CE 壳注册 2026-08-13 起暂停 → **A-stage
> 重建当前未激活**（详见下文 CE 壳定位修订 + `docs/migration-research-dsh.md` §1.5/§6）。

### CE 壳定位（用户设计定论，2026-08-13 修订）

- **CA 注册 ContextEngine（`context.engine: ca_assembler`）是「替代」内置 ContextCompressor 的占位**，不是让 Hermes 跑 compress_context 流程。
- **CA 不触发 Hermes compress_context 的 archive/rotation**：CE shell 的 `should_compress` 恒返回 True（`__init__.py`，pre-set abort 标志 `_last_compress_aborted` 阻止 archive/rotation）。
- **⚠️ CE 壳注册已暂停（2026-08-13 修复）**：旧代码 `ctx.register_context_engine("ca_assembler", _ce_engine)` 传 2 参数 vs Hermes 接口 `register_context_engine(self, engine)` 1 参数 → 必抛 TypeError；Hermes commit `22af80bcf`（08-01）起 register() 抛异常会 **dispose 该插件全部 registration（含 8 个 hooks）→ 插件加载失败、CA 停摆**。修复：`__init__.py` register() 已注释该行（hooks 恢复、engine 回退内置 compressor）。原"参数警告为无害已知项、hooks 不被回滚"的记录**已失效**。恢复 CE 壳的前置条件见 `docs/migration-research-dsh.md` §6.3（条件式 should_compress、pre_llm_call 模式守卫、前检压缩与 seq 0 写入的轮序处理）。
- **A-stage 组装（`_build_conv_history_v6`）与 8 个 hooks 的分工（修订）**：hooks 驱动 E-stage 写入（pre_llm_call 写 seq 0 → 话题检测 → wiki recall；post_llm_call 写 fin → F-stage 异步摘要）；`_build_conv_history_v6` **仅**经 CE 壳 compress() 路径可达，当前未激活。原文档"A-stage 完全由 8 个 hooks 驱动"为误述。
- 判定 CA 工作正常的标准 = hook 链路（E-stage 写库 / F-stage 摘要 / 话题检测 / wiki recall）数据完整，与 CE 是否被 Hermes 选中无关。
