"""unit/ 测试 conftest：插入 Hermes 运行时路径。

供 patch("tools.skill_provenance.get_current_write_origin") 等 Hermes 运行时模块
在独立 pytest 环境可导入。路径使用 get_hermes_home()，不写死 ~/.hermes。
"""

import sys
from pathlib import Path

import pytest

try:
    from hermes_constants import get_hermes_home
    _agent_root = Path(get_hermes_home()) / "hermes-agent"
except ImportError:
    _agent_root = Path.home() / ".hermes" / "hermes-agent"

if _agent_root.exists() and str(_agent_root) not in sys.path:
    sys.path.insert(0, str(_agent_root))


@pytest.fixture(autouse=True)
def _pin_background_origin_foreground(monkeypatch):
    """固定 bg 输入为 foreground，防 pytest 在 bg 上下文驱动时短路 T2-T6/T11/T12/T14。"""
    monkeypatch.setattr(
        "tools.skill_provenance.get_current_write_origin",
        lambda: "foreground",
    )
