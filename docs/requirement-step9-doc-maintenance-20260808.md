# 精炼轮 Step 9：文档引用链接对齐 + INDEX/映射/changelog 维护（需求规格 + 测试需求）

> 版本 v1.1 | 2026-08-08 | 状态：需求定稿（用户三轮裁决确认：范围扩展为本地+OV 两侧）
> 来源：决策 43 §4.1「文档引用链接对齐 ✅ 高可行——L1 结构化维护，代码精确做、零 LLM；把人工手工维护的 INDEX/映射代码化」
> 前置：`docs/decisions/43-ov-wiki-mirror-sync/43-ov-wiki-mirror-sync.md`、`docs/architecture/14-idle-refinement/14-idle-refinement.md`
> 执行方：Codex（业务编码）+ tester（测试/验证）。tester 不写业务代码。
>
> **v1.1 变更（用户裁决）**：作用范围从「仅本地 docs/」扩展为 **「本地 docs/ + OV 权威源两侧对齐」**——实测 OV 侧与本地完全同构（48/58 链接断链，同一根因），同一扫描逻辑双目标执行；OV 写入走 OVFS 直写（已验证可写）。

## 1. 需求描述（做什么）

**精炼轮新增 Step 9（文档维护，纯 L1 代码、零 LLM、零 token）**：把用户手工维护的 INDEX/映射/changelog 工作代码化。核心 = **引用链接对齐**（文档互引/INDEX 链接/决策↔代码映射，修复断链）。

**双目标**：本地 `plugins/ca_assembler/docs/`（git 管理）+ OV `viking://resources/projects/context-assembler/`（权威源，OVFS 直写）。两侧同构，同一扫描逻辑，各自生成报告/各自落盘。

### 1.1 子任务清单（用户裁决全纳入）

| # | 子任务 | 类型 | 实测现状 | 处置 |
|---|--------|------|----------|------|
| 9.1 | **相对链接对齐**（核心） | 扫描+修复 | docs/ 60 链接 **51 断链**（INDEX.md 43 + changelog 3 + testing/INDEX 3 + 11-l-stage/14-idle-refinement 各 1） | 断链报告 + 可精确修复的自动修复 |
| 9.2 | **INDEX 维护** | 扫描+补全 | INDEX.md 架构表/决策表链接全平铺断链；新决策文档缺行 | 断链修复 + 新文档缺行补全（编号/链接精确生成，说明列留空待人工） |
| 9.3 | **changelog 维护** | 扫描+追加 | changelog 3 断链；120 commits / 60% 规范前缀 / 1 tag | A：断链修复 + 格式结构校验；B：git log 结构化追加（draft 模式，人工确认后落盘） |
| 9.4 | **trace/source_files 映射校验** | 扫描+修复 | 39 文档 / 41 路径 / **5 断链**（根因：路径基准混用 `plugins/ca_assembler/__init__.py` vs `ca/theme.py`） | 统一基准（插件根）+ 自动修复 + 全量校验 |
| 9.5 | **viking:// URI 引用校验** | 只读扫描 | docs/ 内 OV URI 引用（本地侧） | 解析到 OVFS 路径存在性校验（本地侧引用 OV 的 URI → 本地报告；OV 自身不引用 viking://） |
| 9.6 | **wiki/ 残留引用报告** | 只读扫描 | changelog `docs/wiki/decisions/`（wiki 已删） | 报告 + 处置建议，**不自动删**（宁缺勿错） |
| 9.7 | **OV 侧对齐（v1.1 新增）** | 扫描+修复 | OV 侧 48/58 断链（INDEX 43 + testing 2 + changelog 1 + architecture 2），与本地同构 | 同一扫描逻辑以 OVFS 为根执行 9.1/9.2/9.4/9.6；OV 写入走 OVFS 直写（已验证）；OV 侧不自动补 INDEX 缺行（权威源人工确认） |
| 9.8 | **代码→设计方案链接校验（v1.2 新增）** | 只读扫描 | 代码注释/文档字符串中 18 处文档引用（OV URI 7 + 本地路径 11）——**实测零断链**（2026-08-07 topic_manager.py 手工修正已覆盖） | 校验代码注释中的 OV URI/本地 docs 路径存在性（断链报告，本次无修复对象）；**运行时路径逻辑（store.py `_source_to_ov_uri` 前缀判断、wiki_to_graph design/ 兼容解析）明确排除**——是功能代码非文档链接 |

### 1.2 目录形态兼容（结构性根因）

decisions/ 下两种形态并存，对齐算法必须兼容：
- 普通目录：`NN-name/NN-name.md`（01-23/28/34-36/42/43）
- **`.md/` 后缀目录**：`NN-name.md/NN-name.md`（37-41，OV 迁移 WebDAV MOVE 遗留）

链接修复规则：`architecture/01-overview.md` → `architecture/01-overview/01-overview.md`（平铺→嵌套补全）；`decisions/34-idle-refinement.md` → `decisions/34-idle-refinement/34-idle-refinement.md`。

## 2. 验收标准（怎样算对）

1. **断链归零（本地+OV）**：执行 Step 9 后本地 docs/ 与 OV 侧全部**可自动修复**相对链接解析成功（0 断链）；不可自动修复项（目标确已删除 / 本地无此文件 / 多候选 / 语义问题）进入报告清单（category=needs_human/wiki_remnant），不静默。
2. **映射校验通过**：全部决策/架构文档 trace/source_files 引用代码文件存在（基准=插件根 `ca/`、`scripts/`、`tests/`；`plugins/ca_assembler/` 前缀自动归一化）。
3. **INDEX 自洽**：本地 INDEX.md 表格与实际文件系统双向对照——无断链、无缺行（新决策文档已登记）；OV 侧 INDEX 断链修复，缺行仅报告不自动补。
4. **changelog 追加正确**：git log draft 追加条目含版本/日期/变更摘要/commit hash；非规范 commit（40%）跳过并说明；追加前人工确认（`--dry-run` 输出 draft）。
5. **零 LLM**：Step 9 全程无 LLM 调用（代码精确可做，语义内容不生成）。
6. **OV 只读/可控**：viking:// 校验只读；OV 侧修复走 OVFS 直写（已验证可写），默认 dry-run，apply 需显式配置；OV 不可达时 Step 不失败（graceful 降级）。
7. **回归安全**：全量测试基线不降（当前 795 collected；embed 服务正常时 793 passed / 1 skipped / 1 xfailed）。
8. **幂等**：连续两次执行无新增变更（第二次 diff 为空，修复已落盘后不重复改）。
9. **代码链接校验**：代码注释中文档引用零断链；运行时路径逻辑（`_source_to_ov_uri` 前缀判断等）不受影响（不修改）。

## 3. 不做（边界）

- ❌ 不生成文档正文（LLM 语义内容——精度不可靠，决策 43 用户原则）
- ❌ 不写 OV（OV 为权威源，同步走 code-ov-agents-sync 流程）
- ❌ 不自动删除文件/目录（含 wiki/ 残留、孤儿文档——残留清理/结构修复需人工确认）
- ❌ 不修改 INDEX.md 的人工列（说明/版本/状态——语义字段人工维护）
- ❌ 不自动修复正文裸路径引用（非 markdown 链接格式，误伤风险，仅报告）
- ❌ 不触碰 changelog 历史条目内容（仅修链接 + 结构校验 + 尾部追加）
- ❌ 不修改运行时路径逻辑代码（`_source_to_ov_uri` 前缀判断、wiki_to_graph `design/` 兼容解析——功能代码非文档链接）
- ❌ OV 侧不自动补 INDEX 缺行/不自动修 changelog（权威源变更需人工确认，Step 9 只报告）
- ❌ 不写 OV 语义索引/不触发 OV 重建（OVFS 直写仅改链接行，reindex 由 OV 自动处理）

## 4. 测试需求（怎样算对的检验场景）

### 4.1 单元测试（新建 tests/unit/test_doc_maintenance.py）

| 用例 | 场景 | 断言 |
|------|------|------|
| T1 | 平铺链接 `architecture/01-overview.md` 断链检测 | 报告断链 + 建议目标 `architecture/01-overview/01-overview.md` |
| T2 | `.md/` 后缀目录链接 `decisions/37-reality-restructure.md`（指向目录） | 判定为有效（目录存在），不误报 |
| T3 | 锚点链接 `doc.md#section` | 按 `doc.md` 解析，忽略锚点 |
| T4 | 外部链接 http/https/mailto | 跳过不检测 |
| T5 | 断链自动修复 | 修复后 `os.path.exists` 通过，文件内容链接更新正确 |
| T6 | 重复执行幂等 | 第二次扫描 0 断链 0 修改 |
| T7 | INDEX 缺行补全 | 新增决策文档 `44-test.md/44-test.md` 后运行 → INDEX.md 出现 44 行（链接+编号），说明列为空 |
| T8 | trace 基准归一化 | `plugins/ca_assembler/__init__.py` → `ca/__init__.py` 判定存在 |
| T9 | trace 断链修复 | 断链路径修复后全部 resolve |
| T10 | changelog git draft | mock git log → 生成 draft 条目（版本/日期/摘要/hash），非规范 commit 被标注跳过 |
| T11 | changelog 结构校验 | 版本表列数不一致/日期格式错误 → 报告 |
| T12 | viking:// URI 校验 | mock OVFS/WebDAV → 200 有效 / 404 断链 / 不可达 graceful |
| T13 | wiki/ 残留引用 | 报告 + 不产生任何删除操作 |
| T14 | 零 LLM | mock LLM 调用器 → 断言 Step 9 全程未调用 |
| T15 | 双目标（本地+OV） | 同一扫描逻辑对本地 docs/ 与 OVFS 根执行 → 两侧断链报告独立、修复独立落盘 |
| T16 | OV 侧 INDEX | OV INDEX.md 平铺链接 → 修复为嵌套（OVFS 直写后存在性通过）；缺行仅报告不自动补 |
| T17 | 代码注释链接校验（9.8） | 扫描 ca/ + scripts/ 注释中文档引用 → 存在性校验；`_source_to_ov_uri` 前缀判断行未被触碰（diff 无该文件变更） |
| T18 | 双重错误链接（v1.2 补） | `../decisions/34-idle-refinement.md`（层级+平铺双错）→ basename 全局查找 → 建议 `../../decisions/34-idle-refinement/34-idle-refinement.md` |
| T19 | basename 多候选 | 同名文件多处命中 → category=needs_human，不自动修 |

### 4.2 集成测试

- 在真实 `docs/` 上运行 Step 9 全量扫描（`--dry-run` 模式）→ 断链报告与实测基线（51 断链）吻合
- `--apply` 修复后断链归零；git diff 仅涉及链接行/INDEX 行
- 全量 pytest 基线回归

## 5. 配置项（新增）

| 配置 | 默认 | 说明 |
|------|------|------|
| `REFINEMENT_DOC_MAINTENANCE` | False | Step 9 总开关（同其他 Step 默认关闭） |
| `REFINEMENT_DOC_DRY_RUN` | True | 默认只报告不落盘（安全默认，人工确认后 `--apply`） |

## 6. 相关资源

- 决策 43：`docs/decisions/43-ov-wiki-mirror-sync/43-ov-wiki-mirror-sync.md`（链接对齐可行性 + 已执行实例）
- 精炼轮架构：`docs/architecture/14-idle-refinement/14-idle-refinement.md`（Step 0-8.5 现状）
- 代码：`ca/refinement.py`（_run_refinement_cycle，Step 9 接入点）、`scripts/wiki_to_graph.py`（frontmatter trace 解析参考）、`ca/store.py:1646 _source_to_ov_uri`（OV URI 映射参考）
- 测试基线：795 collected（2026-08-08 实测）
