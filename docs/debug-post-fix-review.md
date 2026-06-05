# CA v4.4.0 修复验证 + 遗留问题扫描报告

**检查日期**: 2026-06-05
**检查范围**: `ca/__init__.py`、`ca/cache.py`、`tests/conftest.py`

---

## 一、上一轮 Bug 修复验证

### Bug 1: C-stage Executor Shutdown 竞态 ✅ 已修复

| 项                    | 验证结果                                                        |
| --------------------- | --------------------------------------------------------------- |
| 根因                  | `_shutdown_cache_executor()` 没设 `cache._destroyed = True` |
| 修复方案              | `destroy()` 和 `reset()` 改用 `cache.destroy()`           |
| `destroy()` 第245行 | ✅`self.cache.destroy()`                                      |
| `reset()` 第1149行  | ✅`self.cache.destroy()`                                      |
| `_destroyed` 守卫   | ✅`cache.py` 第183行 `if self._destroyed: return` 拦截正常  |

### Bug 2: Head 保护区方向错误 ✅ 已修复

| 项                               | 验证结果                                                         |
| -------------------------------- | ---------------------------------------------------------------- |
| 根因                             | `sorted(...)[-HEAD_AUTO_L1_COUNT:]` 取了末尾 3 轮而非开头 3 轮 |
| 修复方案                         | `[-N:]` → `[:N]`                                            |
| `_compute_layers_v2()` 第669行 | ✅`[:Config.HEAD_AUTO_L1_COUNT]`                               |

---

## 二、新发现

### 2.1 `_shutdown_cache_executor()` 死代码 — 低

**文件**: `ca/__init__.py` 第254-262行

```python
def _shutdown_cache_executor(self):           # 无调用方
    if sys.version_info >= (3, 10):
        try:
            self.cache._rebuild_executor.shutdown(wait=True, timeout=Config.SHUTDOWN_TIMEOUT)
        except Exception:
            logger.warning("Cache executor shutdown timeout")
            self.cache._rebuild_executor.shutdown(wait=False)
    else:
        self.cache._rebuild_executor.shutdown(wait=False)
```

**现象**: 方法定义存在，但全项目只有 `docs/debug-executor-shutdown-fix.md` 中的历史引用，实际代码中**无调用方**。

**根因**: Bug 1 修复时将 `destroy()` 和 `reset()` 的关闭逻辑替换为 `cache.destroy()`，但未清理旧方法。

**安全隐患**: `cache.destroy()`（`ca/cache.py` 第273-276行）直接 `shutdown(wait=False)` 强关，丢失了旧代码的 `wait=True` + 超时 fallback 优雅关闭策略。

```python
def destroy(self):
    self._destroyed = True
    self.cancel_retry_timer()
    self._rebuild_executor.shutdown(wait=False)  # ← wait=False，无超时fallback
```

**影响评估**: 低风险。destroy() 调用前已有 `wait_for_pending(5.0)` 等待 C-stage 线程结束 + `_destroyed=True` 拦截后续 submit，`wait=False` 不会造成实际损坏。但在极端长耗时场景（C-stage 超 5s），executor 仍有未完成任务时被强关。

**修复方向**: 可选 — 删除 `_shutdown_cache_executor()`，或在 `cache.destroy()` 中加入 `wait=True`+超时 fallback。

---

### 2.2 测试文件 bare `except:` 吞异常 — 低

**文件**: `tests/conftest.py` 第23、29、46、48行

```python
except: pass        # 吞所有异常
except: return -1   # 吞所有异常
```

**影响**: 仅在测试辅助函数（`_get_cpu_brand()`、`get_fd_count()`）中，不影响被测代码。实际返回值是"unknown" / `-1`，调用方有处理。

**修复方向**: `except Exception:` 替代 `except:`（避免吞 `KeyboardInterrupt` / `SystemExit`）。

---

## 三、遗留项目录a

| # | 问题                                                                       | 文件                                 | 行            | 严重度 | 状态   |
| - | -------------------------------------------------------------------------- | ------------------------------------ | ------------- | ------ | ------ |
| 1 | `_shutdown_cache_executor()` 死代码 + `cache.destroy()` 丢失 wait=True | `ca/__init__.py` + `ca/cache.py` | 254-262 / 276 | 低     | 待清理 |
| 2 | 指纹去重未实现（TODO）                                                     | `ca/__init__.py`                   | 518           | 中     | 待实现 |
| 3 | bare `except:` 吞异常                                                    | `tests/conftest.py`                | 23,29,46,48   | 低     | 待修   |
