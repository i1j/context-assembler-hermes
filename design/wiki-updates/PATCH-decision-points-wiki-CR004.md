# decision-points-wiki.md 更新说明 (CR-004)

> CR-004: `get_turn_ca_rows` 列扩展断链 + Hdl 巧合正确性 (2026-06-19)

---

## 更新 1：在「事故与审查修复」根分支末尾新增 CR-004

**位置:** `### CR-003` 之后

**新增内容:**

```markdown
### CR-004: `get_turn_ca_rows` 列扩展断链 + Hdl 巧合正确性 (2026-06-19)

Parent: R-000

**Bug:** `get_turn_ca_rows` 只返回 Fct 不返回 Hdl → REL/FAR tool/thought/fin 行替换全部用了 `Fct[:150]` 而非 Hdl。数据详情见下方。

**Root cause: 跨文件接力断链**
- `store.py:1365-1374` — `get_turn_ca_rows` SELECT 仅 `Fct` 列，没拉 `Hdl`
- `__init__.py:399-407` — 队列构建只用 `fct` 值填入 `ca_tools`/`ca_thoughts`/`ca_fins`
- `__init__.py:485-491` — `row_grade == Grade.HDL → content = (ca_tools[tj] or "")[:150]` → 存的是 Fct 值被截断

**两轮修复 (2026-06-19):**
- Round 1: 加 Hdl + 存 `(fct, hdl)` 元组 → 替换时 if/else 选列（打补丁式）
- Round 2（设计重构）: 改为**等级优先→按需选列→只读需要的列**
  1. `get_turn_ca_rows` 加 `content`(Elm) + `Hdl` 列 → 7 列返回
  2. 队列构建按 `row_grade` 选列: ELM 不填队列 / FCT 仅 Fct / HDL 仅 Hdl[:150] / FAR 清空
  3. 替换循环直接 pop，无 if/else
  4. `topic_manager.py` 位置索引相应 `[4]→[5]`

**Affected:**
- `_simple_mutation_mode_v5` — 全量路径（全线 thought/tool/fin REL 分支受影响）
- `_incremental_mutation` — 增量路径（同上）
- `_extract_turn_fct()` / `_init_topic_data()` — 索引偏移 `[4]→[5]`
- 文件: `ca/store.py`, `ca/__init__.py`, `topic_manager.py`
- 修复文件: 3 Python + 4 测试文件

**测试漏网根因 — 巧合正确性 (coincidental correctness):**
- `_setup_data` 全家族从不传 `hdl_text` → DB 的 Hdl 列永远为 NULL
- 旧 HDL 分支实际执行 `Fct[:150]` → 测试断言如 `"Fct_FAR" in content[:50]` 照过不误
- 两个系统性模式:
  - **列扩展无断言审计**: `get_turn_ca_rows` 返回列 5→6→7 扩展时只改解包行，从不加断言
  - **函数命名偏见**: `update_fin_fct_v5` / `_update_fct_v5` 名字只提 Fct，Hdl 作为第 4 参数隐在后面 → 测试跟着函数名只验 Fct

**修复的 6 个测试:**
  - `test_store_v5.py:test_returns_seq_role_finish_tools_fct` — 补 hdl_text + hdl/content 断言
  - `test_store_v5.py:test_updates_fin_row_fct_and_hdl` — 补 SELECT Hdl 断言
  - `test_store_v5.py:test_does_not_affect_other_turns` — 补 Hdl 隔离断言
  - `test_store_v5.py:test_updates_only_latest_assistant_fin` — 补新旧 fin 的 Hdl 断言
  - `test_e_stage.py:test_updates_fin_row` — 补 Hdl 写入验证
  - `test_a_stage_topic_aware.py` 全组 13 条 — 补 hdl_text 数据 + Hdl 断言

**F-stage 对比:** 已有 `test_llm_path_updates_hdl` 验证 Hdl 写入（直查 `SELECT Hdl`），未受影响
  — 因该测试是**后来专项加的**，目标就是验证 Hdl 落盘，不属于列扩展审计漏网。

**Evidence:** `ca/store.py` `get_turn_ca_rows`; `ca/__init__.py` `_simple_mutation_mode_v5` + `_incremental_mutation`;
  调试报告: `design/wiki-updates/dump-wiki-verify-hdl-bug-fix.md`
```

---

## 更新 2：在 TP-002 的替换映射表下方加 Fix 脚注

**位置:** 替换映射表下方，约第 1050 行

**在 `centroid=None → REL fallback（embed 失败保护）` 之后新增:**

```markdown
**⚠️ 2026-06-19 Fix:** `get_turn_ca_rows` 原 SELECT 只返回 `Fct` 列，未包含 `Hdl` 和 `content`(Elm)，
  导致 REL/FAR 等级的 thought/tool/fin 行替换时实际使用 `Fct[:150]` 而非 `Hdl`。已修改为 7 列返回
  `(seq, role, finish_reason, tool_calls_json, content, Fct, Hdl)`，替换逻辑改为等级优先→按需选列。
  详见 [[CR-004]]。
```

---

## 更新 3：`ca-test-index` / `INDEX.md` 的测试缺口登记表新增 GAP-11、GAP-12

**OV 资源路径:**
- `viking://resources/ca-test-index/ca-test-index.md`
- `viking://resources/INDEX/INDEX.md` (内容相同)

**新增缺口 (追加到现有 GAP 表末尾):**

```markdown
| GAP-11 | S-001, TP-002 | **列扩展无断言审计**: `get_turn_ca_rows` 返回列 5→6→7 扩展时只改解包行，新列（Hdl、content）零断言。
  此类问题在列扩展时无法被已有测试体系发现。 | ⚠️ 已修复 | CR-004 |
| GAP-12 | TP-002 | **巧合正确性 (coincidental correctness)**: `_setup_data` 测试数据工厂从不构造 `hdl_text`，
  Hdl 列永远为 NULL → REL/FAR 分支的测试通过不是因为 Hdl 分支正确，而是因为 Fct[:150] 恰好看起来像截断。
  漏洞模式：测试数据构造缺少列 ↔ 生产数据有该列 | ⚠️ 已修复 | CR-004 |
```
