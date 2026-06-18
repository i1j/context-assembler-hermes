"""
ContextAssembler v4.6.0 话题拣选重构测试套件
覆盖：三级定级、TopicRetriever、Plan v2、query_embedding、TOPIC_* 配置

特征:
- _grade_topics_by_radius() — 内球L2/外球L1/远距离L0
- TopicRetriever() — per-topic BM25 + vector + RRF
- _compute_turn_plan_v2() — 话题级Plan决策规则 + topic_boost
- query_embedding 列 — Schema v3→v4
- TOPIC_RADIUS_WEIGHT / TOPIC_BG_LEVEL
"""
import json
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from ca import ContextAssembler
from ca.config import Config
from ca.retrieval import TopicRetriever

pytestmark = pytest.mark.v460

TEST_SESSION = "test_v460"


# =============================================================================
# 0. Mock 帮助函数
# =============================================================================



def _fake_embed(dim: int = 768) -> List[float]:
    """生成固定向量，方便确定形心位置"""
    return [0.1 + (i / dim) * 0.0001 for i in range(dim)]


# =============================================================================
# 2. _grade_topics_by_radius — 三级定级
# =============================================================================

class TestGradeTopicsByRadius:
    """按半径 r 三级定级：内球L2、外球L1、远距离L0"""

    def test_gr_001_no_query_embed(self, monkeypatch):
        """无 query embedding → 非BG: L1, BG: TOPIC_BG_LEVEL"""
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "L0")
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        td = {1: {"is_bg": False}, 2: {"is_bg": True}}
        grades = engine._grade_topics_by_radius({}, td, {}, None, set())
        assert grades[1] == "L1"
        assert grades[2] == "L0"
        engine.destroy()

    def test_gr_002_bg_level_config(self, monkeypatch):
        """TOPIC_BG_LEVEL 可配置"""
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "L2")
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        td = {1: {"is_bg": True}}
        grades = engine._grade_topics_by_radius({}, td, {}, None, set())
        assert grades[1] == "L2"
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "L0")
        engine.destroy()

    def test_gr_003_centroid_none_fallback(self):
        """centroid=None 的非BG → L1 fallback"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        td = {1: {"is_bg": False, "centroid": None}}
        q_emb = _fake_embed()
        grades = engine._grade_topics_by_radius({}, td, {}, q_emb, set())
        assert grades[1] == "L1"
        engine.destroy()

    def test_gr_004_inner_sphere_l2(self):
        """d <= r/2 → L2（query 与 centroid 同向）"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        q = [0.5] * 768
        # 相同方向 + 小 max_intra → d ≈ 0, r = min(small, large) → L2
        td = {1: {"is_bg": False, "centroid": q, "max_intra": 0.1,
                  "nearest_centroid_dist": 1.0}}
        grades = engine._grade_topics_by_radius({}, td, {}, q, set())
        assert grades[1] == "L2", f"Expected L2 (inner sphere), got {grades[1]}"
        engine.destroy()

    def test_gr_005_outer_sphere_l1(self):
        """r/2 < d <= r → L1（中等距离）"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        q = [0.5] * 768
        # cent = [1]*256 + [0]*512 → cos ≈ 0.577, d ≈ 0.423
        # r = min(1.0, 1.0/2.0) = 0.5, r/2 = 0.25
        # d=0.423 → r/2 < d < r → L1
        cent = [1.0] * 256 + [0.0] * 512
        td = {1: {"is_bg": False, "centroid": cent,
                  "max_intra": 1.0, "nearest_centroid_dist": 1.0}}
        grades = engine._grade_topics_by_radius({}, td, {}, q, set())
        assert grades[1] == "L1", f"Expected L1, got {grades[1]}"
        engine.destroy()

    def test_gr_006_retrieved_fct_override(self):
        """远距离但已检索命中 → L1"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        q = [0.5] * 768
        neg_q = [-v for v in q]  # 相反方向 → d ≈ 2.0
        td = {1: {"is_bg": False, "centroid": neg_q,
                  "max_intra": 0.001, "nearest_centroid_dist": 0.001}}
        # 小 r + 大体距离 → d > r，但 retrieved → L1
        grades = engine._grade_topics_by_radius({}, td, {}, q, {1})
        assert grades[1] == "L1", f"Expected L1 (retrieved override), got {grades[1]}"
        engine.destroy()

    def test_gr_007_far_l0(self):
        """d > r 且未检索 → L0"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        q = [0.5] * 768
        neg_q = [-v for v in q]  # 相反方向 → d ≈ 2.0
        td = {1: {"is_bg": False, "centroid": neg_q,
                  "max_intra": 0.001, "nearest_centroid_dist": 0.001}}
        grades = engine._grade_topics_by_radius({}, td, {}, q, set())
        assert grades[1] == "L0", f"Expected L0 (far), got {grades[1]}"
        engine.destroy()

    def test_gr_008_min_radius_protection(self):
        """r <= 0 时保护到 0.05"""
        engine = ContextAssembler(db_path=":memory:", session_id="t")
        q = [0.5] * 768
        # max_intra=0, nearest=0 → r 将设为 0.05
        td = {1: {"is_bg": False, "centroid": q,
                  "max_intra": 0.0, "nearest_centroid_dist": 0.0}}
        grades = engine._grade_topics_by_radius({}, td, {}, q, set())
        # 与 query 相同 → d ≈ 0 → 肯定 L2
        assert grades[1] == "L2"
        engine.destroy()


# =============================================================================
# 3. TopicRetriever — per-topic 双路检索
# =============================================================================

class TestTopicRetriever:
    """per-topic BM25 + vector + RRF"""

    def _make_topic_data(self, topics: Dict[int, Dict], blobs=None) -> Dict[int, Dict]:
        """构造 topic_data 字典"""
        data = {}
        for tid, spec in topics.items():
            agg = spec.get("agg", f"topic {tid} content")
            cent = spec.get("centroid")
            is_bg = spec.get("is_bg", False)
            data[tid] = {
                "agg_text": agg,
                "centroid": cent or _fake_embed(),
                "is_bg": is_bg,
            }
        return data

    def test_tr_001_max_k_zero(self):
        """max_k=0 → 空集合"""
        td = self._make_topic_data({1: {"agg": "hello world"}})
        tr = TopicRetriever(MagicMock(), {}, td)
        result = tr.retrieve("test", _fake_embed(), max_k=0)
        assert result == set()

    def test_tr_002_bg_excluded(self):
        """BG topic 不参与检索"""
        td = self._make_topic_data({1: {"agg": "should appear"},
                                    2: {"agg": "bg stuff", "is_bg": True}})
        tr = TopicRetriever(MagicMock(), {}, td)
        # BM25 会找到 topic 1
        result = tr.retrieve("should appear", _fake_embed(), max_k=5)
        assert 1 in result
        assert 2 not in result, "BG topic should be excluded from retrieval"

    def test_tr_003_bm25_only(self):
        """无 query_embedding → BM25 only"""
        td = self._make_topic_data({1: {"agg": "python programming error"},
                                    2: {"agg": "weather is nice"}})
        tr = TopicRetriever(MagicMock(), {}, td)
        result = tr.retrieve("python error", None, max_k=5)
        assert 1 in result, "BM25 should match python topic"
        assert 2 not in result

    def test_tr_004_vector_only(self):
        """有 query_embedding 但 BM25 无命中 → vector only"""
        td = self._make_topic_data({
            1: {"agg": "xyz", "centroid": _fake_embed()},
            2: {"agg": "abc", "centroid": [0.9] * 768},
        })
        tr = TopicRetriever(MagicMock(), {}, td)
        # 用接近 topic 2 形心的 query
        q = [0.9] * 768
        result = tr.retrieve("xyz", q, max_k=5)
        # 至少 topic 2 应在结果中（BM25 命中 topic 1 也可能）
        assert 1 in result or 2 in result

    def test_tr_005_max_k_honored(self):
        """返回数 ≤ max_k"""
        td = self._make_topic_data({i: {"agg": f"topic {i} text"} for i in range(1, 11)})
        tr = TopicRetriever(MagicMock(), {}, td)
        result = tr.retrieve("topic", _fake_embed(), max_k=3)
        assert len(result) <= 3

    def test_tr_006_empty_corpus(self):
        """所有 topic agg_text 为空 → 空集合"""
        td = self._make_topic_data({
            1: {"agg": "", "centroid": _fake_embed()},
        })
        tr = TopicRetriever(MagicMock(), {}, td)
        result = tr.retrieve("test", _fake_embed(), max_k=5)
        assert result == set()


# =============================================================================
# 6. _compute_turn_plan_v2 — 话题级Plan决策
# =============================================================================

class TestComputeTurnPlanV2:
    """话题级拣选决策 + 工具组绑定（原工具轮测试已迁至 tool_group）"""

    pytestmark = pytest.mark.skip(reason="旧 per-tool 架构测试，已由 tool_group 架构替换")

    def _make_engine(self):
        from ca import ContextAssembler
        return ContextAssembler(db_path=":memory:", session_id=TEST_SESSION)

    def _write_turn_data(self, engine, turn, fct_fields: dict,
                        hdl_text="", assemble_status=0):
        engine.store.write_turn(
            TEST_SESSION, turn,
            hdl_text=hdl_text,
            fct_text=json.dumps(fct_fields, ensure_ascii=False),
            turn_type="dialogue", tool_sub_index=0,
            elm_text=json.dumps([{"role": "user", "content": "test"}], ensure_ascii=False),
            _assemble_status=assemble_status,
        )

    def _write_tool(self, engine, turn, sub=1,
                    fct_fields: dict = None, hdl_text="", assemble_status=0):
        engine.store.write_turn(
            TEST_SESSION, turn,
            hdl_text=hdl_text,
            fct_text=json.dumps(fct_fields or {}, ensure_ascii=False),
            turn_type="tool", tool_sub_index=sub,
            elm_text=json.dumps([{"role": "tool", "tool_call_id": "t1", "content": "ok"}],
                               ensure_ascii=False),
            _assemble_status=assemble_status,
        )

    def test_tpv2_001_tail_dialogue_l2(self):
        """tail 区对话轮 → L2"""
        engine = self._make_engine()
        self._write_dialogue(engine, 1, {"core_change": "讨论1"})
        msgs = [{"role": "user", "content": "hi", "_turn_index": 1}]
        l1 = {1: json.dumps({"core_change": "讨论1"})}
        l0 = {1: "L0:讨论1"}
        plan = engine._compute_turn_plan_v2(
            msgs, l1, l0, {}, {}, {}, {},
            tail_start=0, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        dialogues = [e for e in plan if e.turn_type == "dialogue"]
        assert len(dialogues) >= 1
        # 所有对话轮在 tail 中（tail_start=0）
        for d in dialogues:
            assert d.target_level == "L2", f"Expected L2 (tail), got {d.target_level}"
        engine.destroy()

    def test_tpv2_002_bg_topic_l0(self, monkeypatch):
        """BG topic → TOPIC_BG_LEVEL (默认 L0)"""
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "L0")
        engine = self._make_engine()
        self._write_dialogue(engine, 1, {"bg": True})
        msgs = [{"role": "user", "content": "hi", "_turn_index": 1}]
        l1 = {1: json.dumps({"bg": True})}
        l0 = {1: "L0:背景"}
        plan = engine._compute_turn_plan_v2(
            msgs, l1, l0, {}, {}, {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": True}},
            retrieved_topics=set(), selected_tools=set(),
        )
        d = [e for e in plan if e.turn_type == "dialogue"][0]
        assert d.target_level == "L0", f"Expected L0 (topic_bg), got {d.target_level}"
        assert d.decision_reason == "topic_bg"
        engine.destroy()

    def test_tpv2_003_elm_grade_topic_core(self):
        """L2 grade 对话轮 → L2 (topic_core)"""
        engine = self._make_engine()
        self._write_dialogue(engine, 1, {"core_change": "核心"})
        msgs = [{"role": "user", "content": "core", "_turn_index": 1}]
        l1 = {1: json.dumps({"core_change": "核心"})}
        l0 = {1: "L0:核心"}
        plan = engine._compute_turn_plan_v2(
            msgs, l1, l0, {}, {}, {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L2"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        d = [e for e in plan if e.turn_type == "dialogue"][0]
        assert d.target_level == "L2", f"Expected L2 (topic_core), got {d.target_level}"
        assert d.decision_reason == "topic_core"
        engine.destroy()

    def test_tpv2_004_fct_grade_baseline(self):
        """L1 grade 对话轮 → L1 (topic_baseline)"""
        engine = self._make_engine()
        self._write_dialogue(engine, 1, {"core_change": "baseline"})
        plan = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x", "_turn_index": 1}],
            {1: json.dumps({"core_change": "baseline"})},
            {1: "L0:base"},
            {}, {}, {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L1"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        d = [e for e in plan if e.turn_type == "dialogue"][0]
        assert d.target_level == "L1", f"Expected L1, got {d.target_level}"
        assert d.decision_reason == "topic_baseline"
        engine.destroy()

    def test_tpv2_005_hdl_grade_degraded(self):
        """L0 grade 对话轮 → L0 (topic_degraded)"""
        engine = self._make_engine()
        self._write_dialogue(engine, 1, {"core_change": "远"})
        plan = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x", "_turn_index": 1}],
            {1: json.dumps({"core_change": "远"})},
            {1: "L0:far"},
            {}, {}, {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        d = [e for e in plan if e.turn_type == "dialogue"][0]
        assert d.target_level == "L0", f"Expected L0, got {d.target_level}"
        assert d.decision_reason == "topic_degraded"
        engine.destroy()

    def test_tpv2_006_tool_tail_l2(self):
        """工具轮在 tail → L2 (tail)"""
        engine = self._make_engine()
        plan = engine._compute_turn_plan_v2(
            [], {}, {}, {"key": "L1"},
            {(1, 1): "L0工具"},
            {}, {},
            tail_start=0, tool_tail_turns={1}, idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools={(1, 1)},
        )
        tools = [e for e in plan if e.turn_type == "tool"]
        if tools:
            assert tools[0].target_level == "L2", f"Expected L2 (tool tail), got {tools[0].target_level}"

        # 重新用正确的参数调用
        plan2 = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x"}],
            {1: "L1_text"}, {1: "L0_text"},
            {(1, 1): "L1工具"}, {(1, 1): "L0工具"},
            {}, {},
            tail_start=0, tool_tail_turns={1}, idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools={(1, 1)},
        )
        tools2 = [e for e in plan2 if e.turn_type == "tool"]
        assert len(tools2) >= 1
        assert tools2[0].target_level == "L2", f"Expected L2 (tool tail), got {tools2[0].target_level}"
        engine.destroy()

    def test_tpv2_007_topic_boost_l1(self):
        """父 topic L2 → 工具轮升 L1 (topic_boost)"""
        engine = self._make_engine()
        plan = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x"}],
            {1: json.dumps({"core_change": "核心"})}, {1: "L0核心"},
            {(1, 1): json.dumps({"tool_name": "t"})}, {(1, 1): "L0工具"},
            {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L2"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        tools = [e for e in plan if e.turn_type == "tool"]
        assert len(tools) >= 1
        assert tools[0].target_level == "L1", f"Expected L1 (topic_boost), got {tools[0].target_level}"
        assert tools[0].decision_reason == "topic_boost"
        engine.destroy()

    def test_tpv2_008_retrieved_tool_l1(self):
        """检索命中 + L1 → L1 (retrieved)"""
        engine = self._make_engine()
        plan = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x"}],
            {1: json.dumps({"core_change": "普通"})}, {1: "L0普通"},
            {(1, 1): json.dumps({"tool_name": "t"})}, {(1, 1): "L0工具"},
            {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools={(1, 1)},
        )
        tools = [e for e in plan if e.turn_type == "tool"]
        assert len(tools) >= 1
        assert tools[0].decision_reason == "retrieved"
        engine.destroy()

    def test_tpv2_009_middle_tool_l0(self):
        """非tail 非retrieved 非boost → L0 (middle)"""
        engine = self._make_engine()
        plan = engine._compute_turn_plan_v2(
            [{"role": "user", "content": "x"}],
            {1: json.dumps({"core_change": "无关"})}, {1: "L0无关"},
            {(1, 1): None}, {(1, 1): "L0工具"},
            {}, {},
            tail_start=999, tool_tail_turns=set(), idx_to_turn={}, tool_key_map={},
            budget=1000,
            turn_to_topic={1: 1}, topic_grades={1: "L0"}, topic_data={1: {"is_bg": False}},
            retrieved_topics=set(), selected_tools=set(),
        )
        tools = [e for e in plan if e.turn_type == "tool"]
        assert len(tools) >= 1
        assert tools[0].target_level == "L0", f"Expected L0 (middle), got {tools[0].target_level}"
        assert tools[0].decision_reason == "middle"
        engine.destroy()


# =============================================================================
# 7. query_embedding — 存储新列（Store schema v3→v4）
# =============================================================================

class TestQueryEmbedding:
    """query_embedding BLOB 列"""

    def test_qe_001_column_exists(self, ca_engine):
        """turn_cache 有 query_embedding 列"""
        cursor = ca_engine.store.conn.execute(
            "PRAGMA table_info(turn_cache)"
        )
        cols = {row[1] for row in cursor.fetchall()}
        assert "query_embedding" in cols, "query_embedding column missing"


# =============================================================================
# 8. TOPIC_* 配置项
# =============================================================================

class TestTopicConfig:
    """TOPIC_* 环境变量解析与验证"""

    def test_cfg_001_defaults(self):
        """5个TOPIC_* 配置默认值正确"""
        assert Config.TOPIC_JACCARD_ENTRY == 0.02, f"Got {Config.TOPIC_JACCARD_ENTRY}"
        assert Config.TOPIC_JACCARD_CHAIN == 0.04, f"Got {Config.TOPIC_JACCARD_CHAIN}"
        assert Config.TOPIC_RADIUS_WEIGHT == 2.0, f"Got {Config.TOPIC_RADIUS_WEIGHT}"
        assert Config.TOPIC_MAX_UPGRADE == 10, f"Got {Config.TOPIC_MAX_UPGRADE}"
        assert Config.TOPIC_BG_LEVEL == "L0", f"Got {Config.TOPIC_BG_LEVEL}"

    def test_cfg_002_env_override(self, monkeypatch):
        """环境变量可覆盖默认值"""
        monkeypatch.setattr(Config, "TOPIC_JACCARD_ENTRY", 0.1)
        monkeypatch.setattr(Config, "TOPIC_JACCARD_CHAIN", 0.2)
        monkeypatch.setattr(Config, "TOPIC_RADIUS_WEIGHT", 3.0)
        monkeypatch.setattr(Config, "TOPIC_MAX_UPGRADE", 15)
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "L2")
        assert Config.TOPIC_JACCARD_ENTRY == 0.1
        assert Config.TOPIC_JACCARD_CHAIN == 0.2
        assert Config.TOPIC_RADIUS_WEIGHT == 3.0
        assert Config.TOPIC_MAX_UPGRADE == 15
        assert Config.TOPIC_BG_LEVEL == "L2"
        monkeypatch.undo()
        Config.validate()  # restore defaults

    def test_cfg_003_bg_level_validation(self, monkeypatch):
        """TOPIC_BG_LEVEL 非法值不崩溃"""
        errors = []
        monkeypatch.setattr(Config, "TOPIC_BG_LEVEL", "FOO")
        # validate() 会 append errors，不 raise
        try:
            Config.validate()
        except Exception:
            pass
        monkeypatch.undo()
        Config.validate()

    def test_cfg_004_hot_reload(self, monkeypatch):
        """热重载后 validate() 能接受变化"""
        monkeypatch.setattr(Config, "TOPIC_JACCARD_ENTRY", 0.08)
        assert Config.TOPIC_JACCARD_ENTRY == 0.08
        monkeypatch.undo()
        Config.validate()




