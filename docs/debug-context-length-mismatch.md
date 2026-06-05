# CA v4.4.0 — context_length 错配调试记录

> 记录时间: 2026-06-05, 对话期间
> Agent: Hermes (deepseek-v4-flash)
> Profile: tester

## 发现

CA 引擎的 `assemble()` 始终按 32K token 预算做上下文压缩，但模型实际上下文为 91.5K。导致压缩策略与真实上下文长度不匹配。

## 因果关系链

```
Hermes invoke_hook("pre_llm_call", ...)
    ↓
kwargs 不含 context_length 参数                    ← conversation_loop.py:704-716
    ↓
CA plugin pre_llm_call: 
  context_length = kwargs.get("context_length", Config.CONTEXT_LENGTH)
    ↓                                                                    ← __init__.py:257
kwargs 中无此 key → 走默认值
    ↓
Config.CONTEXT_LENGTH = int(os.getenv("CA_CONTEXT_LENGTH", "32000"))
    ↓                                                                    ← ca/config.py:71
默认 32K，且 CA_CONTEXT_LENGTH 未设置
    ↓
engine.assemble(user_message, context_length=32000)
    ↓                                                                    ← ca/__init__.py:464
以 32K 为预算做 Head/Middle/Tail 分层 + 检索升级
    ↓
实际模型上下文: deepseek-v4-flash = 91.5K tokens
```

## 影响

| 指标 | 实际值 | CA 认知值 |
|------|-------|-----------|
| 模型上下文长度 | 91,500 tokens | 32,000 tokens |
| 预算正确性 | — | ❌ 偏低 2.86× |
| 压缩策略 | — | 按过小预算做组装 |

## 现场证据

- `conversation_loop.py:704-716`: hook 传参中无 `context_length`
- `__init__.py:257`: CA 回退到 Config.CONTEXT_LENGTH
- `ca/config.py:71`: 默认 32K
- `.env` 确认: `CA_CONTEXT_LENGTH` 为空，无覆盖

## 验证

调用 `assemble()` 返回 145 条消息，仅压了 t1（1/14 轮），t2–t14 全部原样。因为 32K 预算没被填满，引擎未进一步压缩；但若设为 91.5K，预算更充裕，压缩策略会不同。

## 可能的修复路径

1. **Hermes 侧** — `invoke_hook("pre_llm_call", ...)` 增加 `context_length` 参数，传入 `agent.context_compressor.context_length`
2. **CA 侧** — 手动在 tester profile 的 `.env` 设 `CA_CONTEXT_LENGTH=91500`，绕过 hook 缺失
3. **双修** — 既补 hook 传参又设环境变量兜底

## 观察记录者

Hermes agent (tester profile), 2026-06-05 session
