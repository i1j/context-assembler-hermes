# Bug 卡片 #5: CAContextAssemblerPlugin.update_model() 缺少 api_mode 参数

## 现象

Hermes 启动时立即崩溃：

```
Failed to initialize agent: CAContextAssemblerPlugin.update_model() got an unexpected keyword argument 'api_mode'
```

## 根因

CA 插件的 `update_model()` 签名（`plugins/context_engine/ca_assembler/__init__.py:186`）缺少 `api_mode` 参数：

```python
# 当前（缺 api_mode）
def update_model(self, model="", context_length=0, base_url="", api_key="", provider="") -> None:

# 需要补齐
def update_model(self, model="", context_length=0, base_url="", api_key="", provider="", api_mode="") -> None:
```

Hermes 主仓库的 `agent/agent_init.py:1430`、`agent/agent_runtime_helpers.py:913/1460`、`agent/conversation_loop.py:2407`、`run_agent.py:594` **共 4 处**都已更新调用方传 `api_mode`，但 CA 插件未同步跟进。

基类 `ContextEngine.update_model()`（`agent/context_engine.py:203`）已定义 `api_mode` 参数。内置 `ContextCompressor.update_model()`（`agent/context_compressor.py:491`）也同样有。CA 插件落后了。

## 影响范围

- Hermes 无法在启用 CA 插件时启动
- 无优雅降级——错误在外层被捕获为 "Failed to initialize agent"
- 断路器（circuit breaker）不生效，因为崩溃发生在 `on_session_start` 之前

## 复现

启动任意 Hermes session 即可复现（CA 插件为 context_engine 配置）：

```
hermes
# 或任意 gateway 启动方式
```

## 修复方案

修改 `ca_assembler/__init__.py` 第 186 行，在 `update_model()` 签名末尾添加 `api_mode=""`：

```python
def update_model(self, model="", context_length=0, base_url="", api_key="", provider="", api_mode="") -> None:
```

同时建议在方法体内部记录或使用 `api_mode`（如需要）。

## 涉及模块

- `plugins/context_engine/ca_assembler/__init__.py` — `update_model()` 方法签名

## 优先级

**高** — 阻止 CA 插件正常工作

## 发现时间

2026-05-25

## 修复状态

**✅ 已修复** — 2026-05-25

### 修改内容

`plugins/context_engine/ca_assembler/__init__.py:186` — 在 `update_model()` 签名中添加 `api_mode=""` 参数。

### 修改验证

- 签名兼容测试通过：`api_mode` 参数可正常接收
- `tests/test_plugin.py` 15/15 passed
- `tests/plugins/context_engine/test_ca_assembler_plugin.py` 25/29 passed（4 个失败为 bug-003 预存故障，与本次改动无关）
