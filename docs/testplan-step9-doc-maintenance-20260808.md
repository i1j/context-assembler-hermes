# Step 9 文档维护——测试技术方案（测试线，tester 产出）

> 版本 v1.0 | 2026-08-08 | 状态：技术方案（待双线交叉评审）
> 对应：`docs/requirement-step9-doc-maintenance-20260808.md`（需求规格 v1.2）
> 实现基准：`docs/design-step9-doc-maintenance-20260808.md`（开发线方案 v1.2）

## 1. 测试目标（覆盖需求点）

| 需求点 | 测试用例 |
|--------|---------|
| 9.1 相对链接对齐 | T1-T6 |
| 9.2 INDEX 维护 | T7 |
| 9.3 changelog | T10-T11 |
| 9.4 trace 映射 | T8-T9 |
| 9.5 viking:// URI | T12 |
| 9.6 wiki/ 残留 | T13 |
| 9.7 OV 双目标 | T15-T16 |
| 9.8 代码链接 | T17 |
| 验收 5 零 LLM | T14 |
| 验收 7 回归 | 集成 §4.2 |
| 验收 8 幂等 | 集成 §4.2 |

## 2. 测试场景

### 2.1 正常场景

| # | 前置 | 步骤 | 预期 |
|---|------|------|------|
| N1 | 真实 docs/（51 断链基线） | `run()` dry-run | 断链报告 51 项（43 INDEX + 3 changelog + 3 testing + 2 architecture）；可自动修复 **49** 项 suggested 非空；2 项 needs_human（changelog wiki 残留 + testing test-env 本地无文件）suggested="" |
| N2 | 真实 docs/ + OVFS | `run()` dry-run 双目标 | local.broken_links=51、ov.broken_links=48；files_modified=[]（dry-run 不落盘） |
| N3 | 真实 docs/ | `apply_local=True` 后重跑 | 断链 0；files_modified 含 INDEX.md/testing/INDEX.md/changelog.md；git diff 仅链接行 |
| N4 | changelog draft | `draft_changelog()` | draft 行含规范 commit；非规范 commit 在 manual 清单；版本号 `_pending_` |
| N5 | OV 可达 | `check_ov_uris()` | 返回 ov_unreachable=False；断链列表与 OVFS 实测一致 |
| N6 | 代码注释链接 | `check_code_links(['ca','scripts'])` | 18 处引用全部通过；`store.py` 无修改 |

### 2.2 边界场景

| # | 前置 | 步骤 | 预期 |
|---|------|------|------|
| B1 | 链接带锚点 | `scan_links` | `doc.md#section` 按 doc.md 解析，锚点忽略 |
| B2 | 外部链接 | `scan_links` | http/https/mailto/viking:// 跳过 |
| B3 | `.md/` 后缀目录 | `scan_links` | `decisions/37-reality-restructure.md`（目录）判有效，不误报 |
| B4 | 幂等 | apply 后第二次 `run()` | 0 断链、0 修改、diff 为空 |
| B5 | INDEX 重复行 | 补全后重跑 | 按编号去重，不产生重复行 |
| B6 | trace plugins 前缀 | `check_trace` | `plugins/ca_assembler/__init__.py` 归一化 `__init__.py` 判存在 |
| B7 | OV 不可达 | 模拟 OVFS 根不存在 | ov_unreachable=True；run() 不抛异常；报告标记 |
| B8 | changelog 版本不同步 | `validate_changelog` | 版本表 v7 vs 详细段 v6.0.5 → 报告不同步 |
| B9 | 中文路径/文件名 | `scan_links` | 中文文件名链接正常解析 |

### 2.3 异常场景

| # | 前置 | 步骤 | 预期 |
|---|------|------|------|
| E1 | 文件读失败 | 目标文件权限错误 | 内部消化为报告项，不抛异常 |
| E2 | git log 无输出 | mock 空 git log | draft 空列表，manual 空，不报错 |
| E3 | INDEX.md 无表格 | 空 INDEX | check_index 返回空缺行，不抛异常 |

## 3. 测试级别与方法

| 级别 | 方法 | 内容 |
|------|------|------|
| 单元（tests/unit/test_doc_maintenance.py） | pytest + tmp_path fixtures | T1-T19：19 用例（需求规格 §4.1） |
| 集成（验证诊断阶段手工） | 真实 docs/ + OVFS 实跑 | N1-N6 真实数据核对（51/48 断链基线） |
| 回归 | 全量 pytest | 795 collected 基线不降 |
| 幂等 | apply 后重跑 + git diff | diff 为空 |

## 4. 测试数据与 mock 策略

| 依赖 | mock 策略 |
|------|----------|
| 文件系统 | `tmp_path` 构造迷你 docs/ 树（含普通目录 + `.md/` 后缀目录双形态 + 断链样本） |
| git | `subprocess.run` mock（`git log` 返回固定 3 条：2 规范 + 1 非规范） |
| OVFS | 测试用独立 tmp 目录模拟 OVFS 根（不触碰真实 OV）；OV 不可达 = tmp 根删除 |
| LLM | 断言 `llm_calls == 0`（DocReport 字段），不需要真 mock |
| 真实数据（集成） | 真实 docs/ + 真实 OVFS（只读 dry-run 不落盘） |

**关键设计**：OVFS 根通过构造器参数注入（`ovfs_root`），测试传 tmp 目录，与真实 OV 隔离。所有扫描/修复函数以 DocTree 为参数——单测只测本地 DocTree + tmp OV DocTree，不触碰真实环境。

## 5. 环境要求

- pytest（现有环境）
- 不需要模型/端口/DB（纯文件操作 + git 只读）
- 集成测试需要：真实 docs/（已在插件目录）+ OVFS 可读（`/home/i1j/.openviking/...`）
- 测试不写 OV（apply_ov 单测用 tmp 目录）

## 6. 风险与盲区

| 风险 | 说明 | 缓解 |
|------|------|------|
| AST 排除逻辑误判 | 9.8 区分「注释引用 vs 运行时字符串」依赖 AST——复杂表达式（f-string/拼接）可能漏检 | 单测覆盖 store.py `_source_to_ov_uri` 实际行（T17 断言未被修改）；集成核对 18 处引用 |
| 真实 OV 写入验证 | 单测不碰真实 OV，apply_ov 落盘只在验证诊断阶段人工执行 | 集成测试显式分两步：dry-run 核对 → 人工确认后 apply_ov + OVFS 存在性复核 |
| 断链基线漂移 | 51/48 是本次实测值，代码构建期间 docs/ 可能变化 | 集成测试以「实跑扫描数」断言，不用硬编码值 |
| 中文文件名 | 正则/路径处理对中文需验证 | B9 用例覆盖 |
| changelog 版本号 | draft 不自动分配（`_pending_`） | 不测版本号具体值，只测占位存在 |

## 7. 已知盲区（接受为已知风险）

- OV 侧 reindex 副作用（.abstract/.overview 重生成）无法在单测断言——集成阶段观察
- 断链修复的目标映射表（changelog `docs/analysis/` 1 处）依赖人工核定——N3 集成时人工确认修复结果
