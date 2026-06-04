# CA (ContextAssembler) v4.4.0 — Tester Profile 部署

## 部署快照

| 项 | 值 |
|---|---|
| 版本 | v4.4.0 |
| 部署方式 | 自包含独立副本，调试不影响 sysadmin |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/` |
| 文件数 | 18（`__init__.py` + `plugin.yaml` + `ca/` 核心引擎 15 文件 + 文档） |
| Config | `plugins.enabled: ca_assembler` 已在 tester config.yaml 中 |
| 来源 | `~/projects/context-assembler/plugins/context_engine/ca_assembler/` + `~/projects/context-assembler/ca/` |

## 已知修复（相对于源项目 v4.4.0）

| 修复 | 说明 |
|---|---|
| `_state_file_path()` 用 `get_hermes_home()` | 原代码写死 `Path.home() / ".hermes"`，已改为 profile 感知。断路器状态文件写入 `~/.hermes/profiles/tester/`，不污染其他 profile 或根目录 |
| `sys.path` 保障本地 `ca/` 优先 | 插件目录加入 `sys.path.insert(0, ...)`，确保导入走本地 `ca/` 子目录而非 Hermes venv 的 `ca.pth` 指针 |
| 添加 `register(ctx)` 函数（2026-06-04） | 原插件无 `register()` 函数，导致 Hermes 插件加载器报 `"has no register() function"`，所有 hook 回调永不注册。新增 `register()` 注册 5 个 hooks：`on_session_start/on_session_end/on_session_reset/pre_llm_call/post_llm_call` |
| Hook 签名适配 | 所有 hook 回调改为 `**kwargs: Any` 模式（与内置 nemo_relay 插件一致）。`on_session_start` 移除对 `kwargs["hermes_home"]` 的依赖（hook 不传递该字段），改用 `get_hermes_home()`。`pre_llm_call` 返回 `Optional[str]`（上下文文本注入），不返回消息列表 |
| 模块级引擎注册表 | `_engines: Dict[session_id → CAContextAssemblerPlugin]` + 锁，支持多 session 并发 |

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

## OpenViking 参考

| 内容 | URI |
|---|---|
| 最新测试报告 | `viking://resources/hermes-agent/ca-v440-test-report.md` |
| QA bug 报告 | `viking://resources/hermes-agent/ca-v440-qa-bug-report.md` |
| 原始测试报告 | `viking://resources/hermes-agent/ca-v440-test-report-original.md` |
| Bug 卡片（7 个） | `viking://resources/hermes-agent/ca-v440-bugs/` |
| 集成历史 | `viking://user/python-api-dev/memories/events/2026/05/23/ca_plugin_integration.md` |
| 项目架构文档 | `~/projects/context-assembler/AGENTS.md` |
| 测试执行指南 | 同目录 `agents.md` |
