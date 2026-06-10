# 180K 上下文字段 LLM 理解错位 — 诊断与修复

**编制日期**: 2026-06-10
**会话**: `20260610_203555_4b6658`
**模型**: deepseek-v4-flash (provider=deepseek)
**CA 版本**: v5.1

## 摘要

对话积累到 ~180K token 时，云端 LLM 出现回复与用户指令错位的系统性错误。根因为 CA 压缩预算被调试屏蔽锁死在 100K，加上 bg_review 路径生成空壳摘要、bypass 保护范围不完整，导致前期对话上下文在 180K 处已不可恢复。

## 根因链（三因素 + 一缺口）

### 根因A：context_length 被调试屏蔽锁死在 100K

`ca/config.py:context_length_for_model()` 中存在两处 `if False`：

```python
# 行 152 — 2026-06-05 屏蔽
if False:  # Hermes get_model_context_length → 返回 1M
    window = get_model_context_length(...)

# 行 161 — 2026-06-14 屏蔽
if False:  # _MODEL_CONTEXT_WINDOW → deepseek-v4-flash=1_000_000
    window = cls._MODEL_CONTEXT_WINDOW.get(model_name)
```

两路屏蔽后走兜底 `cls.CONTEXT_LENGTH=200000`（来自 `.env`），再乘 `COMPRESSION_THRESHOLD=0.5` → 预算 = 100K。实际 deepseek-v4-flash 的上下文窗口 = 1M。

**影响**：CA 认为只有 100K 预算，从早期对话轮就开始强制压缩。绕过屏蔽后预算恢复至 500K（1M×0.5）。

### 根因B：bg_review 路径生成空壳摘要

`ca/__init__.py:356-361`：

```python
if bg_review:
    cleaned = {
        "core_change": "系统后台审查",  # ← 硬编码！用户 6.5K 字节消息只剩 3 个字
        "_assemble_status": 0
    }
```

bg_review 路径跳过 L1 生成 LLM 调用，直接写入固定字符串。两个受影响会话共有 4 个 turn 被此方式吃掉：

| 会话 | turn | 原文大小 | L1 摘要 | 语义损失 |
|------|------|---------|---------|---------|
| 183701 (238K) | turn 2 | 6,826 B | 48 B | 99.3% |
| 183701 (238K) | turn 4 | 6,564 B | 48 B | 99.3% |
| 193912 (207K) | turn 2 | 6,536 B | 48 B | 99.3% |
| 193912 (207K) | turn 4 | 6,763 B | 48 B | 99.3% |

### 根因C：预算不足导致更早对话轮强制 L0

预算分配：`100K × 0.95 = 95K → -系统开销(~20K) → -tail保护(~20K) → ~55K 可用`。55K 要覆盖整个会话体。在 180K 累计点，早期对话轮的 L1/L0 摘要已退化到不可恢复。

### 缺口D：bypass 保护未覆盖对话轮 user 行

`_build_aligned_outcomes` 对 bypass turn（最后 2 对话轮）的工具组有保护：

```python
if entry.turn_index in _bypass_set:
    outcomes.append(None)  # 工具组 → 原文
```

但对话轮 user 行处理中没有 bypass 检查：

```python
if entry.target_level == "L2":         # ← 缺少 or entry.turn_index in _bypass_set
    outcomes.append(None)               # 仅 L2 才原文
```

导致 turn 3（L1）、turn 4（L0）的对话内容被压缩，而最后 2 轮本应是原始保护区。

## 修复清单

| # | 文件 | 行 | 改动 | 类型 |
|---|------|-----|------|------|
| 1 | `ca/config.py` | 152, 161 | 移除 `if False` 调试屏蔽，激活 Hermes 查表 + 自有模型表 | 严重 |
| 2 | `ca/__init__.py` | 356-361 | bg_review 路径用 `user_message[:80]` 替代硬编码"系统后台审查" | 严重 |
| 3 | `ca/__init__.py` | 2016 | bypass 保护扩展至对话轮 user 行：`target_level=="L2" or turn_index in _bypass_set` | 严重 |
| 4 | `ca/__init__.py` | 1024-1031 | `_budget < 20000` 时 WARNING 日志 | 辅助诊断 |

## 验证数据

**修复前预算：100K → 修复后：500K**

```
← before: Hermes查表(1M) → if False → 模型表(1M) → if False → 兜底(200K) ×0.5 = 100K
→ after:  Hermes查表(1M) → try/except → 命中 → ×0.5 = 500K
```

**CA 缓存 DB 验证**（会话 183701）：

| 指标 | before | after |
|------|--------|-------|
| budget_remaining | ~90,594 | ~474,000* |
| turn 2 L1 摘要 | "系统后台审查" | user_message[:80] |
| turn 3 bypass 保护 | 仅工具组 | 对话 + 工具组 |

*注: after 值基于公式推估，需实际运行后确认。

## 残余问题（非 180K 关键路径）

1. **单工具组 group_result 退化**（`tool_summarizer.py:948-950`）
   - 61/79 工具组（77%）显示"调用 1 个工具"而非实际结果
   - 不影响最后 2 轮 bypass（原文）
   - 影响更早轮的上下文质量

2. **工具行 _turn_index 标记缺失**
   - `_format_tool_group_assembly` 中的个体工具详情行因 Hermes 消息无 `_turn_index` 而永不输出
   - 导致 single-tool 组的 group_result 成为唯一出口

## 文件变更

- `ca/config.py` — `context_length_for_model()` 调试屏蔽移除
- `ca/__init__.py` — bg_review 摘要修复 + bypass 范围扩展 + budget 告警

## 调试记录

```python
# 验证命令
import sqlite3
db = sqlite3.connect('~/.hermes/profiles/tester/ca_cache/{session_id}.db')
c = db.cursor()
c.execute('SELECT turn_index, length(l1_text), l1_text FROM turn_cache WHERE role="user" AND length(l1_text) < 100')
# 应无 l1_text < 100 字节的差异轮
```
