# C-stage Executor Shutdown 竞态 — 调试记录

**发现**: 2026-06-05  
**修复**: 2026-06-05  

## 现象

```
Exception in thread CA-CStage-16:
Traceback (most recent call last):
  File "ca/__init__.py", line 360, in _run_c_stage
    self.cache.add_turn(turn_index, l0_text, l1_str, l0_emb, l1_emb)
  File "ca/cache.py", line 168, in add_turn
    self._submit_rebuild()
  File "ca/cache.py", line 188, in _submit_rebuild
    self._rebuild_future = self._rebuild_executor.submit(self._rebuild_if_dirty)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "thread.py", line 167, in submit
    raise RuntimeError('cannot schedule new futures after shutdown')
RuntimeError: cannot schedule new futures after shutdown
```

异常处理路径（降级写"无有效增量"）同样走 `add_turn()` → 同样的 RuntimeError，出现第二段 trace。

## 因果关系

```
reset() / destroy()
  │
  ├─ wait_for_pending(5.0)       ← 等待 C-stage 线程（可能超时）
  ├─ _shutdown_cache_executor()
  │    └─ executor.shutdown()    ← 杀了 executor
  │    └─ BUT 不设 _destroyed   ← ！！！
  │
  └─ 替换 self.cache = 新 cache

旧 C-stage 线程（未在 5s 内完成）完成后：
  add_turn() → _submit_rebuild()
    ├─ if self._destroyed: return   ← False！放行
    └─ executor.submit()            ← RuntimeError!
```

## 根因

`_shutdown_cache_executor()` 直接调 `cache._rebuild_executor.shutdown()` 关 executor，但**没设 `cache._destroyed = True`**。 `cache.py` 的 `_submit_rebuild()` 第183行虽然有 `if self._destroyed: return` 守卫，但 `_destroyed` 一直是 `False`，守卫无效。

`cache.destroy()` 方法同时做了三件事——设 `_destroyed` + cancel retry timer + shutdown executor——但两个调用方（`destroy()` 和 `reset()`）都没用它，而是手动拆开两步骤，恰好漏了关键的 `_destroyed` 赋值。

## 修复

两处调用：

**`destroy()` 第245-246行**：
```python
# before:
self.cache.cancel_retry_timer()
self._shutdown_cache_executor()
# after:
self.cache.destroy()
```

**`reset()` 第1149-1150行**：
```python
# before:
self._shutdown_cache_executor()
self.cache.cancel_retry_timer()
# after:
self.cache.destroy()
```

`cache.destroy()` 同时做：
1. `self._destroyed = True` — 之前漏了这行
2. `self.cancel_retry_timer()`
3. `self._rebuild_executor.shutdown(wait=False)`

`reset()` 在 destroy() 后立即 `CacheBuilder.build()` 建新 cache（含新 executor），旧 cache 实例丢弃，互不影响。

## 后续检查

- `_shutdown_cache_executor()` 方法现在无调用方，可考虑清理
- 确认 `cache.destroy()` 的 `wait=False` shutdown 是否足够（`_shutdown_cache_executor` 在 >=3.10 时尝试 `wait=True` 超时后 fallback 到 `wait=False`）

---

## C.2 Head 保护区方向错误（2026-06-05 发现）

### 现象

CA 的 Head 保护区实际保护的是**最近 3 轮**对话（T10-T12），而非**最开始 3 轮**（T1-T3）。

从 turn_plan 数据可见：
```
T1  🟢L0 middle      ← 应该进 Head 保护区，实际在中区
T2  🟢L0 middle      ← 同上
T3  🟢L0 middle      ← 同上
T4  🟡L1 retrieved   ← 中区检索升级（正确行为）
...
T10 🟡L1 head        ← 最近轮被错标为 Head
T11 🟡L1 head        ← 同上
T12 🟡L1 head        ← 同上
```

### 根因

**`_compute_layers_v2()`** 第 667-669 行：
```python
valid_dialogue = sorted(
    [idx for idx in l1_texts if self._is_valid_summary(l1_texts[idx])]
)[-Config.HEAD_AUTO_L1_COUNT:]    # ← 取末尾 3 个
```

`sorted()` 按 turn_index 升序（T1, T2, ..., T12）。 `[-3:]` 取**最后 3 个**（T10, T11, T12）作为 Head。

但 Head 保护区的设计目标是保留**最开始几轮的信息**以防 LLM 遗忘早期上下文。应该取**前 3 个** `[:3]`。

### 影响

| 面 | 当前（T10-T12 被保） | 期望（T1-T3 被保） |
|----|---------------------|-------------------|
| 最早对话（T1-T3） | 中区 L0/回收升级 → 可能丢失细节 | 固定 L1 保护 ✅ |
| 最新对话（T10-T12） | 多余保护（本来就在 LLM 窗内） | 中区 L1/回收升级 → 信息足够 |
| T4-T7 有效 OODA | 中区回收升级，正确 | 同样中区回收升级，不变 |
| T17-T26 无有效增量 | 尾区 L2 保护（正确） | 尾区 L2 保护（不变） |

### 修复方向

第 669 行 `[-Config.HEAD_AUTO_L1_COUNT:]` → `[:Config.HEAD_AUTO_L1_COUNT]`

### 用户提供的 CA 注入数据佐证

**T1**（应在 Head，实际在中区 middle/L0，但 injection 仍带了完整 L1 JSON）：
```json
{"core_change": "明确CA插件提交目标文件夹",
 "new_materials": ["CA插件文件夹路径", "两个CA项目结构", "提交目标待确认"],
 "objective_facts": ["仅可提交CA插件文件夹", "需明确选择目标", "无权限修改路径"],
 "consensus": ["优先提交tester profile CA", "可选独立CA项目", "需用户确认选择"],
 "todo": ["列出选项供选择", "提交前确认路径", "记录用户选择结果"]}
```

**T2**（同样应在 Head，实际 middle/L0，injection 也带了完整 L1 JSON）：
```json
{"core_change": "工作区干净 CA 插件已提交",
 "new_materials": ["提交完成", "工作区干净", "无异常反馈"],
 ...}
```

两轮都是高质量 L1 摘要，应当进 Head 保护区固定 L1，而不是依赖检索升级保留。

### 备注

CA injection（`pre_llm_call` 返回 `[~/N]` 前缀注入）全程独立于 assemble 的 Head/Middle/Tail 决策。T1-T2 虽然在中区，但 injection 仍然输出了完整 L1 JSON，避免信息丢失。所以这个 bug 在当前会话中**未造成实质性信息丢失**，但在极端长会话中，Head 保护方向错误可能导致最早几轮的关键 L1 被检索跳过。

**修复**（2026-06-05）：第 669 行 `[-HEAD_AUTO_L1_COUNT:]` → `[:HEAD_AUTO_L1_COUNT]`。

### 当前预算状况（2026-06-05 20:xx 快照）

| 指标 | 值 |
|------|-----|
| 累计 32 轮对话 | 原始 L2 总和 ≈ 650K tok |
| CA assemble 后 | ≈ 147K tok |
| CA 压缩预算上限 | 500K tok（1M×0.50） |
| 可用预算 | 475K tok（预算×0.95） |
| CLI 显示 input | ≈ 214K tok |

**预算未超**。CA 的压缩预算只约束 `assemble()` 输出的对话历史大小（147K < 475K ✅）。CLI 多出的 67K 是系统提示词 + CA injection 注入的 `[~/N]` 前缀文本（如 `[~/12/61]` 整段 execute_code 源码和输出）。

CA injection 是不压缩的——所有 `[~/N]` 摘要文本追加到 user message 尾部。这是架构设计（增强而非压缩），不是预算问题。`
