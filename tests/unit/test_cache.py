"""Unit tests for ca/cache.py — AssemblyCache, BM25Okapi, CacheBuilder.

设计决策对照:
  → D-001: AssemblyCache 类级别单例
  → D-003: 本版仅对话轮 BM25 索引（工具缓存已移除以适配 turn_stream）
  → RE-001: BM25 自实现零依赖
  → tests/INDEX.md — 测试套件总览"""
import pytest
from ca.cache import AssemblyCache, BM25Okapi, tokenise, _is_cjk_char


class TestTokenise:
    def test_empty(self):
        assert tokenise("") == []

    def test_ascii(self):
        tokens = tokenise("hello world test")
        assert "hello" in tokens
        assert "world" in tokens

    def test_cjk(self):
        tokens = tokenise("你好世界")
        assert len(tokens) >= 2

    def test_mixed(self):
        tokens = tokenise("hello你好world")
        assert len(tokens) >= 2


class TestIsCjkChar:
    def test_cjk(self):
        assert _is_cjk_char('中')
        assert _is_cjk_char('文')

    def test_ascii(self):
        assert not _is_cjk_char('a')
        assert not _is_cjk_char('1')


class TestBM25Okapi:
    def test_empty_corpus(self):
        bm25 = BM25Okapi([])
        assert bm25.document_count == 0

    def test_single_doc(self):
        # TurnKey for dialogue: just an int
        bm25 = BM25Okapi([(1, "hello world")])
        assert bm25.document_count == 1
        scores = bm25.get_scores(["hello"])
        assert len(scores) == 1
        assert scores[0] > 0

    def test_multi_doc(self):
        corpus = [
            (1, "python code test"),
            (2, "java code test"),
            (3, "python django web"),
        ]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(["python"])
        assert len(scores) == 3
        assert scores[0] > 0
        assert scores[1] == 0  # doc 1 has no "python"

    def test_turn_key_lookup(self):
        bm25 = BM25Okapi([(5, "content")])
        assert bm25.get_turn_key(0) == 5

    def test_get_scores_unknown_tokens(self):
        bm25 = BM25Okapi([(1, "hello")])
        scores = bm25.get_scores(["nonexistent"])
        assert all(s == 0.0 for s in scores)


class TestAssemblyCache:
    def test_init(self):
        cache = AssemblyCache()
        assert cache.fct_texts == {}
        assert cache.hdl_texts == {}

    def test_add_turn(self):
        cache = AssemblyCache()
        cache.add_turn(1, "Hdl", '{"core_change": "test"}')
        l1, l0 = cache.get_snapshot_data()
        assert 1 in l1
        assert l1[1] == '{"core_change": "test"}'
        assert l0[1] == "Hdl"

    def test_get_snapshot_data_returns_copy(self):
        cache = AssemblyCache()
        cache.add_turn(1, "l0", "l1")
        l1, l0 = cache.get_snapshot_data()
        l1[2] = "hacked"
        assert 2 not in cache.fct_texts

    def test_destroy(self):
        cache = AssemblyCache()
        cache.add_turn(1, "l0", "l1")
        cache.destroy()
        cache.destroy()  # second call should not raise

    def test_rebuild_bm25_snapshot_with_no_data(self):
        cache = AssemblyCache()
        cache.rebuild_bm25_snapshot()  # should not crash

    def test_cancel_retry_timer(self):
        cache = AssemblyCache()
        cache.cancel_retry_timer()  # should be safe no-op

    def test_get_snapshot_data_empty(self):
        cache = AssemblyCache()
        l1, l0 = cache.get_snapshot_data()
        assert l1 == {}
        assert l0 == {}
