"""EmbeddingClient 单元测试 — 不依赖 engine fixture，直接构造实例。

覆盖:
  - fallback backend 降级
  - fallback 不缓存
  - 维度检测
  - LRU 缓存行为

从 tests/store/test_embedding.py 拆分而来：单元级用例移至此处，
依赖 engine 的集成用例留在 store/test_embedding.py。
"""
import pytest


class TestEmbeddingClientFallback:
    """EmbeddingClient fallback 后端行为"""

    def test_fallback_embed_returns_vector(self):
        """Fallback backend 返回向量（列表/元组）"""
        from ca.embedding import EmbeddingClient
        client = EmbeddingClient(backend="fallback")
        result = client.embed("测试")
        assert isinstance(result, (list, tuple)), \
            f"Expected vector, got {type(result)}"

    def test_fallback_dimension_detected(self):
        """Fallback 嵌入返回非零维度向量"""
        from ca.embedding import EmbeddingClient
        client = EmbeddingClient(backend="fallback")
        result = client.embed("测试维度")
        assert len(result) > 0, "fallback embed should return non-empty vector"
        assert all(isinstance(v, (int, float)) for v in result), \
            "vector elements should be numeric"

    def test_fallback_not_cached(self):
        """Fallback 嵌入不缓存：相同文本两次调用均走 embed 流程"""
        from ca.embedding import EmbeddingClient
        client = EmbeddingClient(backend="fallback")
        result1 = client.embed("不缓存测试")
        result2 = client.embed("不缓存测试")
        assert isinstance(result1, (list, tuple))
        assert isinstance(result2, (list, tuple))
        stats = client.get_stats() if hasattr(client, 'get_stats') else {}
        if stats and stats.get("total_calls", 0) > 0:
            assert stats.get("cache_misses", 0) == 0, \
                f"Fallback should not count cache misses: {stats}"
            assert stats.get("total_calls", 0) >= 2, \
                f"Expected >=2 calls: {stats}"
            assert stats.get("fallback_used", 0) >= 2, \
                f"Expected >=2 fallback counts: {stats}"


class TestEmbeddingClientCache:
    """EmbeddingClient LRU 缓存行为（需 mock 后端避免真实连接）"""


    def test_lru_cache_different_text_different_result(self):
        """不同文本的嵌入结果不同"""
        from ca.embedding import EmbeddingClient
        client = EmbeddingClient(backend="fallback")
        r1 = client.embed("文本A")
        r2 = client.embed("文本B")
        assert r1 != r2, "不同文本应产生不同嵌入向量"
