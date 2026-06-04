"""适配层单元测试 — 验证 pre_llm_call / post_llm_call 钩子 (v4.3.4)。

## 目的
验证 `CAContextAssemblerPlugin`（`plugins/context_engine/ca_assembler/__init__.py`）
与 Hermes 宿主框架的交互是否正确，包括：
- `pre_llm_call` 钩子正确调用 CA 引擎的 `assemble`
- `post_llm_call` 钩子正确调用 CA 引擎的 `process_turn_async`
- 引擎不可用或出错时的降级行为

## 覆盖范围

| 测试类 | 覆盖方法 | 验证要点 |
|--------|---------|---------|
| `TestPreLlmCall` | `pre_llm_call` | 调用 `assemble` 并传递正确参数；引擎故障/不可用时返回原始 messages |
| `TestPostLlmCall` | `post_llm_call` | 调用 `process_turn_async` 并传递正确参数；异常时标记故障；引擎不可用时跳过 |

## 运行方式

```bash
cd project/CA
pytest tests/test_plugin.py -v
```

"""

import pytest
from unittest.mock import MagicMock
from plugins.context_engine.ca_assembler import CAContextAssemblerPlugin
import sys
from pathlib import Path

# 将项目根目录加入 sys.path（若尚未加入）
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture
def plugin():
    """创建一个带有 mock CA 引擎的插件实例。

    v4.3.4: CAContextAssemblerPlugin 没有 _CA_CLASS 属性，
    引擎通过 session_manager.get() 创建。测试直接注入 mock engine。
    """
    mock_engine = MagicMock()
    mock_engine.assemble.return_value = [
        {"role": "user", "content": "[~/0] hello"},
        {"role": "assistant", "content": "[~/0] hi"},
    ]

    plugin = CAContextAssemblerPlugin()
    plugin._engine = mock_engine
    plugin._engine_errored = False
    return plugin


class TestPreLlmCall:
    """验证 pre_llm_call 钩子。

    v4.3.4 签名: pre_llm_call(self, messages, **kwargs)
    """

    def test_invokes_assemble(self, plugin):
        """pre_llm_call 应调用 assemble() 并传入正确参数。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        plugin.pre_llm_call(
            messages,
            user_input="hello",
            context_length=100_000,
        )
        plugin._engine.assemble.assert_called_once_with(
            "hello",    # user_input
            messages,   # messages
            100_000,    # context_length
        )

    def test_returns_assembled_result(self, plugin):
        """应返回 assemble() 的返回值。"""
        messages = [{"role": "user", "content": "test"}]
        result = plugin.pre_llm_call(messages, user_input="test")
        assert result == [
            {"role": "user", "content": "[~/0] hello"},
            {"role": "assistant", "content": "[~/0] hi"},
        ]

    def test_returns_messages_when_engine_errored(self, plugin):
        """引擎故障时应直接返回原始 messages。"""
        plugin._engine_errored = True
        messages = [{"role": "user", "content": "test"}]
        result = plugin.pre_llm_call(messages, user_input="test")
        assert result is messages

    def test_returns_messages_when_engine_not_ready(self, plugin):
        """引擎不可用时应直接返回原始 messages。"""
        plugin._engine = None
        messages = [{"role": "user", "content": "test"}]
        result = plugin.pre_llm_call(messages, user_input="test")
        assert result is messages


class TestPostLlmCall:
    """验证 post_llm_call 钩子。

    v4.3.4 签名: post_llm_call(self, user_input, assistant_response, **kwargs)
    """

    def test_invokes_process_turn_async(self, plugin):
        """post_llm_call 应调用 process_turn_async() 并传入正确参数。"""
        history = [{"role": "user", "content": "hello"}]
        plugin.post_llm_call(
            user_input="hello",
            assistant_response="hi there",
            conversation_history=history,
        )
        plugin._engine.process_turn_async.assert_called_once_with(
            "hello",      # user_message (位置参数)
            "hi there",   # assistant_response (位置参数)
            history,      # history (位置参数)
        )

    def test_propagates_exception(self, plugin):
        """process_turn_async() 异常应向上传播 (v4.3.4 不捕获)。"""
        plugin._engine.process_turn_async.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError, match="fail"):
            plugin.post_llm_call(
                user_input="hello",
                assistant_response="hi",
            )

    def test_skips_when_engine_not_ready(self, plugin):
        """引擎不可用时应跳过。"""
        plugin._engine = None
        result = plugin.post_llm_call(
            user_input="hello",
            assistant_response="hi",
        )
        assert result is None
