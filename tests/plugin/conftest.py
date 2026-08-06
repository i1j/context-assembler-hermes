"""plugin/ 测试 conftest：autouse mock 防止 embed/LLM 真实调用。

plugin 测试使用真实 ca_engine 并触发 A-stage/F-stage 管线。

"""
import sys
from pathlib import Path

import pytest

# Hermes 运行时 tools 包（skill_provenance 等）——独立 pytest 环境缺该路径
try:
    from hermes_constants import get_hermes_home
    _agent_root = Path(get_hermes_home()) / "hermes-agent"
except ImportError:
    _agent_root = Path.home() / ".hermes" / "hermes-agent"
if _agent_root.exists() and str(_agent_root) not in sys.path:
    sys.path.insert(0, str(_agent_root))

@pytest.fixture(autouse=True)
def _plugin_mock_embed():
    """EmbeddingClient.embed 类级 patch。"""
    from unittest.mock import patch
    from tests.fixtures.fixtures_mock import _content_hash_embed
    with patch(
        "ca.embedding.EmbeddingClient.embed",
        side_effect=lambda text: _content_hash_embed(text),
    ):
        yield


@pytest.fixture(autouse=True)
def _plugin_mock_llm(request):
    """ContextAssembler._call_llm_for_fct 类级 patch。"""
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
