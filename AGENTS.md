# CA (ContextAssembler) — Tester Profile 部署

## 部署快照

| 项 | 值 |
|---|---|
| 当前版本 | v4.4.0+（已与源项目 v4.4.0 分化，含多项独有修复和改进） |
| 部署方式 | 自包含独立副本，调试不影响 sysadmin |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/` |
| 核心引擎 | `ca/__init__.py`（1166 行）+ `ca/tool_summarizer.py`（692 行）+ 13 其他文件 |
| 插件适配 | `plugins/ca_assembler/__init__.py`（337 行） |
| Config | `plugins.enabled: ca_assembler` 已在 tester config.yaml 中 |
| 来源 | `~/projects/context-assembler/` + 独有修复 |

## 已知修复与改进（相对于源项目 v4.4.0）

### 初始部署修复（2026-06-04）

| 修复 | 说明 |
|------|------|
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，已改为 profile 感知。断路器状态文件写入 `~/.hermes/profiles/tester/`，不污染其他 profile 或根目录 |
| `sys.path` 保障本地 `ca/` 优先 | 插件目录加入 `sys.path.insert(0, ...)`，确保导入走本地 `ca/` 子目录而非 Hermes venv 的 `ca.pth` 指针 |
| 添加 `register(ctx)` 函数（2026-06-04） | 原插件无 `register()` 函数，导致 Hermes 插件加载器报 `"has no register() function"`，所有 hook 回调永不注册。新增 `register()` 注册 5 个 hooks：`on_session_start/on_session_end/on_session_reset/pre_llm_call/post_llm_call` |
| Hook 签名适配 | 所有 hook 回调改为 `**kwargs: Any` 模式（与内置 nemo_relay 插件一致）。`on_session_start` 移除对 `kwargs["hermes_home"]` 的依赖（hook 不传递该字段），改用 `get_hermes_home()`。`pre_llm_call` 返回 `Optional[str]`（上下文文本注入），不返回消息列表 |
| 模块级引擎注册表 | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁，支持多 session 并发 |

### Bug 修复（2026-06-05）

| 修复 | 说明 | 代码位置 |
|------|------|---------|
| C-stage Executor shutdown 竞态 | `_shutdown_cache_executor()` 直接关 executor 但**没设 `_destroyed`**，后续 C-stage 线程完成时 `submit()` 报 `RuntimeError`。修复：`destroy()` 和 `reset()` 改用 `cache.destroy()`（同时设 `_destroyed` + cancel retry + shutdown executor） | `ca/__init__.py` 第 245, 1148 行 |
| Head 保护区方向错误 | `_compute_layers_v2()` 取 `sorted(...)[-HEAD_AUTO_L1_COUNT:]` 取了**末尾** 3 轮，导致最早对话被压到中区、最新对话多余保护。修复：`[-N:]` → `[:N]` | `ca/__init__.py` 第 662 行 |

### ToolSummarizer 结构化摘要 10 handlers（2026-06-06）

`ca/tool_summarizer.py` 内按工具名分派 handler，替代通用字段提取，L0/L1 质量大幅提升：

| # | Handler | 行号 | L0 摘要示例 |
|---|---------|------|------------|
| 1 | `_summarize_terminal` | 129 | `terminal: find /home -name "*.py" (4 lines)`，内建 pytest 检测 → `pytest: 8 passed, 2 skipped — 0.44s` |
| 2 | `_summarize_execute_code` | 228 | 代理到 terminal handler，替换 tool_name |
| 3 | `_summarize_write_file` | 235 | `write_file: /home/i1j/test.txt`（只保留文件路径） |
| 4 | `_summarize_patch` | 266 | `patch: /home/i1j/tool_summarizer.py` + replace_all 标记 |
| 5 | `_summarize_read_file` | 302 | `read_file: /tmp/test.py`（文件名 + 行数范围） |
| 6 | `_summarize_search_files` | 342 | `search_files: *.py → 0 hits` 或 `search_files: 66 matches [ca:30, tests:25, docs:11]` |
| 7 | `_summarize_skills_list` | 404 | `skills_list: 3 skills (tester-workflow, ...)` |
| 8 | `_summarize_skill_view` | 444 | `skill_view: tester-workflow — 12 lines` |
| 9 | `_summarize_skill_manage` | 496 | `skill_manage: patch tester-workflow (error)`（提取 action/name/file_path） |
| 10 | `_summarize_memory` | 547 | `memory: replace memory (error)`（action/target/old_text + content 80 字符预览） |

分发机制：`summarize()` → `getattr(self, f"_summarize_{sanitized}", None)` → handler。工具名中 `.`/`-` 自动映射为 `_`。handler 异常时 fallback 到通用字段提取。

### ToolSummarizer 改进（2026-06-06 ~ 2026-06-14）

| 改进 | 说明 | 代码位置 |
|------|------|---------|
| terminal 错误标记 | 正则检测 Traceback/Error/Exception 等关键词，匹配时 result_summary 和 L0 前缀 `[ERROR]` | `ca/tool_summarizer.py` 第 183-191 行 |
| search_files 目录分布 | total_count > 3 时 `Counter` 按父目录分组，输出 `search_files: 66 matches [ca:30, tests:25, docs:11]` | `ca/tool_summarizer.py` 第 371-386 行 |
| 连续同工具空结果合并 + ×n 计数 | CA 注入前合并相邻相同内容的摘要，保留 `×{count}` 标记。全在插件层，不动核心引擎 | `plugins/__init__.py` 第 280-299 行 |
| 指纹去重（消费端） | 剥离 `[~/N/M]` 前缀标签后归一化指纹去重，跨轮相同摘要（同名工具同结果、同 core_change 等）只保留最后出现。默认启用，通过 `Config.is_dedup_enabled()` 控制 | `ca/__init__.py` 第 650-652 行 + 第 1012-1050 行 |

### done 区改进（v4.4.1 — 源项目合并前）

| 改进 | 说明 | 代码位置 |
|------|------|---------|
| 对话/工具尾区分离 | 对话尾区仅计对话消息（10K tokens），工具尾区按最近 2 个对话轮判定 | `ca/__init__.py` |
| Context length 三级回退 | Hermes `get_model_context_length()` → 自有查表 → 200K 兜底。压缩预算 = `model_window × 0.50` | `ca/config.py` 第 100-141 行 |
| 后台审查轮识别 | ContextVar `tools.skill_provenance.get_current_write_origin()` 检测 `background_review`，跳过 LLM 摘要生成 | `ca/__init__.py` |
| 话题边界检测 | C-stage 末尾余弦相似度 < 0.50 → 新话题，递增 topic_id | `ca/__init__.py` |
| turn_plan 表（schema v3） | 记录 A-stage 拣选决策（turn_index, target_level, decision_reason, l2_tokens, summary_tokens, tokens_saved, topic_group） | `ca/__init__.py` |

## 当前遗留状态（2026-06-14）

| 问题 | 说明 | 优先级 |
|------|------|--------|
| `_shutdown_cache_executor()` 死代码 | `ca/__init__.py` 第 254-262 行，全项目无调用方。功能已被 `cache.destroy()` 替代。低风险——destroy() 前已有 `wait_for_pending(5.0)` + `_destroyed=True` 守卫 | 低 |
| 空摘要 BM25 排除 | `result_summary="无返回数据"` 的工具轮仍进入检索/升级候选，产生无用升级 | 低 |
| 系统消息降级 | `background_review` ContextVar 路径已修。其他系统触发消息（技能库维护 prompt 等）仍可能被 LLM 误判为「无有效增量」 | 低 |
| bare `except:` 吞异常 | 在 `tests/conftest.py`，不影响被测代码 | 低（tests 目录暂不干预） |

## 预算实测结论（2026-06-14）

18 轮对话实测验证：
- 所有行 `_assemble_status=0`（无降级）
- budget 从未耗尽：~72K 预算 vs ~32K 使用，正预算 ~39K
- `budget=0` 只跳过检索升级（`retriever.retrieve()` 不执行）
- **Middle L0 永远生成，不受预算约束**——这是设计，不是 bug
- `_system_overhead` 默认 20K 仅作保守缓冲区，动态测量代码已于 2026-06-14 移除

## ⚠️ 关键概念：CA 不是 context engine

**CA 是 Hermes 插件（plugin），不是 context engine。**

Hermes 有两条完全独立的机制：

1. **Plugin hooks** → `plugins.enabled` 中的 `ca_assembler`。通过 `register()` 注册的 `on_session_start/pre_llm_call/post_llm_call` 等 hook 回调工作。这是 CA 的核心功能路径——上下文摘要生成、turn 存储、上下文注入。
2. **Context engine** → `context.engine` 配置项。只从仓库 `plugins/context_engine/` 子目录加载引擎，与用户 profile 的 `plugins/ca_assembler/` 毫无关系。

**`context.engine` 设成什么、是否回退，都不影响 CA 插件的工作。** CA 的功能入口是 `plugins.enabled`，不是 `context.engine`。不要再查 `context.engine` 来判断 CA 是否在运行。

## 工作区说明

本目录即完整工作区。所有代码修改（`__init__.py` 或 `ca/*.py`）仅影响 tester profile，不影响 sysadmin。

核心引擎入口：`ca/__init__.py` → `ContextAssembler` 类
插件适配入口：`__init__.py` → `CAContextAssemblerPlugin` 类

## 相关文档

| 文档 | 路径 | 内容 |
|------|------|------|
| agents.md（小写） | `plugins/ca_assembler/agents.md` | 技术手册：Hook API 定义、三阶段数据流图、存储结构、环境变量表、运行时验证命令 |
| 技术方案 | `docs/technical-plan.md` | 完整设计文档 |
| 调试报告验证工作流 | `docs/ca-debug-report-fix-verification.md` | 修复验证流程、关键管线代码位置、未修复项 |
| ToolSummarizer 结构改进 | `docs/tool-summary-improvements.md` | 10 handlers 清单 + 待改进/已排除 |
| L0 摘要质量观察 | `ca-ctx-inspect/references/l0-summary-quality-observations.md` | 对话轮 L0 摘要质量实测（参考） |
| system_overhead 分析 | `docs/system-overhead-measurement-analysis.md` | 测量代码移除分析 |

## OpenViking 参考

| 内容 | URI |
|---|---|
| 最新测试报告 | `viking://resources/hermes-agent/ca-v440-test-report.md` |
| QA bug 报告 | `viking://resources/hermes-agent/ca-v440-qa-bug-report.md` |
| 原始测试报告 | `viking://resources/hermes-agent/ca-v440-test-report-original.md` |
| Bug 卡片（7 个） | `viking://resources/hermes-agent/ca-v440-bugs/` |
| 集成历史 | `viking://user/python-api-dev/memories/events/2026/05/23/ca_plugin_integration.md` |
| 项目架构文档 | `~/projects/context-assembler/AGENTS.md` |
| 测试执行指南 | `tests/agents.md` |
