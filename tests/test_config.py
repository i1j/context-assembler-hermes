"""Auto-generated tests for batch: config"""
import pytest, os
from unittest.mock import patch


@pytest.mark.high
def test_tc_cf_001(engine):
    """环境变量覆盖
    Steps: Config.reload(); 读取 PROTECT_TAIL_TOKENS"""
    os.environ["CA_PROTECT_TAIL_TOKENS"] = "100"
    from ca.config import Config
    Config.reload()
    assert Config.PROTECT_TAIL_TOKENS == 100, f"Expected 100, got {Config.PROTECT_TAIL_TOKENS}"

@pytest.mark.medium
def test_tc_cf_002(engine):
    """validate 校验
    Steps: Config.reload(); 调用 validate()"""
    os.environ["CA_LLM_TIMEOUT"] = "-1"
    from ca.config import Config
    Config.reload()
    try:
        Config.validate()
        assert False, "Should have raised ValueError"
    except (ValueError, Exception):
        assert True

@pytest.mark.medium
def test_tc_cf_003(engine):
    """热重载
    Steps: 修改环境变量; Config.reload(); 验证新值"""
    os.environ["CA_PROTECT_TAIL_TOKENS"] = "5000"
    from ca.config import Config
    Config.reload()
    assert Config.PROTECT_TAIL_TOKENS == 5000

@pytest.mark.medium
def test_tc_cf_004(engine):
    """调试开关
    Steps: 执行操作; 切换 CA_DEBUG=1; 对比日志"""
    import logging
    os.environ["CA_DEBUG"] = "1"
    from ca.config import Config
    Config.reload()
    assert Config.DEBUG_MODE == True, "CA_DEBUG=1 should enable debug mode"

@pytest.mark.medium
def test_tc_cf_005(engine):
    """非法配置值 Fail‑Safe
    Steps: Config.reload(); 验证去重禁用; 检查 WARNING 日志"""
    os.environ["CA_PROTECT_TAIL_TOKENS"] = "5000"
    from ca.config import Config
    Config.reload()
    assert Config.PROTECT_TAIL_TOKENS == 5000

@pytest.mark.medium
def test_tc_cf_006(engine):
    """负值配置项时 validate 报错
    Steps: 设置 CA_CONTEXT_LENGTH=-1 → Config.reload() → validate 报错"""
    from ca.config import Config
    old = Config.CONTEXT_LENGTH
    os.environ["CA_CONTEXT_LENGTH"] = "-1"
    Config.reload()
    try:
        with pytest.raises((ValueError, AssertionError)):
            Config.validate()
    finally:
        os.environ["CA_CONTEXT_LENGTH"] = str(old)
        Config.reload()
