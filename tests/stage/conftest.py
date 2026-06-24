"""stage/ 集成测试 conftest：开启 autouse mock 防止 embed/LLM 真实调用。

stage 测试使用真实 ca_engine 并触发 A-stage/F-stage 管线，
需要 mock 外部依赖避免测试挂死或依赖真实服务。
"""
import pytest


@pytest.fixture(autouse=True)
def _stage_mock_embed():
    """EmbeddingClient.embed 类级 patch（无须 ca_engine 实例）。"""
    from unittest.mock import patch
    from tests.fixtures.fixtures_mock import _content_hash_embed
    with patch(
        "ca.embedding.EmbeddingClient.embed",
        side_effect=lambda text: _content_hash_embed(text),
    ):
        yield


@pytest.fixture(autouse=True)
def _stage_mock_llm(request):
    """ContextAssembler._call_llm_for_fct 类级 patch。

    默认返回有效 mock Fct。支持 @pytest.mark.llm_return 注入不同返回值。
    """
    from unittest.mock import patch
    _LLM_MOCK_RESPONSE = (
        "### 现象与问题\n- 无\n"
        "### 背景与约束\n- 无\n"
        "### 决策与方案\n- 无\n"
        "### 后续行动\n- 无\n"
        "<core_change>mock_response</core_change>",
        "stop",
    )
    retval = _LLM_MOCK_RESPONSE
    marker = request.node.get_closest_marker("llm_return")
    if marker:
        retval = marker.args if marker.args else _LLM_MOCK_RESPONSE
    with patch("ca.ContextAssembler._call_llm_for_fct", return_value=retval):
        yield
