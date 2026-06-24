"""CA 测试 fixture 体系（v5.10+ 适配版）

提供：
- 引擎 fixture（ca_engine / engine）
- v5 turn_stream fixture（v5_store / v5_turn_stream）
- 文件描述符泄漏检测
- _content_hash_embed 纯函数（伪嵌入生成）

mock fixture（_mock_embed / _mock_llm）不再由根 conftest 导出，
stage/ 和 plugin/ 目录通过子 conftest 开启目录级 autouse mock。
单元测试按需 mock 自身模块。

Fixtures 定义在 tests/fixtures/ 子包中，此处 re-export。
"""

import sys
from pathlib import Path

# 确保 ca/ 可被 import（需在 re-export 之前执行）
_ca_root = str(Path(__file__).resolve().parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

# 验证 ca/ 可导入
try:
    import ca
except ImportError as exc:
    raise RuntimeError(
        f"CA 核心引擎 (ca/) 无法导入。请确认 {_ca_root}/ca/ 存在且无 import 错误。"
    ) from exc


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

# _content_hash_embed 是纯函数（非 fixture），被 test_topic_manager 等引用
from tests.fixtures.fixtures_mock import (
    _content_hash_embed,
)

# mock fixtures 不再 autouse 导出；需要 mocks 的目录通过子 conftest 开启
# (stage/conftest.py, plugin/conftest.py)
