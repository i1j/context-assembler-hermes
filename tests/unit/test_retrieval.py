"""Unit tests for ca/retrieval.py — cosine_similarity, _dynamic_allocation, _rrf_fuse.

设计决策对照:
  → RE-001: BM25 自实现零依赖 (test_cosine_*)
  → RE-002: RRF K=60 (test_rrf_*)
  → C-015: 动态分配 BM25/向量候选 (test_dynamic_allocation_*)
Wiki: decision-points-wiki.md §RE-001, §RE-002, §C-015

  → tests/INDEX.md — 测试套件总览"""
import pytest
from ca.retrieval import (
    cosine_similarity,
    cosine_similarity_batch,
    _dynamic_allocation,
    _rrf_fuse,
)


class TestCosineSimilarity:
    def test_identical(self):
        v = [0.1, 0.2, 0.3]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite(self):
        a = [1.0, 2.0]
        b = [-1.0, -2.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        a = [1.0, 0.0]
        b = [0.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_typical(self):
        a = [0.5, 0.5, 0.5]
        b = [0.6, 0.4, 0.2]
        sim = cosine_similarity(a, b)
        assert 0.5 <= sim <= 1.0


class TestCosineSimilarityBatch:
    """cosine_similarity_batch(query, targets_dict) -> list of (key, score)."""

    def test_basic(self):
        query = [1.0, 0.0]
        targets = {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [0.5, 0.5]}
        results = cosine_similarity_batch(query, targets)
        assert len(results) == 3
        scores_by_key = dict(results)
        assert scores_by_key[1] == pytest.approx(1.0)
        assert scores_by_key[2] == pytest.approx(0.0)

    def test_empty_targets(self):
        assert cosine_similarity_batch([1.0, 0.0], {}) == []


class TestDynamicAllocation:
    def test_balanced(self):
        bm25, vec = _dynamic_allocation(10, 10, 10)
        assert bm25 + vec <= 10

    def test_only_bm25(self):
        bm25, vec = _dynamic_allocation(10, 0, 5)
        assert vec == 0
        assert bm25 <= 5

    def test_only_vec(self):
        bm25, vec = _dynamic_allocation(0, 10, 5)
        assert bm25 >= 0
        assert vec >= bm25  # vec should be >= bm25 when only vec has hits

    def test_zero_max(self):
        bm25, vec = _dynamic_allocation(0, 0, 0)
        assert bm25 == 0 and vec == 0

    def test_hit_ratio_edge(self):
        """Only one source has hits."""
        bm25, vec = _dynamic_allocation(1, 0, 10)
        assert bm25 >= 1


class TestRrfFuse:
    def test_single_list(self):
        keys = [1, 2, 3]
        result = _rrf_fuse([keys], k=60)
        assert len(result) == 3
        assert result == [1, 2, 3]

    def test_two_lists(self):
        list1 = [1, 2, 3]
        list2 = [2, 3, 4]
        result = _rrf_fuse([list1, list2], k=60)
        # Turn 2 should rank higher (appears in both)
        assert len(result) >= 3

    def test_empty(self):
        assert _rrf_fuse([], k=60) == []

    def test_single_empty_list(self):
        assert _rrf_fuse([[]], k=60) == []
