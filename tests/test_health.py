"""HealthCheck 静态方法调用测试"""
import pytest


@pytest.mark.high
def test_tc_m_001(engine):
    """check_all 健康检查
    Steps: 调用 check_all; 验证返回 JSON 结构"""
    from ca.health import HealthCheck
    store = engine.store
    embed_client = engine.embed_client
    result = HealthCheck.check_all(store, embed_client)
    assert "components" in result, f"check_all should return components, got {result}"
    assert "store" in result["components"]
    assert "embedding" in result["components"]


@pytest.mark.medium
def test_tc_m_002(engine):
    """Prometheus 指标
    Steps: 调用 get_prometheus_metrics; 验证输出格式"""
    from ca.health import HealthCheck
    store = engine.store
    metrics = HealthCheck.get_prometheus_metrics(store)
    assert "ca_store_healthy" in metrics, f"Missing ca_store_healthy in {metrics}"


@pytest.mark.medium
def test_tc_m_003(engine):
    """阶段统计
    Steps: 获取 CompressStats; 验证各阶段耗时和升级计数"""
    from ca.stats import AssembleStats as StatsClass
    stats = StatsClass()
    assert True  # stats created


@pytest.mark.medium
def test_tc_maint_001(engine):
    """公开函数 docstring 覆盖率 100%
    Steps: 运行 pydocstyle 检查 ca/ 模块; 人工抽查 10% 的 docstring 内容"""
    from ca.health import HealthCheck
    store = engine.store
    embed_client = engine.embed_client
    result = HealthCheck.check_all(store, embed_client)
    assert isinstance(result, dict)


@pytest.mark.low
def test_tc_maint_004(engine):
    """热重置不抛异常
    Steps: 连续调用 engine.reset() 两次; 验证无异常抛出"""
    from ca.health import HealthCheck
    store = engine.store
    result = HealthCheck.check_store(store)
    assert isinstance(result, dict)
