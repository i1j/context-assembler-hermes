"""Integration tests for v5.2.1 features: Jaccard merge, adaptive threshold."""
import json
from unittest.mock import patch
import pytest


class TestJaccardMergeThreshold:
    """Jaccard 独立合并路径"""

    def _compute(self, engine, fct_data, threshold):
        fct_texts = {i: json.dumps(d) for i, d in fct_data.items()}
        fct_embeddings = {i: [0.1 + i * 0.01] * 768 for i in fct_data}
        turn_to_topic, _ = engine._compute_topic_groups(fct_texts, fct_embeddings, jaccard_merge_threshold=threshold)
        return turn_to_topic

    def test_high_threshold_prevents_merge(self, ca_engine):
        base = {"new_materials": ["A"], "core_change": "a", "todo": [], "objective_facts": [], "consensus": []}
        fct_data = {1: {**base, "new_materials": ["内容A"]}, 2: {**base, "new_materials": ["内容B"]}}
        tt = self._compute(ca_engine, fct_data, threshold=100.0)
        assert tt[1] != tt[2]

    def test_low_threshold_merges(self, ca_engine):
        base = {"new_materials": ["A"], "core_change": "a", "todo": [], "objective_facts": [], "consensus": []}
        fct_data = {1: {**base, "new_materials": ["内容A"]}, 2: {**base, "new_materials": ["内容B"]}}
        tt = self._compute(ca_engine, fct_data, threshold=0.001)
        assert tt[1] == tt[2]


class TestAdaptiveThreshold:
    """自适应阈值持久化"""

    def test_load_topic_meta_missing(self):
        from ca import ContextAssembler
        with patch.object(ContextAssembler, '_topic_threshold_meta_path', return_value="/nonexistent/path.json"):
            meta = ContextAssembler._load_topic_meta()
            assert meta is None

    def test_save_and_load_roundtrip(self, tmp_path):
        from ca import ContextAssembler
        meta_path = str(tmp_path / "topic_threshold_meta.json")
        with patch.object(ContextAssembler, '_topic_threshold_meta_path', return_value=meta_path):
            ContextAssembler._save_topic_meta({"global_sum": 1.5, "global_count": 3, "last_ideal": 0.5})
            meta = ContextAssembler._load_topic_meta()
            assert meta["global_sum"] == 1.5
            assert meta["global_count"] == 3
            assert meta["last_ideal"] == 0.5

    def test_load_start_threshold_no_meta(self, ca_engine):
        from ca import ContextAssembler
        with patch.object(ContextAssembler, '_load_topic_meta', return_value=None):
            t = ca_engine._load_start_threshold()
            assert t > 0

    def test_load_start_threshold_with_meta(self, ca_engine, monkeypatch):
        monkeypatch.delenv("CA_TOPIC_JACCARD_MERGE", raising=False)
        from ca import ContextAssembler
        with patch.object(ContextAssembler, '_load_topic_meta', return_value={"global_sum": 0.6, "global_count": 3, "last_ideal": 0.15}):
            t = ca_engine._load_start_threshold()
            assert 0.08 <= t <= 0.35

    def test_persist_ideal_threshold(self, ca_engine, tmp_path):
        """persist_ideal_threshold 更新持久化文件"""
        from ca import ContextAssembler
        meta_path = str(tmp_path / "topic_threshold_meta.json")
        with patch.object(ContextAssembler, '_topic_threshold_meta_path', return_value=meta_path):
            ca_engine._ideal_threshold_this_session = 0.25
            ca_engine.persist_ideal_threshold()
            meta = ContextAssembler._load_topic_meta()
            assert meta["last_ideal"] == 0.25
            assert meta["global_count"] == 1

    def test_persist_twice_accumulates(self, ca_engine, tmp_path):
        from ca import ContextAssembler
        meta_path = str(tmp_path / "topic_threshold_meta.json")
        # 预存初始值
        with patch.object(ContextAssembler, '_topic_threshold_meta_path', return_value=meta_path):
            ContextAssembler._save_topic_meta({"global_sum": 1.0, "global_count": 4, "last_ideal": 0.25})
            ca_engine._ideal_threshold_this_session = 0.3
            ca_engine.persist_ideal_threshold()
            meta = ContextAssembler._load_topic_meta()
            assert meta["global_count"] == 5
            assert meta["global_sum"] == 1.3
