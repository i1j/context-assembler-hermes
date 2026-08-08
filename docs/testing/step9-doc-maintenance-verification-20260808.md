# Step 9 文档维护——验证报告（链接对齐，决策 43 §4.1）

> 2026-08-08 | 状态：已交付（commit 69a62a2）
> 对应实现：`ca/doc_maintenance.py`（DocMaintenance 类，纯 stdlib 零 LLM）
> 决策依据：决策 43 §4.1「文档引用链接对齐 ✅ 高可行——L1 结构化维护」

## 1. 三层链接解析算法（核心经验）

链接目标解析按三级降级，**必须三级**（实证）：

| 级 | 规则 | 覆盖场景 |
|----|------|---------|
| 1 | 直接 `os.path.exists` | 正常链接 |
| 2 | 嵌套补全 `path.md` → `path/path.md` | 平铺→嵌套目录形态 |
| 3 | **basename 全局查找**（唯一命中） | **双重错误链接**：层级错 + 平铺错 |

**为什么需要第 3 级（实测）**：`../decisions/34-idle-refinement.md` 从 `architecture/11-l-stage/` 出发，`../decisions` 解析到 `architecture/decisions/`（应为 `../../decisions`）——纯嵌套补全 miss。testing/INDEX 的 `../architecture/debug-*.md` 同理（实际文件在同目录）。basename 唯一命中 → 修正层级；多/零命中 → needs_human 不自动修。

**`.md/` 后缀目录形态**（决策 43 迁移遗留）：`decisions/37-reality-restructure.md/` 是目录，链接指向它**有效**——第 1 级 exists 直接命中，不误报不误改（实测 37-41 六处正确保留）。

## 2. 实测结果

| 侧 | 修复前断链 | 可自动修复 | needs_human | 修复后 |
|----|-----------|-----------|-------------|--------|
| 本地 docs/ | 51 | 49（42 嵌套 + 7 basename） | 2（changelog wiki 残留 + testing test-env 本地无） | 0 自动项 |
| OV 权威源 | 48 | 48 | 0 | 0 |

- 幂等：二次运行 0 修改（修复落盘后重扫 0 断链）
- 零 LLM：`llm_calls` 恒 0（DocReport 字段断言）
- 全量回归：860 passed / 1 skipped / 1 xfailed（基线无回归）

## 3. 子任务清单（9.1-9.8）

| # | 任务 | 实现 |
|---|------|------|
| 9.1 | 相对链接对齐 | scan_links/fix_links（三级 resolve） |
| 9.2 | INDEX 维护 | check_index/apply_index（本地补行；OV 只报告） |
| 9.3 | changelog | validate_changelog（结构校验）+ draft_changelog（git log 规范前缀 draft，`_pending_` 版本号） |
| 9.4 | trace 映射 | check_trace/fix_trace（frontmatter trace/source_files，基准归一化 plugins/ 前缀） |
| 9.5 | viking:// URI | check_ov_uris（解析 OVFS 路径 exists；OV 不可达 graceful） |
| 9.6 | wiki 残留 | report_remnants（只报告不删） |
| 9.7 | OV 双目标 | DocTree 抽象（local_root/ovfs_root 双实例，同一逻辑） |
| 9.8 | 代码注释链接 | check_code_links（**AST：仅 docstring + tokenize 注释**，函数体运行时字符串天然排除） |

## 4. 关键设计决策

- **DocTree 双目标抽象**：本地与 OV 同构（实测差异仅 OV 的 .abstract/.overview 元数据），同一扫描/修复逻辑以不同 root 实例化——一处实现两侧复用
- **默认 dry-run**：`REFINEMENT_DOC_DRY_RUN=True`，apply 需显式配置（安全默认）
- **原子写**：apply 用 tempfile+mv，失败 rollback + io_errors 计数，无 partial-write
- **所有方法不抛异常**：读失败/OV 不可达内部消化为报告项（graceful）
- **OV 侧不自动补 INDEX 缺行/不自动修 changelog**：权威源变更人工确认

## 5. 教训（防再犯）

1. **Codex 越权 apply**：任务书「默认 dry-run」被 Codex 在验证阶段直接 apply 真实 docs——DoD 须显式「验证阶段禁止 apply，落盘由 tester 执行」（已入 devtest-workflow skill）
2. **黄金基线对比**：dry-run 报告是修复前快照，apply 后对比应断言「剩余 ⊆ golden 不可修项」，非全量相等
3. **`.abstract/.overview` 是 OV 自动生成**：本地 git 提交前须 gitignore（已加规则）
4. **read_file 对含控制字符文件误判 binary**：文档读取用 terminal 兜底（已知工具怪癖）
