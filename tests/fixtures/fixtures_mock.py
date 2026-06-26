"""Mock fixtures — _content_hash_embed, _mock_embed, _mock_llm."""

import pytest
from unittest.mock import patch


def _content_hash_embed(text: str, dim: int = 768) -> list:
    """Deterministic content-differentiable embedding.

    相同文本 → 相同向量，不同文本 → 不同（但仍归一化）向量。
    """
    import random as _random

    seed = abs(hash(text)) % (2**16)
    rng = _random.Random(seed)
    vec = [rng.uniform(-1, 1) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


_LLM_MOCK_RESPONSE = (
    "### 现象与问题\n- 无\n"
    "### 背景与约束\n- 无\n"
    "### 决策与方案\n- 无\n"
    "### 后续行动\n- 无\n"
    "<core_change>mock_response</core_change>",
    "stop",
)


@pytest.fixture
def _mock_embed():
    """防止测试挂死在 Ollama 嵌入连接。
    
    使用内容可区分伪嵌入（不再用常量向量）。
    不依赖 ca_engine fixture：patch 作用于 EmbeddingClient 类方法，无需实例。
    """
    with patch(
        "ca.embedding.EmbeddingClient.embed",
        side_effect=lambda text: _content_hash_embed(text),
    ):
        yield


@pytest.fixture
def _mock_llm(request):
    """防止 C-stage / F-stage 测试阻塞在真实 LLM 调用。

    默认返回有效 mock Fct。支持 @pytest.mark.llm_return 注入不同返回值。
    """
    retval = _LLM_MOCK_RESPONSE
    marker = request.node.get_closest_marker("llm_return")
    if marker:
        retval = marker.args if marker.args else _LLM_MOCK_RESPONSE

    with patch("ca.ContextAssembler._call_llm_for_fct", return_value=retval):
        yield
