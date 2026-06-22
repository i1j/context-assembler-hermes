# CA (Context Assembler) 项目文档索引

> 自动上下文汇编器 — Hermes 插件
> 所有文档已集中存放于此目录。

---

## 设计文档

| 路径 | 内容 |
|------|------|
| `design/decision-points-wiki.md` | 决策树全文（主入口，含 E-stage 写即落盘 + 代码分化说明） |
| `design/tests/INDEX.md` | **测试体系总览**（中央索引，2026-06-19 新增） |
| `design/decision-points/INDEX.md` | 决策树节点索引（141 个独立决策点，v5.10） |
| `design/decision-points/R-000.md` | 根节点：设计哲学（Elm/Fct/Hdl + ACT/REL/FAR） |
| `design/decision-points/C-*.md` | C/A/L-stage 管线决策（29 个） |
| `design/decision-points/P-*.md` | Plugin 适配（5 个） |
| `design/decision-points/S-*.md` | 存储层（6 个） |
| `design/decision-points/D-*.md` | 内存缓存（6 个） |
| `design/decision-points/DE-*.md` | 去重（4 个） |
| `design/decision-points/RE-*.md` | 检索（2 个） |
| `design/decision-points/T-*.md` | 工具摘要（2 个） |
| `design/decision-points/E-*.md` | 嵌入服务（4 个） |
| `design/decision-points/F-*.md` | 配置体系（4 个） |
| `design/decision-points/V-*.md` | v5.1 注入重构（4 个） |
| | `design/decision-points/SC-*.md` | Schema v5 重构（4 个） |
| | `design/decision-points/TP-*.md` | 话题拣选 v4.6.0（7 个） + v5.10 死代码清理（1 个） |
| | `design/decision-points/L1-*.md` | L1 摘要重构 v4.7.0 PDD（12 个） |
| `design/decision-points/INC-*.md` | 事故报告（3 个） |
| `design/decision-points/CR-*.md` | 审查修复（3 个） |
| `design/decision-points/H-*.md` | 历史决策演化（10 个） |
| `design/decision-points/GAP-*.md` | 已知差距项（10 个） |
| `design/decision-points/*` | 其他：TK, HC, TH, SYS |
| `design/impl/bg-review-a-stage-skip.md` | bg_review A-stage 跳过闸门 |
| `design/impl/replace-mode-injection-refactoring.md` | Replace 模式注入重构方案 |
| `design/ca-20k-dialogue-tail-redesign.md` | 20K 对话尾区设计 |
| `design/ca-budget-resummary-redesign.md` | 预算-再摘要重构设计 |
| `design/ca-context-engine-pipeline-shell.md` | 替换 Hermes compress engine 空壳管线（CE-000~CE-003） |
| `design/ca-ce-shell-to-real.md` | CE 空壳→实装技术方案（CS-000~CS-006） |
| `design/test-system-refactoring-v5.0.md` | 测试体系重构设计（如存在） |

## 测试 & 调试

| 路径 | 内容 |
|------|------|
| `tests/INDEX.md` | **测试体系总览**（中央索引，含决策点↔测试文件对照矩阵） |
| `testing/bugs/bug-001/` | A-stage tide marker 偏移 |
| `testing/bugs/bug-002/` | C-stage 异步时机 |
| `testing/bugs/bug-003/` | Plugin hooks 不匹配 |
| `testing/bugs/bug-004/` | 并发竞态 |
| `testing/bugs/bug-005/` | Plugin update model API mode |
| `testing/bugs/bug-007/` | OODA parser core_change 冒号前缀 |
| `testing/bugs/obs-001/` | Subdirectory hint tracker dedup |
| `testing/test-reports/test-report-v4.4.0.md` | 测试报告 v4.4.0 |
| `testing/test-reports/test-report-original.md` | 原始测试报告（v4.3） |
| `testing/test-reports/test-report-ce-shell.md` | CE 空壳管线测试报告（v5.1.1） |
| `testing/qa-reports/qa-bug-report.md` | QA 报告 |

---

> **项目入口**: `design/decision-points-wiki.md`（决策树全文）
> **测试入口**: `tests/INDEX.md`（测试体系总览）
> **导航起点**: `design/decision-points/INDEX.md`（节点索引）
> **代码基线**: master=v4.4.0 (`~/projects/context-assembler/`) / deploy=v6.0 (`~/.hermes/profiles/tester/plugins/ca_assembler/`)
