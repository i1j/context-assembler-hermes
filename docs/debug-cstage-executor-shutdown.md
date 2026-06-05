# CA v4.4.0 — C-stage Executor Shutdown 竞态故障

> 记录时间: 2026-06-05, 对话期间
> Agent: Hermes (deepseek-v4-flash)
> Profile: tester

## 故障现象

```
Exception in thread CA-CStage-22:
RuntimeError: cannot schedule new futures after shutdown
```

C-stage 异步线程 `add_turn()` → `_submit_rebuild()` 时 executor 已关闭。

## 因果关系链

```
Hermes 触发 reset() (on_session_end/reset)
  |
  reset():
  ├─ wait_for_pending(5.0)     ← 尝试等C-stage结束
  ├─ _shutdown_cache_executor()  ← shutdown executor
  └─ 重建 cache（新executor）     ← cache.py 第 146 行
  |
  C-stage 线程仍在跑（等待 embedding 返回）
  ├─ embedding 超时默认 = 10s (Config.EMBED_TIMEOUT)
  ├─ shutdown 超时默认 = 5s  (Config.SHUTDOWN_TIMEOUT)
  └─ 10s > 5s → executor 先关，embedding 后回
  |
  C-stage 完成 → add_turn() → _submit_rebuild()
  → executor.submit() → RuntimeError
```

## 影响范围

| 项 | 值 |
|----|-----|
| 丢失本轮 `add_turn`（摘要+嵌入→BM25快照） | ✅ |
| DB 原始数据（L2/L1/L0） | ✅ 完好（C-stage 先写入 DB，再调 cache） |
| A-stage assemble() 后续读取 | ✅ 从 DB 重建，不丢数据 |
| 后续 BM25 检索 | ⚠️ 缺本轮增量，需下一轮 C-stage 补 |

注：第二段 trace 显示降级路径也走了同一 `add_turn` → 一样的 executor shutdown 错误，说明降级逻辑未处理 executor 已关的场景。

## 代码定位

| 位置 | 行 | 说明 |
|------|-----|------|
| `ca/__init__.py` reset() | 870–881 | `wait_for_pending(5.0)` 后 shutdown executor |
| `ca/__init__.py` _shutdown_cache_executor() | 193–201 | shutdown 策略（wait=5s fallback to wait=False） |
| `ca/__init__.py` wait_for_pending() | 883–890 | 默认 timeout=30s，但 reset 硬编码 5.0 |
| `ca/cache.py` _submit_rebuild() | 188 | executor.submit() → RuntimeError |
| `ca/config.py` SHUTDOWN_TIMEOUT | 81 | 默认 **5** |
| `ca/config.py` EMBED_TIMEOUT | 41 | 默认 **10** |
| `ca/config.py` LLM_TIMEOUT | 52 | 默认 **120** |

## 修复建议

1. **`reset()` 用 `Config.SHUTDOWN_TIMEOUT` 代替硬编码 5.0** — 当前硬编码 5.0 覆盖了 Config 值
2. **`_submit_rebuild()` 增加 RuntimeError catch** — executor 已关时静默跳过而非抛异常
3. **`add_turn()` 在 fallback 路径也保护 _submit_rebuild** — 降级路径同样会触发同一错误
4. **考虑 `wait_for_pending(Config.SHUTDOWN_TIMEOUT)`** — 统一超时来源

## Observation

Hermes agent (tester profile), 2026-06-05 session
