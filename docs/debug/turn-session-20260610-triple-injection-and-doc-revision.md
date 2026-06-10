# 设计文档修订与三路注入落地调试报告

## 元信息

| 项目 | 内容 |
|------|------|
| 会话 | `20260610_013946_bf8a52`（CA 缓存 session） |
| 日期 | 2026-06-10 01:50 – 02:40 |
| 用户 | CA 架构设计者 |
| 涉及模块 | `AGENTS.md`、`technical-plan.md`、`changelog.md`、`ca/config.py`、`__init__.py`、`tests/debug_injection_demo.py` |

## 改动清单

### 1. AGENTS.md —— 完整重写

| 节 | 改前 | 改后 |
|----|------|------|
| 注入方式 | 两路（mutation / annotation） | 三路（replace / append / off） |
| 注入路径表 | 二路对比表 | 三路对比表 + off 模式（纯数据积累） |
| 格式化函数 | 提到 mutation 模式 | 改为 replace 模式 |
| AssemblyCache | 未文档化 | 完整的 6 种缓存 Key 类型表 |
| 关键环境变量 | `CA_HISTORY_MUTATE`（二值） | `CA_HISTORY_INJECTION`（三态） + `CA_HISTORY_MUTATE` 标已弃用 |
| 行类型注射规则 | 仅 mutation 替换 | 明确三路模式下各自的规则 |

### 2. technical-plan.md —— 附录清理 + 注入模式更新

删除/清理：

- **附录 C**（三轮评审处理记录）→ 53 项闭环记录移至 changelog 汇总行
- **附录 E**（v4.7.1 基线架构汇总）→ 17 节内容通过 §2.2 模块依赖的"基线 v4.7.1 稳定"标注 + AGENTS.md 分散承接  
  ⚠️ 操作注意：patch 删除多行块时曾残留尾部空行 + 二进制字节，需 `head -c -N` 清理

更新：

| 位置 | 改前 | 改后 |
|------|------|------|
| §2.1 架构图 | `两路注入`/`CA_HISTORY_MUTATE` | `三路注入`/`Config.HISTORY_INJECTION`，追加 off 框 |
| §2.2 模块依赖 | `mutation 模式` | `replace 模式` |
| §4.4 标题+注释 | `mutation 模式默认` | `replace 模式默认` |
| §4.4 code block | `CA_HISTORY_MUTATE=0` | `CA_HISTORY_INJECTION=append` |
| §5.3.11 _CA_TAG_RE | 6 处 mutation/annotation | replace/append |
| §D1 工具组注入 | `CA_HISTORY_MUTATE=1` | `CA_HISTORY_INJECTION=replace` |
| §D2 | `Mutation 模式`，配置 `HISTORY_MUTATE` | `replace 模式`，配置 `HISTORY_INJECTION` |
| §D3 | `历史文档清理` | `文档清理` |
| 最终结论 | 提及 mutation/annotation | replace/off/append 三种模式 |

### 3. ca/config.py —— 注入模式三态化

```python
# 改前：二值
HISTORY_MUTATE: ClassVar[bool] = os.getenv("CA_HISTORY_MUTATE", "1") == "1"

# 改后：三态字符串
HISTORY_INJECTION: ClassVar[str] = "replace" / "append" / "off"
```

关键实现细节：
- 读取 `CA_HISTORY_INJECTION`，未设时兼容旧 `CA_HISTORY_MUTATE`（1→replace，0→append）
- 无效值 WARNING 降级为 `"replace"`
- `reload()` 方法同步更新

### 4. plugins/__init__.py —— pre_llm_call 三路分支

```python
# 改前：二路
if Config.HISTORY_MUTATE:
    return _mutation_mode(...)
return _annotation_mode(...)

# 改后：三路
if injection_mode == "off":
    return None
if injection_mode == "append":
    return _annotation_mode(...)
# replace (default)
return _mutation_mode(...)
```

### 5. changelog.md

追加两条决策记录行：mutation 模式落地、三轮评审闭环、CA 注入实时查看方法。

### 6. tests/debug_injection_demo.py

经历了 3 轮改写：初始版（3 种模式全部打印）→ 精简版（仅 replace）→ 最终版（replace JSONL + 矩阵）。现仅保留 replace 模式验证（append/off 不改变 history，对比无意义）。

## 关键发现

### 发现 1：_build_aligned_outcomes 索引假设

`_build_aligned_outcomes` 的 `seq_idx` 计算（`current_turn - 1`）**假设 plan 采用 `_compute_turn_plan_v2` 的结构**：每轮只有 1 个 `dialogue` 条目 + N 个 `tool_group` 条目。

如果 plan 中插入了非用户对话条目（如 assistant 行也标记为 `turn_type="dialogue"`），索引会偏移导致工具组查找失败。

**验证手段**：给 plan 里多加第 2 个对话条目（`turn_type="dialogue"` for plain assistant row），观察到 `tg_by_seq` 索引错位。修复 plan 结构后恢复正常。

### 发现 2：CA 缓存 DB 可重建注入状态

不依赖 Hermes 运行时，仅 3 行 Python 即可从 CA 缓存 DB 复现当前会话的真实注入结果。

详见 `docs/debug/injection-live-inspection-report.md`

### 发现 3：本会话注入状态

- 19 轮对话, 468 条消息, 155 个 plan 条目
- L1 填充率 100%（424/424 行非空）
- 全部 `_assemble_status=0`（无降级）
- 尾区保护正常工作（最近 2 轮 user L2 原文保留）
- 工具组/工具摘要正常产出

## 已知问题与下次调校方向

| 优先级 | 问题 | 说明 |
|--------|------|------|
| P1 | L0 注入移除行为未验证 | `_build_aligned_outcomes` 的 `target_level="L0"` 分支：工具行应返回 `""`（移除）。当前测试全是 L1，未触发此路径 |
| P2 | 多工具组 group_idx 边界 | 当前测试全是单组（api_call_count=1）。当同回合多个工具组时，`group_idx` 跨组递增是否正确？验证点：插件层 `_on_post_api_request` 的 `api_call_count` |
| P3 | ×N 合并是否真实触发 | `_merge_consecutive_tool_outcomes` 从未在实际注入输出中观察到。触发条件：同工具组内连续相同内容的工具行摘要（仅在 `target_level="L0"` 时可能触发） |
| P4 | off 模式下数据积累验证 | `CA_HISTORY_INJECTION=off` 时 C-stage 正常跑但 pre_llm_call 返回 None。需验证 post_llm_call 的 flush + process_turn_async 确实执行 |
| P5 | 注释中的 mutation 残留 | 除已改的 technical-plan.md，`plugins/__init__.py` 和 `ca/__init__.py` 中仍含 `mutation 模式` / `annotation 模式` 等旧术语 |
| P6 | python -c 中文冒号导致 SyntaxError | 写入 Python 字符串时用了全角 `：`，虽然在外层 docstring 里但 `py_compile` 报了 SyntaxError。需用英文冒号 `:` 避免 |
| P7 | patch 删除大块后残留尾部字节 | 删除附录 E 的大块内容后，文件末尾残留了 `\x00\x02` 空字节 + 多余空行。需要用 `head -c -N` 清理后再写 |

## 遗留工作

- [ ] 将 L0 注入测试加入 `test_aligned_outcomes.py`
- [ ] 检查 `ca/__init__.py` 中 `_build_aligned_outcomes` 注释的 `mutation` 术语
- [ ] 将 injection 实时查看方法封装为可复用的调试脚本
- [ ] 同步更新 `plugins/__init__.py` 中残留的旧术语注释
