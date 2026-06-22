"""CA 测试 fixture 体系（v5.10+ 适配版）

提供：
- 引擎 fixture（ca_engine / engine）
- 自动 mock 外部依赖（embed / LLM）
- v5 turn_stream fixture（v5_store / v5_turn_stream）
- 文件描述符泄漏检测

Fixtures 定义在 tests/fixtures/ 子包中，此处 re-export。
"""

import sys
from pathlib import Path

# 确保 ca/ 可被 import（需在 re-export 之前执行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "critical: 关键路径测试")
    config.addinivalue_line("markers", "high: 高优先级测试")
    config.addinivalue_line("markers", "medium: 中优先级测试")
    config.addinivalue_line("markers", "low: 低优先级/信息性测试")
    config.addinivalue_line("markers", "fct: 需 Fct LLM 调用")
    config.addinivalue_line("markers", "elm: 需 Elm LLM 调用")
    config.addinivalue_line("markers", "linux_only: 仅 Linux 环境")
    config.addinivalue_line("markers", "v460: 测试已删除的 v4.6 API，仅兼容归档")
    config.addinivalue_line("markers", "llm_return: 注入 mock LLM 返回值（覆盖降级路径）")


# ── 从 fixtures/ 子包 re-export fixture ──
# 显式命名导入（不可使用 import *，下划线开头的 autouse fixture 不会被通配导入）
from tests.fixtures.fixtures_engine import (
    ca_engine,
    engine,
    hardware_info,
    fd_checker,
)
from tests.fixtures.fixtures_store import (
    v5_store,
    v5_turn_stream,
)
from tests.fixtures.fixtures_mock import (
    _content_hash_embed,
    _mock_embed,
    _mock_llm,
)
