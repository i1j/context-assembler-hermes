# P0: conftest 测试基础设施改进

> 目标：改善 conftest.py 及测试基础设施，消除标记重复、import 脆弱性、autouse fixture 过度耦合、文档漂移。

## Phase 1 — 标记定义合并

**问题**：pytest.ini 和 conftest.py 各自注册 marker，存在重叠（`llm_return`/`critical`/`high`/`medium`/`low`/`linux_only`/`v460`），且 conftest 特有 `fct`/`elm` 未在 pytest.ini 注册 → 使用 `--strict-markers` 时会报警告。

**修复**：
- 将所有 marker 定义移至 `tests/pytest.ini`（追加 `fct`、`elm`）
- 删除 `tests/conftest.py` 中的 `pytest_configure()` 函数（仅作 marker 注册，无其他逻辑）

## Phase 2 — import 弹性增强

**问题**：`sys.path.insert(0, ...)` 无验证。若 `ca/` 模块 import 失败，错误栈指向 fixture 导入而非 conftest 入口，定位困难。

**修复**：
- 添加 `try/except ImportError` 包裹 `import ca`，抛出带路径的 `RuntimeError`
- 使用 `_ca_root` 变量避免重复插入 `sys.path`

## Phase 3 — autouse fixture 解耦

**问题**：`_mock_embed`（autouse）依赖 `ca_engine`，导致每个测试函数（包括纯单元测试如 `test_cache.py`、`test_grade.py`）都触发 `ca_engine` 创建（SQLiteStore + EmbeddingClient + AssemblyCache）。patch 实际作用于 `EmbeddingClient` 类方法，无需实例。

**修复**：
- 移除 `_mock_embed` 的 `ca_engine` 参数
- 效果：单元测试不再创建不必要的 `ca_engine` 实例
- **性能提升**：全量测试 10.11s → 1.15s（-89%）

## Phase 4 — autouse mock 下放到子 conftest

**问题**：`_mock_embed` 和 `_mock_llm` 在根 conftest 中 autouse=True，意味着所有测试（包括纯 SQLite 单元测试）都加载 embed/LLM mock，增加 fixture 图边数（约 30 边）。

**修复**：
- 移除 `_mock_embed`/`_mock_llm` 的 `autouse=True`，改为普通 `@pytest.fixture`
- 创建 `tests/stage/conftest.py` 和 `tests/plugin/conftest.py`，各定义目录级 autouse mock
- 根 conftest 不再导出 mock fixture（仅导出 engine/store fixture）
- 效果：根 conftest 依赖边从 30 降至 < 10

## Phase 5 — 新建 3 个测试文件

### tests/store/test_store_contract.py（12 测试）
覆盖 session_id 契约、get_turn_ca_rows 7 列、max_turn_v5、UPSERT 幂等、close/reopen

### tests/unit/test_embedding.py（4 测试）
拆分自 store/test_embedding.py，覆盖 EmbeddingClient fallback 后端维度检测、不缓存、LRU

### tests/unit/test_lstage.py（10 测试）
覆盖 LStageMixin.reset() 清空 pending、重建 cache、重置 stats、多线程 wait_for_pending 超时

## 验证

```bash
$ python -m pytest tests/ --tb=short -q -p no:cacheprovider
444 passed, 1 skipped in ~2.9s
（较原 416 增加 28 测试，速度从 10.11s 降至 2.9s，改进约 71%）
```
