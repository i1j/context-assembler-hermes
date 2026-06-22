# CR-007 故障报告 — 断路器缺失 + 死胎测试双重漏洞

## 现象
所有新格式 session ID（`mql*` 前缀）的 CA DB 均为 schema-only（3 页, 0 行）。

## 根因（生产代码）

`__init__.py` 中 `_record_success()` / `_record_failure()` 等 8 个断路器函数从未被实现。
commit `2c4e34b` (TP-008 死代码清理) 删除断路器代码但保留了 3 个调用点：
- `on_session_start()` L179: `_record_success()` → NameError → `_engine = None` → 后续 E-stage hooks 全跳过
- `on_session_start()` L173: `_record_failure()` → 同上
- `on_session_reset()` L209: `_record_success()` → 同上

## 根因（测试体系）— 双重 DOA

### DOA #1: import 崩溃
同一 commit `2c4e34b` 新建了 `tests/plugin/test_plugin.py`，在模块级引用不存在的函数：
```python
is_available = _ca_plugin.is_available        # AttributeError
````
pytest 收集阶段直接崩溃，**33 个测试从未执行**。

### DOA #2: 命名不匹配
`tests/audit/cross_ref_wiki_audit.py` 文件名不匹配 `test_*.py` 模式，且目录无 `__init__.py`，**35 个交叉验证测试从未被收集**。

### 为何未被发现
```bash
# 作者跑的是 --ignore 过滤的全量
pytest tests/ --ignore=tests/plugin   → 292 passed ✓

# 而非包含 plugin 的完整收集
pytest tests/                          → ERROR collecting plugin/test_plugin.py ❌

# commit msg 虚报: "324 test passed" = 292(实际跑的) + 32(新文件里数的)
```

## 修复（2026-06-20）

### 断路器实现
在 `__init__.py` 模块级插入 8 个函数：`_state_file_path`, `_read_state`, `_write_state`, `_record_success`, `_record_failure`, `is_available`, `_cleanup_stale_state_files`, `_pid_exists`。

### 命名 DOA 修复
- 重命名 `cross_ref_wiki_audit.py` → `test_cross_ref_wiki_audit.py`
- 补 `tests/audit/__init__.py`

### 防御体系新增
| 防御层 | 文件 | 作用 |
|--------|------|------|
| 自防御测试 | `tests/audit/test_doa_self_defense.py` | 3 个运行时检查：命名 DOA / import DOA / 死目录 |
| 预检脚本 | `scripts/test-preflight.sh` | CI 集成：收集完整性 + 命名 DOA + 死目录 |
| 运行包装器 | `tests/run_ca_tests.py` | 自动执行预检后再跑测试 |

## 验证
- 全量: **385 passed, 1 skipped**（较之前+78，其中 68 个从 DOA 恢复）
- 所有防御测试通过
