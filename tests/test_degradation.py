"""Auto-generated tests for batch: degradation"""
import pytest


@pytest.mark.high
def test_tc_degr_002(engine):
    """Fallback 伪向量合理性：相同输入多次返回相同伪向量，维度与真实一致
    Steps: 对同一文本调用 fallback embed 两次; 对比向量，验证相同; 验证维度"""
    from ca.embedding import EmbeddingClient
    client = EmbeddingClient(backend="fallback")
    v1 = client.embed("一致性测试")
    v2 = client.embed("一致性测试")
    assert len(v1) == len(v2), f"Vector dim mismatch: {len(v1)} vs {len(v2)}"
    assert v1 == v2, "Two identical inputs should produce identical fallback vectors"
    assert len(v1) == 768, f"Expected 768 dims, got {len(v1)}"
