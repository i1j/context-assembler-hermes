# CA 插件精炼轮 Step 9 文档维护——实现任务书（Codex 执行，2026-08-08）

> 前置文档（必读，按序）：
> 1. `docs/requirement-step9-doc-maintenance-20260808.md`（需求规格 v1.2，含验收标准 9 条）
> 2. `docs/design-step9-doc-maintenance-20260808.md`（实现技术方案 v1.2——**本任务书以此为准**）
> 3. `docs/testplan-step9-doc-maintenance-20260808.md`（测试技术方案——tester 已写好测试，你实现接口对齐）
> 4. `docs/decisions/43-ov-wiki-mirror-sync/43-ov-wiki-mirror-sync.md`（决策背景：OV 权威源/本地工作副本）
> 5. `docs/architecture/14-idle-refinement/14-idle-refinement.md`（精炼轮现状，Step 0-8.5 接入模式）
>
> 验收标准：测试基线（**795 collected**，embed 服务正常时 793 passed / 1 skipped / 1 xfailed）+ tester 提供的 `tests/unit/test_doc_maintenance.py`（19 用例全绿）。

## 0. 部署快照

- 插件路径：`/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/`（git worktree，branch `master`）
- 测试命令：`/usr/bin/python3 -m pytest tests/unit/test_doc_maintenance.py -q -p no:cacheprovider`
- OVFS 根：`/home/i1j/.openviking/data/viking/default/resources/`（CA 项目 = `projects/context-assembler/`）
- 实测断链基线（本任务书编写时）：本地 docs/ **51** 断链（INDEX 43 + changelog 3 + testing/INDEX 3 + architecture 2）；OV 侧 **48** 断链

## 1. 实现范围（新增模块为主，勿动既有逻辑）

| 文件 | 操作 | 内容 |
|------|------|------|
| `ca/doc_maintenance.py` | **新增** | `DocMaintenance` + `DocTree` + `BrokenLink`/`IndexRow`/`DocReport`/`DocTreeReport` 数据结构 + 7 子任务（9.1-9.8）。**stdlib only**（os/re/datetime/subprocess/ast，无第三方依赖） |
| `ca/refinement.py` | 修改 | `_run_refinement_cycle` 末尾加 Step 9 接入（`if Config.REFINEMENT_DOC_MAINTENANCE:`）+ `doc_maintained` 计数 + `refinement_meta` 字段 |
| `ca/config.py` | 修改 | 3 新配置项（见 §4） |
| `docs/architecture/14-idle-refinement/14-idle-refinement.md` | 修改 | 任务表加 Step 9 行 + 配置表 3 行 |
| `docs/INDEX.md` | 修改 | 仅链接行平铺→嵌套修复（Step 9 apply 的效果，也是验收依据） |
| `docs/testing/INDEX.md` | 修改 | 3 处断链修复 |
| `docs/changelog.md` | 修改 | 3 处断链修复（勿动历史条目内容） |

**禁止**：修改 `ca/store.py`（`_source_to_ov_uri` 是运行时逻辑，9.8 只读校验不修改它）；修改 `scripts/wiki_to_graph.py`；改测试文件。

## 2. 核心算法（按方案 §2.4/§3 实现）

### 2.1 链接解析 resolve（三级，兼容双形态）
1. 剥锚点/查询；跳过 http/https/mailto/viking:///#
2. `os.path.exists` 直接命中 → 有效
3. 未命中 → 嵌套补全 `path.md` → `path/path.md`；`.md/` 后缀目录（`path.md` 是目录）→ 有效
4. 仍未命中 → **basename 全局查找** `glob(**/<basename>)`（排除 `.abstract/.overview`）：唯一命中 → suggested=相对路径；多/零命中 → needs_human
5. 仍无 → 断链 + suggested

### 2.2 修复规则 fix_links（apply）
- 平铺→嵌套：`architecture/01-overview.md` → `architecture/01-overview/01-overview.md`
- 双重错误：`../decisions/NN-name.md` → `../../decisions/NN-name/NN-name.md`（basename 查找修正层级）
- changelog 特例（映射表硬编码，仅 2 处）：`docs/analysis/ca-v5.1-...md` → `docs/architecture/ca-v5.1-cache-analysis-and-injection-refactor.md`
- `docs/wiki/...` / `test-env.md`（本地无） / 多候选 → 不修，进报告（needs_human/wiki_remnant）

### 2.3 INDEX 维护（9.2）
- 正则解析表格行：`^\| (\*{0,2})(\d+\w*) \| \[([^\]]+)\]\(([^)]+)\) \| (.*?) \| (.*?) \| (.*?) \|$`
- 枚举 `NN-*/` + `NN-name.md/` 双形态 → 缺行补全（本地 apply；OV 只报告）

### 2.4 trace 校验（9.4）
- frontmatter `trace.forward/backward` + `source_files` → 基准归一化（`plugins/ca_assembler/xxx` → `xxx`）→ 相对插件根存在性

### 2.5 changelog（9.3，仅本地）
- validate：表头/列数/日期格式/版本表 vs 详细段不同步
- draft：`git log --since=<最后条目日期> --pretty=format:%h|%ad|%s --date=short`；规范前缀进 draft（版本号 `_pending_`），非规范进 manual

### 2.6 viking:// URI 校验（9.5，只读）
- `viking://resources/projects/context-assembler/X` → `$OVFS_ROOT/X` → exists 判定；OVFS 根不可达 → `ov_unreachable=True` 不抛异常

### 2.7 OV 侧（9.7）
- `DocTree(root=OVFS 项目根, is_ov=True)` 实例跑 9.1/9.2/9.4/9.6
- **跳过 `.abstract.md`/`.overview.md`**（OV 自动生成文件）
- apply_ov 默认 False；OV 侧 INDEX 缺行只报告不补

### 2.8 代码链接校验（9.8，只读）
- `ca/*.py` + `scripts/*.py` 中：**用 ast 解析**——docstring/注释中的 `viking://resources/projects/context-assembler/` 与 `docs/{architecture,decisions,testing}/` 引用 → OVFS/本地存在性校验
- 函数体内运行时字符串（如 store.py `_source_to_ov_uri` 的 `docs/architecture/` 前缀）**排除**——ast 判断字符串节点是否在 FunctionDef 语句体而非 docstring
- 实测 18 处引用零断链 → 校验报告即可

## 3. 接口契约（严格对齐方案 §5，tester 测试已按此写）

```python
class DocMaintenance:
    def __init__(self, local_root="docs",
                 ovfs_root="/home/i1j/.openviking/data/viking/default/resources",
                 ov_project="projects/context-assembler",
                 apply_local=False, apply_ov=False): ...
    def run(self) -> DocReport: ...
    def scan_links(self, tree) -> list[BrokenLink]: ...
    def fix_links(self, tree, broken) -> int: ...
    def check_index(self, tree) -> tuple[list[IndexRow], list[str]]: ...
    def apply_index(self, tree, missing) -> bool: ...
    def validate_changelog(self) -> list[str]: ...
    def draft_changelog(self) -> tuple[list[str], list[str]]: ...
    def check_trace(self, tree) -> list[BrokenLink]: ...
    def fix_trace(self, tree, broken) -> int: ...
    def check_ov_uris(self) -> tuple[list[BrokenLink], bool]: ...
    def report_remnants(self, tree) -> list[BrokenLink]: ...
    def check_code_links(self, code_roots) -> list[BrokenLink]: ...
```

- `DocReport`：`local: DocTreeReport`、`ov: DocTreeReport`、`total_before_fix/after_fix: int`、`io_errors: int`、`changelog_issues/draft/manual`、`ov_unreachable: bool`、`code_broken`、`files_modified`、`llm_calls=0`
- `DocTreeReport`：`broken_links/index_missing/index_orphans/trace_broken/remnants`
- `BrokenLink`：`was_fixed: bool=False`、`error_context: str=""`
- `diff_summary()` → 干版 git diff 摘要字符串（集成验收用）
- **实例属性（测试依赖）**：`self.local` / `self.ov` 为 DocTree 实例（`local_root`/`ov_project` 构造）；`self.apply_local` / `self.apply_ov` / `self.ovfs_root` 为构造参数同名字段（测试会直接赋值 `m.apply_ov = True` 覆盖）
- **错误语义：所有方法不抛异常**（读失败/OV 不可达内部消化为报告项）；`check_ov_uris` OVFS 根不存在/scandir 失败 → `([], True)` 永不 raise；apply 写文件用 `@atomic_write`（tempfile+mv），失败 → rollback + `io_errors += 1`，无 partial-write

## 4. 配置项（ca/config.py，参照既有 REFINEMENT_* 模式）

```python
REFINEMENT_DOC_MAINTENANCE: ClassVar[bool] = os.getenv("CA_REFINEMENT_DOC_MAINTENANCE", "0") == "1"  # Step 9 总开关，默认关
REFINEMENT_DOC_DRY_RUN: ClassVar[bool] = os.getenv("CA_REFINEMENT_DOC_DRY_RUN", "1") == "1"          # 默认 dry-run
REFINEMENT_DOC_OV_APPLY: ClassVar[bool] = os.getenv("CA_REFINEMENT_DOC_OV_APPLY", "0") == "1"        # OV 侧落盘，默认关
```

Step 9 接入 refinement.py（在 fact_linking 之后、写 meta 之前）：

```python
# Step 9: 文档维护（决策 43 §4.1：链接对齐 L1 代码可精确做）
if Config.REFINEMENT_DOC_MAINTENANCE:
    tasks_run.append("doc_maintenance")
    try:
        from ca.doc_maintenance import DocMaintenance
        dm = DocMaintenance(apply_local=not Config.REFINEMENT_DOC_DRY_RUN,
                            apply_ov=Config.REFINEMENT_DOC_OV_APPLY)
        report = dm.run()
        doc_maintained = len(report.files_modified)
    except Exception as exc:
        logger.warning("[CA_L4] Step 9 doc maintenance failed: %s", exc)
        doc_maintained = -1
```

## 5. 测试注意（tester 已就位）

- `tests/unit/test_doc_maintenance.py`（19 用例）由 tester 提供——**不要修改测试**，实现按接口契约对齐即可
- 测试用 `tmp_path` 构造迷你 docs 树 + tmp OVFS 目录（构造器注入 `ovfs_root`），**不触碰真实 OV**
- 验收跑：`/usr/bin/python3 -m pytest tests/unit/test_doc_maintenance.py tests/unit/test_refinement_health.py -q -p no:cacheprovider`
- 全量回归：`/usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider`（基线 795 collected，embed 2 failed 为环境性）

## 6. 完成定义（DoD）

1. 新增 `ca/doc_maintenance.py`（7 子任务完整实现，stdlib only）
2. `refinement.py` Step 9 接入 + `config.py` 3 配置项
3. 19 单测全绿（tester 提供）
4. 全量测试基线不降
5. docs 三处链接修复已 apply（INDEX/testing INDEX/changelog）
6. commit message 用 `feat, test: 精炼轮 Step 9 文档维护（决策 43 §4.1 链接对齐）`
