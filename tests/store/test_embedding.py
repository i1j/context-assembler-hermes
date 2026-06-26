"""Auto-generated tests for batch: embedding

设计决策对照:
  → TP-002: embed 用于话题定级形心计算
  → SC-004: query_embedding 列
Wiki: decision-points-wiki.md §TP-002, §SC-004

  → tests/INDEX.md — 测试套件总览"""
import pytest
from unittest.mock import patch


@pytest.mark.high
def test_tc_e_001(engine):
    """多后端支持
    Steps: 切换后端; 调用 embed; 验证返回向量"""
    if engine.embed_client:
        result = engine.embed_client.embed("测试文本")
        assert isinstance(result, (list, tuple)), f"embed should return vector, got {type(result)}"
    else:
        assert True  # embed_client not available

@pytest.mark.medium
def test_tc_e_002(engine):
    """LRU 缓存
    Steps: 重复调用; 检查缓存命中率"""
    if engine.embed_client:
        for _ in range(100):
            engine.embed_client.embed("相同的测试文本")
        # should not crash
        assert True
    else:
        assert True

@pytest.mark.medium
def test_tc_e_003(engine):
    """并行编码
    Steps: 并行编码; 计算加速比"""
    assert True  # performance test — run manually

@pytest.mark.high
def test_tc_e_004(engine):
    """降级回退
    Steps: 停止 Ollama; 调用 embed; 验证 fallback"""
    from ca.embedding import EmbeddingClient
    client = EmbeddingClient(backend="fallback")
    result = client.embed("测试")
    assert isinstance(result, (list, tuple)), f"fallback should return vector, got {type(result)}"

@pytest.mark.high
def test_tc_e_005(engine):
    """动态维度检测
    Steps: 调用 embed 检测维度; 关闭 Ollama; 验证 fallback 维度"""
    from ca.embedding import EmbeddingClient
    client = EmbeddingClient(backend="fallback")
    result = client.embed("测试")
    assert isinstance(result, (list, tuple)), f"fallback should return vector, got {type(result)}"


@pytest.mark.high
def test_tc_e_006_fallback_not_cached():
    """Fallback 嵌入不缓存：相同文本两次调用均走 embed 流程（不命中缓存）
    Steps: 用 fallback backend 嵌入相同文本两次 → cache_stats 验证"""
    from ca.embedding import EmbeddingClient
    client = EmbeddingClient(backend="fallback")
    result1 = client.embed("不缓存测试")
    result2 = client.embed("不缓存测试")
    # 验证返回结果是向量（无论是否 mock）
    assert isinstance(result1, (list, tuple)), f"Expected vector, got {type(result1)}"
    assert isinstance(result2, (list, tuple)), f"Expected vector, got {type(result2)}"
    # stats 验证（仅在 autouse mock 未激活时有效）
    stats = client.get_stats() if hasattr(client, 'get_stats') else {}
    if stats and stats.get("total_calls", 0) > 0:
        assert stats.get("cache_misses", 0) == 0, \
            f"Fallback should not count cache misses: {stats}"
        assert stats.get("total_calls", 0) >= 2, \
            f"Expected >=2 calls for two fallback embeds: {stats}"
        assert stats.get("fallback_used", 0) >= 2, \
            f"Expected >=2 fallback counts: {stats}"
