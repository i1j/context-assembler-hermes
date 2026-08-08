---
title: bg_review A-stage 跳过闸门
date: 2026-06-16
status: 已实施
type: impl
commit: (插件 __init__.py 改动，无 engine 层变更)
---

## 背景

bg_review（后台审查）fork 创建一个独立的 AIAgent 实例，其 `conversation_history` 是父 agent 的 `messages_snapshot`（完整原文）。CA 原有的 A-stage 组装会对这些原文做压缩（Fct/Hdl 摘要），导致 bg_review agent 收到的上下文不是完整对话，从而影响其判断"是否值得存 memory/skill"的准确性。

## 方案

在插件层 `_on_pre_llm_call` 中加一个早返 gate，检测当前调用是否来自 bg_review fork。如是，跳过整个 A-stage 组装管线（不做 plan 计算、不做 mutation），让 bg_review agent 的 conversation_history 保持为父 agent 的原始消息快照。

## 改动详情

### 文件：`plugins/ca_assembler/__init__.py`

**改动 1 — import gate（行 27-33）**

```python
# ── bg_review 检测（当前轮类型识别，用于跳过 A-stage 组装）──
try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"
```

Hermes 运行时 `tools.skill_provenance` 在 sys.path 上，import 成功 → 返回真实 ContextVar 值。
测试环境 / IDE 中 import 失败 → 降级返回 `"unknown"`（gate 不激活，走现有代码）。

**改动 2 — early-return gate（行 117-124）**

```python
    # ── bg_review 轮跳过 A-stage 组装 ──
    # bg_review agent 的 conversation_history 是父 agent 的消息快照（完整原文），
    # CA 压缩会破坏 bg_review 的审查判断（需原文评估是否值得存 memory/skill）。
    # C-stage 继续积累数据（写 biz_category 供后续轮过滤）。
    if get_current_write_origin() == "background_review":
        logger.info("[CA] _on_pre_llm_call: background_review, skip A-stage assembly")
        return None
```

### 不动

- `_on_post_llm_call` / `post_llm_call`：C-stage 继续运行
- `process_turn_async` / `_run_c_stage`：已在写入 `biz_category="bg_review"`
- 整个 `ca/` 子目录（engine 层）：零改动

## 信号可靠性验证

```
turn_context.py:108-109
  set_current_write_origin(getattr(agent, "_memory_write_origin", ...))
       │
       ▼  (同一线程，无竞态)
turn_context.py:320-331
  _invoke_hook("pre_llm_call", ...)
       │
       ▼
CA: _on_pre_llm_call()
  get_current_write_origin() → ContextVar 已绑定
```

bg_review agent 的 `_memory_write_origin` 在 `background_review.py:417` 设为 `"background_review"`，经 `turn_context.py` 桥接到 ContextVar。ContextVar 在 hook 调度**之前**绑定，信号可靠。

## 并发安全

bg_review fork 和父 agent 共享 `session_id`，因此共享 `_engines[session_id]` 引擎实例。`_saved_history_snapshot` 可能被两个线程同时访问。现状：

- ✅ **此改动不恶化该问题**：gate 减少了一次快照写入（pre_llm_call 不保存），反而降低冲突概率
- ⚠️ bg_review 的 post_llm_call 恢复快照时，可能拿到的是父 turn 的快照，但父 agent 的 `conversation_history` 列表引用独立，互不影响
- 属已有问题，不在本次范围

## 边界情况

| 边界 | 行为 |
|------|------|
| `get_current_write_origin()` 返回 `"foreground"` | gate 不激活，走现有全链路 |
| import 失败 → `"unknown"` | gate 不激活，安全降级 |
| bg_review + `injection_mode="append"` | gate 在 `pre_llm_call()` 前返回，不执行 `_annotation_mode` |
| bg_review + engine errored | 先有 `_engine_errored` 挡在第一层，双层保护 |
| 首轮就是 bg_review | 无 history 可组装，gate 返回 None |

## 测试验证

```
315 passed, 4 failed (既存，test_a.py 标签注入), 20 skipped
0 regression
```

gate 行为在现有测试中 0 覆盖（测试 fixture 的 AIAgent 无 `_memory_write_origin` 覆盖）。后续可按需加 monkeypatch 测试：

```python
def test_pre_llm_call_skips_for_bg_review(monkeypatch):
    monkeypatch.setattr(
        "plugins.ca_assembler.__init__.get_current_write_origin",
        lambda: "background_review",
    )
    result = _on_pre_llm_call(session_id="test", ...)
    assert result is None
```

## 数据流（改动后）

```
正常对话轮 Tn:
  pre_llm_call → get_current_write_origin()="foreground" → 全量组装
  post_llm_call → write_turn(biz_category=NULL)

bg_review 轮:
  pre_llm_call → get_current_write_origin()="background_review" → return None（不碰 history）
  post_llm_call → process_turn_async() → write_turn(biz_category="bg_review")

后续正常轮 T(n+1):
  pre_llm_call → read_turn_biz_categories() → 过滤 bg_review → 正确配额
```
