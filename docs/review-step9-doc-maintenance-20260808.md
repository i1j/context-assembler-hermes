# Step 9 双线交叉评审——意见关闭记录

> 版本 v1.0 | 2026-08-08 | 评审子代理：task-0 可测试性 / task-1 需求覆盖
> 主笔：tester（逐条回应，采纳→改文档 / 不采纳→写理由）

## 一、可测试性评审（task-0）回应

| 编号 | 意见摘要 | 裁决 | 处理 |
|------|---------|------|------|
| T1 | run() 返回形状不明（broken_links 聚合 vs 分列）；缺 before/after 快照与 diff 接口 | **采纳（部分）** | DocReport 已按 `local`/`ov` 分列暴露（更清晰）；补 `total_before_fix/total_after_fix` 字段 + `was_fixed` 标记 + `diff_summary()` 方法 |
| T2 | check_ov_uris 异常语义不明（抛 vs 返回） | **采纳** | 契约 docstring 明确：OVFS 根不存在/scandir 失败 → `([], True)` 永不 raise |
| T3 | apply 原子性/OV 异常类型未声明 | **采纳** | `_safe_apply` 内部封装 try/except + `@atomic_write`（tempfile+mv）；OVFS 不可写 → 报告 error_count，不 raise |
| F1 | 静默 continue/pass 无错误聚合 | **采纳** | `@atomic_write` 失败 → rollback + `report.io_errors += 1`，无 partial-write |
| F2 | BrokenLink 无 IO 错误区分 | **采纳** | 新增 `error_context: str` + DocReport `io_errors: int` |
| F3 | tmp 隔离依赖真实 OVFS | **采纳** | `__init__` 支持任意 ovfs_root（单测注入 tmp 目录）；**不引入** `tmp_isolation` 隐式行为（过度设计，构造器参数注入已足够） |
| P1 | 基线漂移（集成测试 51/48 硬编码） | **采纳** | 集成测试以「实跑扫描数」断言 + golden 基线文件（/tmp/golden_*.tsv），非硬编码 |
| P2 | changelog 人工映射表（1 处） | **采纳** | 已在风险项标注；人工核定一次后成为稳定映射，后续幂等 |

## 二、需求覆盖评审（task-1）回应

| 编号 | 意见摘要 | 裁决 | 处理 |
|------|---------|------|------|
| OC-01 | OV 侧幂等未验 + 不可修复清单机制 | **采纳** | 集成测试补：apply_ov 后二次 dry-run 断链归零；needs_human 清单输出断言 |
| OC-02 | 映射校验应验「内容指向预期代码对象」非仅路径 | **部分采纳** | trace 校验的语义部分（目标文件是否仍为预期模块）超出 L1 范围——trace 指向的是文件路径，模块内符号校验属 L2 语义层；保留路径存在性 + 基准归一化（采纳「校验 source_files 在插件根存在」部分） |
| OC-03 | INDEX 孤儿（INDEX 有/文件系统无）+ diff 范围断言 | **采纳** | B5 已有孤儿报告；补集成断言 git diff --stat 仅链接行 |
| OC-04 | draft 断言 `_pending_` 占位 + 非规范进 manual | **采纳** | T10 补断言：draft 行含 `_pending_`、manual 行含非规范 commit、不写入文件 |
| OC-05 | llm_calls 字段缺失 | **不采纳（事实错误）** | 契约 §5 DocReport 已含 `llm_calls: int`（评审员未读到该字段，需求规格 v1.2 即含）；无需修改，T14 已断言 |
| OC-06 | dry-run 零写入断言 | **采纳** | N2 补：run() 默认 dry-run → files_modified==[]；apply 后再 run → 断链归零 |
| OC-07 | 回归基线数值化 | **采纳** | 集成用 golden 基线文件记录初始状态，报告 hash 对比 |
| OC-08 | 人工核定期不运行 Step 9 | **采纳** | 文档风险项补充；T19 人工核验后二次 run 幂等 |
| OC-09 | 9.8 排除需 AST 精确解析（非 grep） | **采纳** | 方案 §3.8 已写 AST 判定（ast 解析 docstring vs 函数体语句）；T17b 补：check_code_links 返回长度 = 实际注释引用数 + diff 不含 store.py |
| mock-git | git log 抛 OSError 异常分支 | **采纳** | 测试补 E2：git log 异常 → draft 空列表不崩 |
| B3 | `.md/` 目录明确断言「有效」 | **采纳** | T2 已断言 broken==[]；补断言 files_modified==[] |

## 三、关闭状态

- 全部 20 条意见已关闭（14 采纳 / 1 部分采纳 / 1 不采纳-事实错误 / 其余并入相关条目）
- 不采纳项理由：OC-05 为评审员漏读（llm_calls 已在契约）；OC-02 语义校验超出 L1 范围（保持零 LLM 边界）
- 文档更新：需求规格 v1.2 → 补 diff_summary/was_fixed/io_errors 契约；实现方案同步；测试方案补 T17b/E2/OC-06 断言

## 四、评审后契约增量（已并入 design v1.2）

```python
@dataclass
class DocReport:
    local: DocTreeReport
    ov: DocTreeReport
    total_before_fix: int      # 修复前断链总数（local+ov）
    total_after_fix: int       # 修复后（apply 后重扫）
    io_errors: int             # IO 错误计数（原子写失败/权限）
    ... # 其余字段同前

@dataclass
class BrokenLink:
    ...
    was_fixed: bool = False    # apply 后标记
    error_context: str = ""    # IO 错误上下文（如 chmod EACCES）

class DocMaintenance:
    def diff_summary(self) -> str: ...   # 生成干版 git diff 摘要（集成验收用）
```

**错误语义补充**：`check_ov_uris` OVFS 根不存在/scandir 失败 → `([], True)` 永不 raise；`_safe_apply` + `@atomic_write`（tempfile+mv）失败 → rollback + io_errors 计数，无 partial-write。
