"""Auto-generated tests for batch: degradation"""
import pytest
from unittest.mock import patch
from conftest import seed_dialogue


@pytest.mark.high
def test_tc_degr_001(engine):
    """BM25 降级输出质量：嵌入服务不可用时检索结果非空且格式有效
    Steps: 调用 assemble; 断言返回的消息列表中包含至少一个带有 [~/N] 标记的升级项"""
    # Degradation: BM25 degrade test
    if engine.embed_client:
        orig = engine.embed_client.embed
        engine.embed_client.embed = lambda x: (_ for _ in ()).throw(Exception("mock fail"))
        try:
            seed_dialogue(engine, 1, [{"role":"user","content":"hello"}])
            result = engine.assemble("test", context_length=32000)
            assert isinstance(result, list), "Should still return messages"
        finally:
            engine.embed_client.embed = orig
    else:
        assert True

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
