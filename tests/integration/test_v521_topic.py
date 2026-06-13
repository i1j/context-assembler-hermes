"""Integration tests for v5.2.1 features: forced topic split, Jaccard merge, adaptive threshold."""
import json
from unittest.mock import patch
import pytest


class TestForcedSplitTurns:
    """_detect_forced_split_turns — 用户消息含切换短语时强制分裂"""

    def _detect(self, messages):
        from ca import ContextAssembler
        return ContextAssembler._detect_forced_split_turns(messages)

    def test_detect_topic_switch(self):
        forced = self._detect([{"role": "user", "content": "换一个话题", "_turn_index": 3}])
        assert 3 in forced

    def test_detect_change_topic(self):
        forced = self._detect([{"role": "user", "content": "换个话题", "_turn_index": 5}])
        assert 5 in forced

    def test_detect_no_switch(self):
        forced = self._detect([{"role": "user", "content": "继续讨论", "_turn_index": 2}])
        assert 2 not in forced

    def test_detect_multiple_turns(self):
        messages = [
            {"role": "user", "content": "正常聊天", "_turn_index": 1},
            {"role": "user", "content": "换一个话题", "_turn_index": 3},
            {"role": "user", "content": "另一个问题", "_turn_index": 5},
        ]
        forced = self._detect(messages)
        assert 3 in forced and 5 in forced and 1 not in forced

    def test_detect_english(self):
        forced = self._detect([{"role": "user", "content": "change topic", "_turn_index": 4}])
        assert 4 in forced

    def test_detect_no_turn_index(self):
        forced = self._detect([{"role": "user", "content": "换一个话题"}])
        assert forced == set()

    def test_assemble_with_forced_split(self, ca_engine):
        """集成：话题切换短语应产生分裂"""
        from tests.conftest import seed_dialogue

        for i in range(1, 4):
            seed_dialogue(ca_engine, i, [{"role": "user", "content": f"msg{i}", "_turn_index": i}])
            ca_engine.cache.add_turn(i, f"l0_{i}", json.dumps({"new_materials": [f"内容{i}"], "core_change": "c"}))

        l1_texts, _ = ca_engine.cache.get_snapshot_data()
        l1_embeddings = {t: [0.1] * 768 for t in l1_texts}
        turn_to_topic, _ = ca_engine._compute_topic_groups(
            l1_texts, l1_embeddings, jaccard_merge_threshold=0.07, forced_split_turns={3},
        )
        assert turn_to_topic[1] != turn_to_topic[3], "turn 3 should be in a different topic"


class TestJaccardMergeThreshold:
    """Jaccard 独立合并路径"""

    def _compute(self, engine, l1_data, threshold):
        l1_texts = {i: json.dumps(d) for i, d in l1_data.items()}
        l1_embeddings = {i: [0.1 + i * 0.01] * 768 for i in l1_data}
        turn_to_topic, _ = engine._compute_topic_groups(l1_texts, l1_embeddings, jaccard_merge_threshold=threshold)
        return turn_to_topic

    def test_high_threshold_prevents_merge(self, ca_engine):
        base = {"new_materials": ["A"], "core_change": "a", "todo": [], "objective_facts": [], "consensus": []}
        l1_data = {1: {**base, "new_materials": ["内容A"]}, 2: {**base, "new_materials": ["内容B"]}}
        tt = self._compute(ca_engine, l1_data, threshold=100.0)
        assert tt[1] != tt[2]

    def test_low_threshold_merges(self, ca_engine):
        base = {"new_materials": ["A"], "core_change": "a", "todo": [], "objective_facts": [], "consensus": []}
        l1_data = {1: {**base, "new_materials": ["内容A"]}, 2: {**base, "new_materials": ["内容B"]}}
        tt = self._compute(ca_engine, l1_data, threshold=0.001)
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
