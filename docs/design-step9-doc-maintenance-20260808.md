# Step 9 文档维护——实现技术方案（开发线，Codex 执行基准）

> 版本 v1.2 | 2026-08-08 | 状态：技术方案（待双线交叉评审）
> 对应：`docs/requirement-step9-doc-maintenance-20260808.md`（需求规格 v1.2）
> 执行方：Codex（实现），tester（测试/验证）
>
> **v1.2 变更**：双目标（本地 docs/ + OVFS 权威源）；新增 9.8 代码→设计方案链接校验。

## 1. 目标与范围

在精炼轮 `ca/refinement.py` 新增 **Step 9 文档维护**（纯 L1 代码、零 LLM、零 token），把人工手工维护的 INDEX/映射/changelog 代码化。核心 = 引用链接对齐。

**双目标**：本地 `plugins/ca_assembler/docs/`（git 管理）+ OV `viking://resources/projects/context-assembler/`（权威源，OVFS 直写）。两侧同构（实测差异仅 OV 的 .abstract/.overview 元数据 + 本地任务书类文档），同一 `DocTree` 抽象双实例执行。

不生成文档正文、不删除文件、不修改运行时路径逻辑代码。默认 dry-run 报告，`--apply` 才落盘（本地与 OV 分别控制）。

## 2. 总体设计

### 2.1 模块划分

新建 `ca/doc_maintenance.py`（DocMaintenance 类，~450 行），refinement.py 的 `_run_refinement_cycle` 末尾新增 Step 9 接入点：

```
ca/doc_maintenance.py     # 新模块：全部 7 子任务（9.1-9.8）
ca/refinement.py          # 修改：_run_refinement_cycle 加 Step 9 调用 + 计数
ca/config.py              # 修改：新增 3 配置项
docs/architecture/14-idle-refinement/14-idle-refinement.md  # 文档：Step 9 登记
tests/unit/test_doc_maintenance.py   # 新增：19 单测（tester 写）
```

### 2.2 数据流

```
DocTree 抽象（root 可为本地 docs/ 或 OVFS 根）
   ├─ 9.1 相对链接对齐 → scan_links → fix_links(apply)
   ├─ 9.2 INDEX 维护 → check_index → apply_index(本地) / report(OV)
   ├─ 9.3 changelog（仅本地）→ validate_changelog / draft_changelog
   ├─ 9.4 trace 映射校验 → check_trace → fix_trace(apply)
   ├─ 9.5 viking:// URI 校验 → check_ov_uris（只读，解析 OVFS）
   ├─ 9.6 wiki/ 残留报告 → report_remnants
   ├─ 9.7 OV 侧对齐 → 同 9.1/9.2/9.4/9.6 以 OVFS 为根执行
   └─ 9.8 代码注释链接校验 → check_code_links（只读，ca/ + scripts/）
```

### 2.3 DocTree 抽象（双目标核心）

```python
@dataclass
class DocTree:
    root: str                  # 本地 docs/ 绝对路径 或 OVFS 根
    is_ov: bool                # True 时写入走 OVFS、INDEX 缺行只报告
    ovfs_root: str = ""        # OVFS 文件系统根（viking:// URI 解析基准）
```

同一扫描/修复函数接受 DocTree 参数，本地与 OV 各自实例化。

### 2.4 目录形态兼容（核心算法）

decisions/ 下双形态并存（决策 43 迁移遗留）：
- 普通目录：`NN-name/NN-name.md`（01-23/28/34-36/42/43）
- `.md/` 后缀目录：`NN-name.md/NN-name.md`（37-41）

**链接解析规则**（resolve 函数，三级）：
1. 链接剥锚点（`#...`）、剥查询；跳过 http/https/mailto/viking:///#
2. 目标存在（`os.path.exists`）→ 有效
3. 目标不存在 → 尝试嵌套补全：`path.md` → `path/path.md`（去掉 .md 当目录查 `dirname/basename`）；`.md/` 后缀目录形态 `path.md` 本身存在（是目录）→ 按有效处理
4. 仍未命中 → **basename 全局查找**：`glob(docs/**/<basename>)`（排除 `.abstract/.overview`），唯一命中 → 建议修复为相对路径；多命中/零命中 → 报告人工决定（category 标 `needs_human`）
5. 仍无 → 断链，报告 + 建议目标

**为什么需要第三级（实测）**：`../decisions/34-idle-refinement.md` 是**双重错误**——层级错（从 architecture/11-l-stage/ 出发 `../decisions` 解析到 `architecture/decisions/`，实际应为 `../../decisions`）+ 平铺错。纯嵌套补全（第 3 级）会 miss，必须 basename 全局查找（→ `../../decisions/34-idle-refinement/34-idle-refinement.md`）。testing/INDEX 的 `../architecture/debug-*.md` 同理（实际文件在同目录）。

**修复规则**（fix_links，apply 模式）：
- 平铺→嵌套：`architecture/01-overview.md` → `architecture/01-overview/01-overview.md`
- `../decisions/NN-name.md` → `../../decisions/NN-name/NN-name.md`（basename 查找修正层级）
- changelog `docs/analysis/...` → `docs/architecture/ca-v5.1-cache-analysis-and-injection-refactor.md`（目标实存映射表，仅 2 处）
- 已删除目标（`docs/wiki/...`）→ 不修，进残留报告（9.6）
- `test-env.md`（本地无此文件，OV 有）/ 多候选 → 不修，报告人工决定

## 3. 关键实现细节

### 3.1 LinkReport 数据结构

```python
@dataclass
class BrokenLink:
    source_file: str      # 含断链的文件（相对 DocTree 根）
    link_text: str        # 原链接
    link_target: str      # 解析后的目标路径
    suggested: str        # 建议修复目标（"" = 不可自动修复）
    category: str         # "relative" | "trace" | "ov_uri" | "wiki_remnant" | "code_link"

@dataclass
class IndexRow:
    number: str           # "01" | "38a"
    name: str             # 链接文本
    link: str             # 链接目标
    version: str
    doc_type: str         # "arch" | "decision"
    desc: str
```

### 3.2 INDEX 维护（9.2）

- 枚举 `NN-*/` 与 `NN-name.md/`（`.md/` 后缀目录）两种形态
- 正则解析 INDEX.md 表格行：`^\| (\*{0,2})(\d+\w*) \| \[([^\]]+)\]\(([^)]+)\) \| (.*?) \| (.*?) \| (.*?) \|$`
- 对照：文件系统有/INDEX 无 → 缺行（本地：draft 补全后 apply；OV：仅报告）
- INDEX 有/文件系统无 → 报告（可能是历史删除，人工确认，不自动删行）

### 3.3 trace/source_files 校验（9.4）

- 解析决策/架构文档 frontmatter：`trace.forward/backward` 的 `- xxx.py`、`source_files: [...]`
- **基准归一化**：`plugins/ca_assembler/xxx` → `xxx`（插件根相对）；保留 `ca/`、`scripts/`、`tests/` 前缀原样
- 校验相对插件根存在；断链 → 报告 + 建议修复
- 实测 5 断链：`plugins/ca_assembler/__init__.py` ×4（14/20/21/22 + 01-overview）

### 3.4 changelog（9.3，仅本地）

- **validate**（A）：版本表列数一致性、日期格式（YYYY-MM-DD）、`\| 版本 \| 日期 \|` 表头；版本表 vs 详细段落版本不同步检测（实测：版本表 v7 vs 详细段 v6.0.5）
- **draft**（B）：`git log --since=<changelog 最后条目日期> --pretty=format:%h|%ad|%s --date=short`
  - 规范前缀（feat/fix/docs/test/chore/refactor + `:`）→ 生成 draft 行 `| vX.Y | date | 阶段 | summary | commit |`
  - 非规范（40%）→ 单独列出「需人工整理」清单，不写入
  - **draft 模式不落盘**——输出到 stdout/日志，人工确认后追加
  - 版本号推断：tag v0.1.0-ca-baseline 存在，但版本历史手工维护——**draft 不自动分配版本号**，标 `_pending_` 由人工填

### 3.5 viking:// URI 校验（9.5，只读）

- 提取本地 docs/ 内 `viking://...` URI → 解析为 OVFS 路径（`viking://resources/projects/context-assembler/X` → `$OVFS_ROOT/X`）→ `os.path.exists` 判定
- **OV 不可达 → graceful**：OVFS 根不存在/权限异常 → 跳过该批，标记 `ov_unreachable=True`，不 fail
- 不写 OV（只读）

### 3.6 wiki/ 残留报告（9.6）

- 扫描指向 `docs/wiki/`、`wiki/` 的链接/裸路径 → 报告 + 建议（指向已删 wiki 的链接应移除或指向 OV 权威源）
- 不自动删、不自动改

### 3.7 OV 侧对齐（9.7）

- 同一 DocTree 以 OVFS 根实例化，执行 9.1（链接修复）/9.2（INDEX 断链修复 + 缺行报告）/9.4（trace）/9.6（残留）
- OV 写入：OVFS 直写（`write_file` 到 OVFS 路径，已验证可写）；OV 自动 reindex
- OV 侧不执行 9.3 changelog（权威源，人工维护）
- 注意 OV 侧 `.abstract.md`/`.overview.md` 是 OV 自动生成文件——**跳过**（不扫描不修改）

### 3.8 代码→设计方案链接校验（9.8，只读）

- 扫描 `ca/*.py` + `scripts/*.py` 注释/文档字符串中的文档引用：
  - OV URI：`viking://resources/projects/context-assembler/...` → OVFS 存在性
  - 本地路径：`docs/{architecture,decisions,testing}/...` → 本地存在性
- **排除**：字符串字面量用于运行时路径逻辑的——`store.py _source_to_ov_uri` 的 `docs/architecture/` 前缀判断、`wiki_to_graph.py` 的 `design/` 兼容解析（通过 grep 定位"出现在函数体内非注释/非 docstring"的行，跳过）
- 实测 18 处引用零断链 → 本轮无修复对象，校验能力防回归

## 4. 依赖与影响

| 影响面 | 内容 |
|--------|------|
| 新增模块 | `ca/doc_maintenance.py`（stdlib only：os/re/datetime/subprocess/urllib；无新依赖） |
| 修改 | `ca/refinement.py`（_run_refinement_cycle + Step 9 计数）、`ca/config.py`（3 配置项） |
| 测试 | `tests/unit/test_doc_maintenance.py`（19 用例，tester 写） |
| 风险 | 断链修复改动 docs/ 内容——**默认 dry-run**，apply 需显式配置；OV 侧写入权威源——默认 dry-run，apply 需显式配置 |

**不引入**：无新第三方依赖。

## 5. 接口契约（测试线对齐基准）

```python
class DocMaintenance:
    def __init__(self, local_root: str = "docs",
                 ovfs_root: str = "/home/i1j/.openviking/data/viking/default/resources",
                 ov_project: str = "projects/context-assembler",
                 apply_local: bool = False, apply_ov: bool = False): ...
    def run(self) -> DocReport: ...                       # 全量执行 7 子任务（本地+OV）

    # 9.1（DocTree 通用）
    def scan_links(self, tree: DocTree) -> list[BrokenLink]: ...
    def fix_links(self, tree: DocTree, broken: list[BrokenLink]) -> int: ...
    # 9.2
    def check_index(self, tree: DocTree) -> tuple[list[IndexRow], list[str]]: ...  # (缺行, 孤儿引用)
    def apply_index(self, tree: DocTree, missing: list[IndexRow]) -> bool: ...     # OV 侧 raise/拒绝
    # 9.3（仅本地）
    def validate_changelog(self) -> list[str]: ...
    def draft_changelog(self) -> tuple[list[str], list[str]]: ...
    # 9.4（DocTree 通用，基准=插件根）
    def check_trace(self, tree: DocTree) -> list[BrokenLink]: ...
    def fix_trace(self, tree: DocTree, broken: list[BrokenLink]) -> int: ...
    # 9.5（只读）
    def check_ov_uris(self) -> tuple[list[BrokenLink], bool]: ...   # (断链, ov_unreachable)
    # 9.6（只读）
    def report_remnants(self, tree: DocTree) -> list[BrokenLink]: ...
    # 9.7：DocTree(ov) 实例跑 9.1/9.2/9.4/9.6
    # 9.8（只读）
    def check_code_links(self, code_roots: list[str]) -> list[BrokenLink]: ...

@dataclass
class DocReport:
    local: DocTreeReport
    ov: DocTreeReport
    changelog_issues: list[str]
    changelog_draft: list[str]
    changelog_manual: list[str]
    ov_unreachable: bool
    code_broken: list[BrokenLink]
    files_modified: list[str]       # apply 模式落盘文件清单（本地+OV）
    llm_calls: int                  # 恒 0（零 LLM 断言）

@dataclass
class DocTreeReport:
    broken_links: list[BrokenLink]
    index_missing: list[IndexRow]
    index_orphans: list[str]
    trace_broken: list[BrokenLink]
    remnants: list[BrokenLink]
```

**错误语义**：所有方法不抛异常（OV 不可达/文件读失败内部消化为报告项）；`check_ov_uris` OVFS 根不存在/scandir 失败 → `([], True)` 永不 raise；`_safe_apply` + `@atomic_write`（tempfile+mv）失败 → rollback + `report.io_errors += 1`，无 partial-write；`run()` 异常 → 上层 refinement 捕获记 aborted（沿用现有模式）。

**评审契约增量（2026-08-08 双线交叉评审关闭）**：

```python
@dataclass
class DocReport:
    total_before_fix: int      # 修复前断链总数（local+ov）
    total_after_fix: int       # 修复后（apply 后重扫）
    io_errors: int             # IO 错误计数（原子写失败/权限）

@dataclass
class BrokenLink:
    was_fixed: bool = False    # apply 后标记
    error_context: str = ""    # IO 错误上下文（如 chmod EACCES）

class DocMaintenance:
    def diff_summary(self) -> str: ...   # 干版 git diff 摘要（集成验收用）
```

## 6. 改动清单

| 文件 | 操作 | 内容 |
|------|------|------|
| `ca/doc_maintenance.py` | 新增 | DocMaintenance + DocTree + 数据结构 + 7 子任务 |
| `ca/refinement.py` | 修改 | Step 9 接入（`if Config.REFINEMENT_DOC_MAINTENANCE`）+ doc_maintained 计数 + refinement_meta 字段 |
| `ca/config.py` | 修改 | `REFINEMENT_DOC_MAINTENANCE=False`、`REFINEMENT_DOC_DRY_RUN=True`、`REFINEMENT_DOC_OV_APPLY=False` |
| `docs/architecture/14-idle-refinement/14-idle-refinement.md` | 修改 | 任务表加 Step 9 行 + 配置表 3 行 |
| `docs/INDEX.md` | 修改 | 架构组件 14 行链接平铺→嵌套（Step 9 首次 apply 的落盘效果，也用于验收） |
| `docs/testing/INDEX.md` | 修改 | 3 处断链修复（apply 效果） |
| `docs/changelog.md` | 修改 | 3 处断链修复（apply 效果） |
| `tests/unit/test_doc_maintenance.py` | 新增 | 19 单测（tester） |
| OV 侧 INDEX.md/architecture 引用 | 修改（apply_ov） | OV 侧断链修复（验收时可复核 OVFS 存在性） |

## 7. 风险与不确定项

1. **apply 落盘 diff 范围**：修复后 git diff 应仅链接行。INDEX.md 决策表 37-41 链接指向 `.md/` 目录（存在）——不误改。验收时核对 diff。
2. **changelog 版本号**：draft 不自动分配版本号（历史手工维护），标 `_pending_`。用户若希望自动分配需扩展。
3. **OV 校验依赖可用性**：OV 不可达时 Step 9 不失败（graceful），报告标记。
4. **断链自动修复的目标映射表**：changelog `docs/analysis/...` 的目标实存映射需人工核定一次（1 处），其余全部算法可推。
5. **幂等性**：修复落盘后二次运行 0 断链（验收标准 8）；INDEX 补全需防重复行（按编号去重）。
6. **OVFS 直写副作用**：OV 自动 reindex 触发 .abstract/.overview 重生成——仅改链接行，内容不变，reindex 结果稳定；不扫描不修改 .abstract/.overview。
7. **代码链接校验的排除逻辑**：运行时路径逻辑识别依赖「字符串在函数体内 vs 注释/docstring」——用 AST 解析精确区分，避免误改功能代码。
