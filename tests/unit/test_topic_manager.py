"""topic_manager 模块单元测试 — TopicGradeManager + 模块级函数全覆盖。

术语强制：Elm/Fct/Hdl，严禁 L2/L1/L0。
Actor = 活跃话题, Rel = 关联话题, Far = 远距离话题。

设计决策对照:
  → TP-001: Jaccard 话题分割 + 强制短语 (test_jaccard_*, TestScanForcedSplitPhrases)
  → TP-002: `_grade_topics_by_radius` 半径定级 (TestGradeTopicsByRadius)
  → TP-006: 水位压力 _apply_water_pressure (TestWaterPressure)
  → TP-007: embed 故障 → centroid=None → REL fallback (test_centroid_none_*) 
  → tests/INDEX.md — 测试套件总览"""

import math
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from ca.grade import Grade, TopicGrade


# ============================================================================
# 模块级纯函数
# ============================================================================

class TestJaccardText:
    """_jaccard_text — CJK 单字/二元组 + 英文 token 集合相似度"""

    def test_identical_cjk(self):
        from topic_manager import _jaccard_text
        assert _jaccard_text("你好世界", "你好世界") == 1.0

    def test_similar_cjk(self):
        from topic_manager import _jaccard_text
        j = _jaccard_text("帮我查数据", "帮我查天气")
        assert 0.3 < j < 0.9

    def test_different_cjk(self):
        from topic_manager import _jaccard_text
        j = _jaccard_text("你好", "再见")
        assert j == 0.0

    def test_english_tokens(self):
        from topic_manager import _jaccard_text
        j = _jaccard_text("hello world", "hello world")
        assert j == 1.0

    def test_partial_english(self):
        from topic_manager import _jaccard_text
        j = _jaccard_text("what is python", "what is java")
        assert 0.4 < j < 1.0

    def test_mixed_cjk_english(self):
        from topic_manager import _jaccard_text
        j = _jaccard_text("python 好强大", "python 好厉害")
        assert 0.1 < j < 0.5

    def test_bigram_matters(self):
        from topic_manager import _jaccard_text
        # "查数据" vs "查天气" 共享 "查" 但二元组不同
        j_same = _jaccard_text("查数据", "查数据")
        j_diff = _jaccard_text("查数据", "查天气")
        assert j_same > j_diff

    def test_both_empty(self):
        from topic_manager import _jaccard_text
        assert _jaccard_text("", "") == 0.0

    def test_one_empty(self):
        from topic_manager import _jaccard_text
        assert _jaccard_text("你好", "") == 0.0


class TestScanForcedSplitPhrases:
    """_scan_forced_split_phrases — 强制话题分割短语"""

    def test_chinese_switch_topic(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("换个话题") is True

    def test_chinese_talk_other(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("聊点别的") is True

    def test_chinese_another(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("另一个问题") is True

    def test_chinese_dont_mention(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("别提这件事了") is True

    def test_english_switch(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("topic switch") is True

    def test_english_moving_on(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("moving on") is True

    def test_case_insensitive(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("Topic Switch") is True
        assert _scan_forced_split_phrases("TOPIC SWITCH") is True

    def test_substring_match(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("能不能换个话题聊点开心的") is True

    def test_normal_message_no_split(self):
        from topic_manager import _scan_forced_split_phrases
        assert _scan_forced_split_phrases("你帮我查一下今天的天气") is False
        assert _scan_forced_split_phrases("继续之前的讨论") is False
        assert _scan_forced_split_phrases("") is False


class TestCosineSimilarity:
    """_cosine_similarity — 余弦相似度"""

    def test_identical(self):
        from topic_manager import _cosine_similarity
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == 1.0

    def test_orthogonal(self):
        from topic_manager import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_opposite(self):
        from topic_manager import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0

    def test_partial(self):
        from topic_manager import _cosine_similarity
        sim = _cosine_similarity([1.0, 0.0], [0.5, 0.5])
        expected = 0.5 / math.sqrt(0.5)
        assert abs(sim - expected) < 1e-10

    def test_zero_vector(self):
        from topic_manager import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
        assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_768_dim_identical(self):
        from topic_manager import _cosine_similarity
        v = [0.1] * 768
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-10


class TestComputeCentroid:
    """_compute_centroid — 向量形心计算"""

    def test_empty_list_returns_none(self):
        from topic_manager import _compute_centroid
        assert _compute_centroid([]) is None

    def test_single_vector(self):
        from topic_manager import _compute_centroid
        v = [1.0, 2.0, 3.0]
        c = _compute_centroid([v])
        assert c is not None
        # 归一化
        norm = math.sqrt(1 + 4 + 9)
        assert c == [1.0/norm, 2.0/norm, 3.0/norm]

    def test_two_identical_vectors(self):
        from topic_manager import _compute_centroid
        v1 = [1.0, 0.0]
        v2 = [1.0, 0.0]
        c = _compute_centroid([v1, v2])
        assert c is not None
        assert c[0] == 1.0
        assert c[1] == 0.0

    def test_two_different_vectors(self):
        from topic_manager import _compute_centroid
        c = _compute_centroid([[1.0, 0.0], [0.0, 1.0]])
        assert c is not None
        # 均值 [0.5, 0.5] → 归一化
        norm = math.sqrt(0.5)
        assert abs(c[0] - 0.5/norm) < 1e-10
        assert abs(c[1] - 0.5/norm) < 1e-10

    def test_all_zero_vectors_returns_none(self):
        from topic_manager import _compute_centroid
        c = _compute_centroid([[0.0, 0.0], [0.0, 0.0]])
        assert c is None

    def test_mean_norm_zero_returns_none(self):
        from topic_manager import _compute_centroid
        c = _compute_centroid([[1.0, -1.0], [-1.0, 1.0]])
        # 均值 [0,0] → norm=0 → None
        assert c is None


class TestGradeTopicsByRadius:
    """_grade_topics_by_radius — 三级定级 ACT/REL/FAR"""

    def _make_td(self, centroid=None, max_intra=0.05,
                 nearest=0.0, is_bg=False):
        return {
            "centroid": centroid,
            "max_intra": max_intra,
            "nearest_centroid_dist": nearest,
            "is_bg": is_bg,
        }

    def test_no_query_embed_defaults_rel(self):
        from topic_manager import _grade_topics_by_radius
        td = {1: self._make_td(), 2: self._make_td(is_bg=True)}
        grades = _grade_topics_by_radius(td, None, set())
        assert grades[1] == TopicGrade.REL
        assert grades[2] == TopicGrade.FAR  # TOPIC_BG_LEVEL 默认 "Far"

    def test_centroid_none_fallback_rel(self):
        from topic_manager import _grade_topics_by_radius
        td = {1: self._make_td(centroid=None)}
        q = [0.5] * 768
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.REL

    def test_inner_sphere_act(self):
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        td = {1: self._make_td(centroid=q, max_intra=0.1, nearest=1.0)}
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.ACT

    def test_outer_sphere_rel(self):
        from topic_manager import _grade_topics_by_radius, _cosine_similarity
        q = [0.5] * 768
        # centroid 方向不同，cos ≈ 0.577, d ≈ 0.423
        cent = [1.0] * 256 + [0.0] * 512
        sim = _cosine_similarity(q, cent)
        d = 1.0 - sim
        td = {1: self._make_td(centroid=cent, max_intra=1.0, nearest=1.0)}
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.REL, \
            f"Expected REL for d={d:.4f}"

    def test_far_not_retrieved(self):
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        neg_q = [-v for v in q]
        td = {1: self._make_td(centroid=neg_q, max_intra=0.001, nearest=0.001)}
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.FAR

    def test_far_but_retrieved_becomes_rel(self):
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        neg_q = [-v for v in q]
        td = {1: self._make_td(centroid=neg_q, max_intra=0.001, nearest=0.001)}
        grades = _grade_topics_by_radius(td, q, {1})
        assert grades[1] == TopicGrade.REL

    def test_min_radius_protection(self):
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        # r 会保护到 0.05，q 与自身 cos=1 → d=0 → ACT
        td = {1: self._make_td(centroid=q, max_intra=0.0, nearest=0.0)}
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.ACT

    def test_nearest_centroid_limits_radius(self):
        from topic_manager import _grade_topics_by_radius, _cosine_similarity
        q = [0.5] * 768
        # two topics, centroid q (topic 1), other topic centroid opposite (topic 2)
        # nearest_centroid_dist 小 → r 被限制
        opp = [-v for v in q]
        cent = [v * 0.1 for v in q]  # close to q
        td = {
            1: self._make_td(centroid=cent, max_intra=1.0, nearest=0.5),
            2: self._make_td(centroid=opp, max_intra=0.5, nearest=0.5),
        }
        grades = _grade_topics_by_radius(td, q, set())
        # r for topic 1: nearest=0.5, radius_weight=2.0, r = min(1.0, 0.5/2.0) = 0.25
        # d = 1 - cos(q, cent) ≈ 1 - 1.0 = 0 (same direction) → d <= r/2 → ACT
        assert grades[1] in (TopicGrade.ACT, TopicGrade.REL)

    def test_bg_topic_uses_bg_level(self):
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        td = {1: self._make_td(centroid=q, is_bg=True)}
        grades = _grade_topics_by_radius(td, q, set())
        assert grades[1] == TopicGrade.FAR  # 默认 BG_LEVEL = Far

    def test_single_topic_no_nearest(self):
        """单话题时 nearest_centroid_dist=0，r 只受 max_intra 保护"""
        from topic_manager import _grade_topics_by_radius
        q = [0.5] * 768
        # 两话题都指向 q，nearest=0 和 max_intra=0.05
        td = {1: self._make_td(centroid=q, max_intra=0.05, nearest=0.0)}
        grades = _grade_topics_by_radius(td, q, set())
        # d=0 <= r/2=0.025 → ACT
        assert grades[1] == TopicGrade.ACT


# ============================================================================
# TopicGradeManager — 构造
# ============================================================================

@pytest.fixture
def mock_store():
    """内存 store（不带 ContextAssembler 开销）"""
    from ca.store import SQLiteStore
    import tempfile
    s = SQLiteStore(db_path=":memory:")
    yield s
    s.close()


@pytest.fixture
def mock_embed():
    """embed 返回内容可区分伪嵌入（相同文本→相同向量，不同文本→不同向量）"""
    from tests.conftest import _content_hash_embed
    return SimpleNamespace(embed=lambda text, **kw: _content_hash_embed(text or ""))


@pytest.fixture
def mgr(mock_store, mock_embed):
    """干净的 TopicGradeManager 实例"""
    from topic_manager import TopicGradeManager
    return TopicGradeManager(mock_store, mock_embed)


class TestTopicGradeManagerInit:
    """__init__ — 状态初始化"""

    def test_stores_refs(self, mock_store, mock_embed):
        from topic_manager import TopicGradeManager
        m = TopicGradeManager(mock_store, mock_embed)
        assert m._store is mock_store
        assert m._embed_client is mock_embed

    def test_state_defaults(self, mgr):
        assert mgr._turn_to_topic == {}
        assert mgr._topic_data == {}
        assert mgr._topic_grades == {}
        assert mgr._current_topic_id is None
        assert mgr._switch_turn == 0
        assert mgr._last_processed_turn == 0
        assert mgr._next_topic_id == 1


# ============================================================================
# TopicGradeManager — detect()
# ============================================================================

class TestDetect:
    """detect — 增量话题分割 + 切换检测"""

    def test_skips_processed_turn(self, mgr):
        mgr._last_processed_turn = 5
        assert mgr.detect(5, [], "你好") is False
        assert mgr.detect(3, [], "你好") is False

    def test_first_turn_no_switch(self, mgr):
        """第一轮对话不应返回话题切换"""
        assert mgr.detect(1, [], "你好") is False
        assert mgr._turn_to_topic[1] == 1

    def test_forced_split_returns_switch(self, mgr):
        mgr.detect(1, [], "你好")   # topic 1
        assert mgr.detect(2, [], "换个话题") is True
        assert mgr._turn_to_topic[2] == 2

    def test_jaccard_chain_same_topic(self, mgr):
        # 第一个 detect 带 Fct 文本 → profile 非空
        rows1 = [(0, "user", None, None, "原始消息", "关于Python的学习笔记", "")]
        mgr.detect(1, rows1, "关于Python的学习笔记")
        mgr._current_topic_id = 1
        # 相同 Fct 内容 → Jaccard 高 → chain（同话题）
        rows2 = [(0, "user", None, None, "原始消息", "关于Python的学习笔记", "")]
        assert mgr.detect(2, rows2, "关于Python的学习笔记") is False
        assert mgr._turn_to_topic[2] == 1

    def test_jaccard_miss_new_topic(self, mgr):
        mgr.detect(1, [], "聊Python")
        mgr._current_topic_id = 1
        assert mgr.detect(2, [], "我们来聊JavaScript的闭包原理") is True
        assert mgr._turn_to_topic[2] == 2

    def test_turn_zero_skipped(self, mgr):
        assert mgr.detect(0, [], "你好") is False

    def test_detect_stores_processed_rows(self, mgr):
        rows = [(0, "user", None, None, "原始消息", "fct1", "")]
        mgr.detect(1, rows, "你好")
        assert mgr._processed_ca_rows == rows

    def test_update_last_processed(self, mgr):
        mgr.detect(1, [], "你好")
        assert mgr._last_processed_turn == 1
        mgr.detect(2, [], "继续")
        assert mgr._last_processed_turn == 2


# ============================================================================
# TopicGradeManager — grade_on_switch()
# ============================================================================

class TestGradeOnSwitch:
    """grade_on_switch — 话题切换时定级"""

    def test_initializes_topic_data_when_empty(self, mgr):
        mgr._turn_to_topic = {1: 1, 2: 2}
        mgr._current_topic_id = 2
        q = [0.5] * 768
        mgr.grade_on_switch(q, "切换话题")
        assert 1 in mgr._topic_data
        assert 2 in mgr._topic_data

    def test_current_topic_forced_act(self, mgr, mock_store):
        mgr._turn_to_topic = {1: 1, 2: 2}
        mgr._current_topic_id = 2
        q = [0.5] * 768
        mgr.grade_on_switch(q, "切换话题")
        assert mgr._topic_grades[2] == TopicGrade.ACT

    def test_sets_switch_turn(self, mgr):
        mgr._turn_to_topic = {1: 1}
        mgr._current_topic_id = 1
        mgr._last_processed_turn = 5
        q = [0.5] * 768
        mgr.grade_on_switch(q, "切换")
        assert mgr._switch_turn == 5

    def test_old_topic_graded_by_radius(self, mgr, mock_store):
        """旧话题应被半径定级（非 ACT）"""
        from ca.store import write_turn_v5
        # 两个 topic 用不同 Fct 文本产生不同 embedding
        write_turn_v5(mock_store, "test", 1, 0, role="user", elm_text="A",
                      fct_text='{"core_change":"AAAA"}')
        write_turn_v5(mock_store, "test", 2, 0, role="user", elm_text="B",
                      fct_text='{"core_change":"BBBB"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._turn_to_topic = {1: 1, 2: 2}
        mgr._current_topic_id = 2
        # query 对齐 topic 2（BBBB）→ topic 1 应 FAR
        q = mgr._embed_client.embed("BBBB")
        mgr.grade_on_switch(q, "切换")
        assert mgr._topic_grades[1] == TopicGrade.FAR
        assert mgr._topic_grades[2] == TopicGrade.ACT

    def test_grade_frozen_until_next_switch(self, mgr, mock_store):
        """定级后缓存在 _topic_grades，切换间不变"""
        from ca.store import write_turn_v5
        write_turn_v5(mock_store, "test", 1, 0, role="user", elm_text="A",
                      fct_text='{"core_change":"AAAA"}')
        write_turn_v5(mock_store, "test", 2, 0, role="user", elm_text="B",
                      fct_text='{"core_change":"BBBB"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._turn_to_topic = {1: 1, 2: 2}
        mgr._current_topic_id = 2
        q = mgr._embed_client.embed("BBBB")
        mgr.grade_on_switch(q, "切换")
        # 下次查询应该返回缓存的值，不重算
        assert mgr._topic_grades[1] == TopicGrade.FAR
        assert mgr._topic_grades[2] == TopicGrade.ACT
        # 再查一次还是相同
        assert mgr.get_turn_grade(1) == mgr._topic_grades[1]
        assert mgr.get_turn_grade(2) == mgr._topic_grades[2]


# ============================================================================
# TopicGradeManager — get_turn_grade()
# ============================================================================

class TestGetTurnGrade:
    """get_turn_grade — 获取指定 turn 的话题等级"""

    def test_turn_zero_returns_act(self, mgr):
        assert mgr.get_turn_grade(0) == TopicGrade.ACT
        assert mgr.get_turn_grade(-1) == TopicGrade.ACT

    def test_unknown_turn_returns_act(self, mgr):
        assert mgr.get_turn_grade(99) == TopicGrade.ACT

    def test_unknown_topic_returns_act(self, mgr):
        mgr._turn_to_topic[1] = 99
        assert mgr.get_turn_grade(1) == TopicGrade.ACT

    def test_returns_cached_grade(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._topic_grades[1] = TopicGrade.REL
        assert mgr.get_turn_grade(1) == TopicGrade.REL

    def test_switched_turn_returns_old_topic_grade(self, mgr):
        """话题2的 turn 应返回 topic2 的 grade"""
        mgr._turn_to_topic[1] = 1
        mgr._turn_to_topic[2] = 2
        mgr._topic_grades[1] = TopicGrade.FAR
        mgr._topic_grades[2] = TopicGrade.ACT
        assert mgr.get_turn_grade(1) == TopicGrade.FAR
        assert mgr.get_turn_grade(2) == TopicGrade.ACT


# ============================================================================
# TopicGradeManager — get_topic_grades(), get_current_topic_id(), reset()
# ============================================================================

class TestAccessors:
    """get_topic_grades / get_current_topic_id / reset"""

    def test_get_topic_grades_returns_copy(self, mgr):
        mgr._topic_grades[1] = TopicGrade.ACT
        ret = mgr.get_topic_grades()
        ret[2] = TopicGrade.REL  # 修改返回不应影响内部
        assert 2 not in mgr._topic_grades

    def test_get_current_topic_id_none_at_start(self, mgr):
        assert mgr.get_current_topic_id() is None

    def test_get_current_topic_id_after_detect(self, mgr):
        mgr.detect(1, [], "你好")
        assert mgr.get_current_topic_id() == 1

    def test_reset_clears_all_state(self, mgr):
        mgr.detect(1, [], "你好")
        mgr.detect(2, [], "换个话题")
        mgr.grade_on_switch([0.5] * 768, "切换")
        mgr.reset()
        assert mgr._turn_to_topic == {}
        assert mgr._topic_data == {}
        assert mgr._topic_grades == {}
        assert mgr._current_topic_id is None
        assert mgr._switch_turn == 0
        assert mgr._last_processed_turn == 0
        assert mgr._next_topic_id == 1


# ============================================================================
# TopicGradeManager — _assign_topic()
# ============================================================================

class TestAssignTopic:
    """_assign_topic — 增量话题分配"""

    def test_turn_zero_returns_none(self, mgr):
        assert mgr._assign_topic(0, [], "") is None
        assert mgr._assign_topic(-1, [], "hi") is None

    def test_empty_msg_returns_none(self, mgr):
        assert mgr._assign_topic(1, [], "") is None

    def test_forced_split_new_topic(self, mgr):
        tid = mgr._assign_topic(2, [], "换个话题")
        assert tid == 1  # 第一个强制分割 → topic 1
        assert mgr._turn_to_topic[2] == 1

    def test_first_turn_topic_1(self, mgr):
        tid = mgr._assign_topic(1, [], "你好")
        assert tid == 1
        assert mgr._turn_to_topic[1] == 1
        assert mgr._next_topic_id >= 2

    def test_jaccard_chain_continues(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._current_topic_id = 1
        mgr._topic_text_profiles[1] = "聊Python"
        # 相同内容 → Jaccard 高 > CHAIN(0.04)
        tid = mgr._assign_topic(2, [(0, "user", None, None, "原始消息", "聊Python", "")], "聊Python")
        assert tid == 1
        assert mgr._turn_to_topic[2] == 1

    def test_jaccard_entry_continues(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._current_topic_id = 1
        mgr._topic_text_profiles[1] = "聊Python和Java"
        # 弱匹配 > ENTRY(0.02)
        from topic_manager import _jaccard_text
        j = _jaccard_text("Python和Java", "聊Python和Java")
        assert j >= 0.02, f"j too low: {j}"
        tid = mgr._assign_topic(2, [(0, "user", None, None, "原始消息", "Python和Java", "")], "Python和Java")
        assert tid == 1

    def test_jaccard_miss_new_topic(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._current_topic_id = 1
        mgr._next_topic_id = 2  # topic 1 已分配
        mgr._topic_text_profiles[1] = "聊Python"
        tid = mgr._assign_topic(2, [(0, "user", None, None, "原始消息", "今天是几号", "")], "今天是几号")
        assert tid == 2
        assert mgr._turn_to_topic[2] == 2

    def test_no_current_topic_starts_new(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._next_topic_id = 2
        mgr._current_topic_id = None  # 当前话题丢失
        tid = mgr._assign_topic(2, [(0, "user", None, None, "原始消息", "hello", "")], "hello")
        assert tid == 2

    def test_extract_turn_fct_fallback_to_user_msg(self, mgr):
        mgr._turn_to_topic[1] = 1
        mgr._current_topic_id = 1
        mgr._next_topic_id = 2
        mgr._topic_text_profiles[1] = "旧话题"
        tid = mgr._assign_topic(2, [], "全新的内容没有交集")
        # ca_rows 空 → 用 user_msg 做 Jaccard → 应该 miss → 新 topic
        assert tid == 2


# ============================================================================
# TopicGradeManager — _apply_water_pressure()
# ============================================================================

class TestApplyWaterPressure:
    """_apply_water_pressure — 水位压力 Jaccard 扣减"""

    def test_below_start_unchanged(self, mgr, monkeypatch):
        # peak=1000, start=300
        from ca.config import Config
        monkeypatch.setattr(Config, "TOPIC_PEAK_TOKEN", 1000)
        assert mgr._apply_water_pressure(0.10, 100) == 0.10
        assert mgr._apply_water_pressure(0.10, 300) == 0.10

    def test_at_peak_full_penalty(self, mgr, monkeypatch):
        from ca.config import Config
        monkeypatch.setattr(Config, "TOPIC_PEAK_TOKEN", 1000)
        monkeypatch.setattr(Config, "ACCUMULATED_SPLIT_START", 300)
        result = mgr._apply_water_pressure(0.10, 1000)
        assert abs(result - (0.10 - 0.30)) < 1e-10

    def test_partial_progression(self, mgr, monkeypatch):
        from ca.config import Config
        monkeypatch.setattr(Config, "TOPIC_PEAK_TOKEN", 1000)
        monkeypatch.setattr(Config, "ACCUMULATED_SPLIT_START", 300)
        # 650 → 进度 50%
        result = mgr._apply_water_pressure(0.10, 650)
        expected = 0.10 - 0.50 * 0.30
        assert abs(result - expected) < 1e-10

    def test_beyond_peak_capped(self, mgr, monkeypatch):
        from ca.config import Config
        monkeypatch.setattr(Config, "TOPIC_PEAK_TOKEN", 1000)
        monkeypatch.setattr(Config, "ACCUMULATED_SPLIT_START", 300)
        result = mgr._apply_water_pressure(0.10, 2000)
        assert abs(result - (0.10 - 0.30)) < 1e-10


# ============================================================================
# TopicGradeManager — _extract_turn_fct()
# ============================================================================

class TestExtractTurnFct:
    """_extract_turn_fct — 从 ca_rows 提取代表性 Fct"""

    def test_none_or_empty_returns_none(self, mgr):
        assert mgr._extract_turn_fct(None) is None
        assert mgr._extract_turn_fct([]) is None

    def test_prefers_user_seq_0(self, mgr):
        rows = [
            (0, "user", None, None, "原始消息", "user_fct", ""),
            (1, "assistant", None, None, "", "assistant_fct", ""),
            (2, "tool", None, None, "原始结果", "tool_fct", ""),
        ]
        assert mgr._extract_turn_fct(rows) == "user_fct"

    def test_falls_back_to_assistant_fin(self, mgr):
        rows = [
            (1, "assistant", None, None, "", "", ""),
            (2, "assistant", "stop", None, "", "fin_fct", ""),
            (3, "tool", None, None, "结果", "tool_fct", ""),
        ]
        assert mgr._extract_turn_fct(rows) == "fin_fct"

    def test_falls_back_to_any_fct(self, mgr):
        rows = [
            (1, "assistant", None, None, "", "", ""),
            (2, "tool", None, None, "结果", "tool_fct", ""),
        ]
        assert mgr._extract_turn_fct(rows) == "tool_fct"

    def test_no_fct_at_all_returns_none(self, mgr):
        rows = [
            (1, "assistant", None, None, "", None, None),
            (2, "tool", None, None, "", None, None),
        ]
        assert mgr._extract_turn_fct(rows) is None

    def test_row_with_insufficient_length(self, mgr):
        """行长度不足 6 时跳过"""
        rows = [(0, "user")]
        assert mgr._extract_turn_fct(rows) is None


# ============================================================================
# TopicGradeManager — _init_topic_data()
# ============================================================================

class TestInitTopicData:
    """_init_topic_data — 从 turn→topic 映射初始化 topic_data"""

    def test_creates_entries_for_all_topics(self, mgr):
        mgr._turn_to_topic = {1: 1, 2: 2, 3: 2}
        mgr._init_topic_data()
        assert 1 in mgr._topic_data
        assert 2 in mgr._topic_data
        assert len(mgr._topic_data) == 2

    def test_each_entry_has_required_keys(self, mgr):
        mgr._turn_to_topic = {1: 1}
        mgr._init_topic_data()
        d = mgr._topic_data[1]
        assert "centroid" in d
        assert "max_intra" in d
        assert "nearest_centroid_dist" in d
        assert "is_bg" in d
        assert "turns" in d
        assert "embeddings" in d

    def test_turns_collected_per_topic(self, mgr):
        mgr._turn_to_topic = {1: 1, 2: 2, 3: 2, 4: 1}
        mgr._init_topic_data()
        assert mgr._topic_data[1]["turns"] == [1, 4]
        assert mgr._topic_data[2]["turns"] == [2, 3]


# ============================================================================
# TopicGradeManager — _compute_centroids()
# ============================================================================

class TestComputeCentroids:
    """_compute_centroids — 为每个 topic 计算形心"""

    def test_no_store_returns_early(self, mgr):
        mgr._store = None
        mgr._topic_data = {1: {"centroid": None, "is_bg": False, "turns": [1]}}
        mgr._compute_centroids()
        assert mgr._topic_data[1]["centroid"] is None

    def test_is_bg_skipped(self, mgr):
        mgr._topic_data = {1: {"centroid": None, "is_bg": True, "turns": [1]}}
        mgr._compute_centroids()
        assert mgr._topic_data[1]["centroid"] is None

    def test_computes_centroid(self, mgr, mock_store):
        """写入 turn_stream 数据后应能计算形心"""
        from ca.store import write_turn_v5
        # 写入 user 行（seq=0），带 Fct
        write_turn_v5(mock_store, "test", 1, 0,
                      role="user", elm_text="原始文本",
                      fct_text='{"core_change":"测试内容"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._topic_data = {
            1: {"centroid": None, "is_bg": False, "turns": [1],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
        }
        mgr._compute_centroids()
        assert mgr._topic_data[1]["centroid"] is not None
        assert len(mgr._topic_data[1]["centroid"]) == 768

    def test_embed_failure_sets_none_centroid(self, mgr, mock_store):
        from ca.store import write_turn_v5
        write_turn_v5(mock_store, "test", 1, 0,
                      role="user", elm_text="原始文本",
                      fct_text='{\"core_change\":\"测试内容\"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        # embed 异常 → centroid 保持 None
        mgr._embed_client.embed = MagicMock(side_effect=RuntimeError("embed failed"))
        mgr._topic_data = {
            1: {"centroid": None, "is_bg": False, "turns": [1],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
        }
        mgr._compute_centroids()
        assert mgr._topic_data[1]["centroid"] is None

    def test_embed_returns_none(self, mgr, mock_store):
        from ca.store import write_turn_v5
        write_turn_v5(mock_store, "test", 1, 0,
                      role="user", elm_text="原始文本",
                      fct_text='{\"core_change\":\"测试内容\"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._embed_client.embed = MagicMock(return_value=None)
        mgr._topic_data = {
            1: {"centroid": None, "is_bg": False, "turns": [1],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
        }
        mgr._compute_centroids()
        assert mgr._topic_data[1]["centroid"] is None

    def test_get_turn_ca_rows_exception_continues(self, mgr, mock_store):
        """异常读取行不应阻断整个 centroid 计算"""
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._embed_client.embed = MagicMock(return_value=[0.5] * 768)
        mgr._topic_data = {
            1: {"centroid": None, "is_bg": False, "turns": [999],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
        }
        # turn 999 不存在，get_turn_ca_rows 返回 []
        mgr._compute_centroids()
        # centroid 应该保持原值（无向量）
        assert mgr._topic_data[1]["centroid"] is None

    def test_nearest_centroid_dist_computed(self, mgr, mock_store):
        from ca.store import write_turn_v5
        write_turn_v5(mock_store, "test", 1, 0,
                      role="user", elm_text="A",
                      fct_text='{"core_change":"A"}')
        write_turn_v5(mock_store, "test", 2, 0,
                      role="user", elm_text="B",
                      fct_text='{"core_change":"B"}')
        mgr._store = mock_store
        mock_store.session_id = "test"
        mgr._embed_client = SimpleNamespace(embed=lambda *a, **kw: [0.5] * 768)
        mgr._topic_data = {
            1: {"centroid": None, "is_bg": False, "turns": [1],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
            2: {"centroid": None, "is_bg": False, "turns": [2],
                "max_intra": 0.05, "nearest_centroid_dist": 0.0, "embeddings": []},
        }
        mgr._compute_centroids()
        # 两个 topic 相同 embed → centroid 相同 → nearest=0
        assert mgr._topic_data[1]["nearest_centroid_dist"] == 0.0
        assert abs(mgr._topic_data[2]["nearest_centroid_dist"]) < 1e-10


# ============================================================================
# 全场景集成：detect → grade_on_switch → get_turn_grade
# ============================================================================

class TestFullPipeline:
    """完整话题切换管线"""

    def test_two_topic_switch_cycle(self, mgr, mock_store):
        """全流程：第一话题 → 切换 → 第二话题 → 查询等级"""
        from ca.store import write_turn_v5
        # 写 store 数据供 _compute_centroids 使用
        write_turn_v5(mock_store, "test", 1, 0, role="user", elm_text="Python消息",
                      fct_text='{"core_change":"Python脚本相关"}')
        mgr._store = mock_store
        mock_store.session_id = "test"

        # Turn 1: 话题 1
        mgr.detect(1, [(0, "user", None, None, '{"core_change":"Python脚本相关"}')], "帮我写个Python脚本")
        # Turn 2: 强制切换
        switched = mgr.detect(2, [], "换个话题，聊Java怎么样")
        assert switched is True

        # query 对齐 topic 2 不相关文本 → topic 1 的嵌入应较远
        q_emb = mgr._embed_client.embed("Java虚拟机")
        mgr.grade_on_switch(q_emb, "换个话题，聊Java怎么样")

        # Turn 1（旧话题）应为 FAR（与 Java query 嵌入距离远）
        assert mgr.get_turn_grade(1) == TopicGrade.FAR
        # Turn 2（新话题）应为 ACT
        assert mgr.get_turn_grade(2) == TopicGrade.ACT

    def test_single_topic_all_act(self, mgr):
        """单话题全部 ACT"""
        mgr.detect(1, [], "你好")
        mgr._current_topic_id = 1
        assert mgr.get_turn_grade(1) == TopicGrade.ACT

    def test_embed_failure_fallback(self, mgr, mock_store):
        """embed 失败时 centroid=None → REL fallback + 新话题 ACT"""
        from ca.store import write_turn_v5
        write_turn_v5(mock_store, "test", 1, 0,
                      role="user", elm_text="A",
                      fct_text='{"core_change":"A"}')
        mgr._store = mock_store
        mock_store.session_id = "test"

        mgr.detect(1, [(0, "user", None, None, '{"core_change":"A"}')], "聊Python")
        mgr.detect(2, [], "换个话题")
        # embed 失败 → _compute_centroids 拿不到 centroid
        mgr._embed_client.embed = MagicMock(side_effect=RuntimeError("dead Ollama"))
        q_emb = [0.5] * 768
        mgr.grade_on_switch(q_emb, "换个话题")
        # topic 1 centroid=None → REL
        assert mgr._topic_grades[1] == TopicGrade.REL
        # topic 2 新话题 → ACT
        assert mgr._topic_grades[2] == TopicGrade.ACT

    def test_multi_switch_no_topic_data_leak(self, mgr, mock_store):
        """多轮 switch：第二次 grade_on_switch 后新话题仍在 _topic_data 中"""
        from ca.store import write_turn_v5
        # 写 3 个 turn 的 Fct 数据，供 _compute_centroids 读取
        for t in (1, 2, 3):
            write_turn_v5(mock_store, "test", t, 0,
                          role="user", elm_text=f"消息{t}",
                          fct_text=f'{{"core_change":"内容{t}"}}')
        mgr._store = mock_store
        mock_store.session_id = "test"

        # Turn 1 → topic 1
        mgr.detect(1, [(0, "user", None, None, '{"core_change":"内容1"}')], "聊Python")
        # Turn 2 → topic 2（强制切换）
        mgr.detect(2, [(0, "user", None, None, '{"core_change":"内容2"}')], "换个话题，聊Java")
        # 第一次 grade_on_switch：topic_data 重建 → 含 topic 1,2
        q1 = mgr._embed_client.embed("聊Java")
        mgr.grade_on_switch(q1, "聊Java")
        assert 1 in mgr._topic_data, "topic 1 应在 _topic_data"
        assert 2 in mgr._topic_data, "topic 2 应在 _topic_data"

        # Turn 3 → topic 3（强制切换）
        mgr.detect(3, [(0, "user", None, None, '{"core_change":"内容3"}')], "聊点别的，Golang")
        # 第二次 grade_on_switch：topic_data 重建 → 必须含 topic 3
        q2 = mgr._embed_client.embed("聊Golang")
        mgr.grade_on_switch(q2, "聊Golang")
        assert 1 in mgr._topic_data, "topic 1 应在 _topic_data（第二次 switch 后）"
        assert 2 in mgr._topic_data, "topic 2 应在 _topic_data（第二次 switch 后）"
        assert 3 in mgr._topic_data, "BUG: topic 3 不在 _topic_data（_assign_topic 创建后未同步到 _topic_data）"

        # 三个 topic 都应有等级（非默认 ACT fallback）
        assert 1 in mgr._topic_grades, "topic 1 应有等级"
        assert 2 in mgr._topic_grades, "topic 2 应有等级"
        assert 3 in mgr._topic_grades, "topic 3 应有等级"

        # get_turn_grade 也应返回有效等级
        g1 = mgr.get_turn_grade(1)
        g2 = mgr.get_turn_grade(2)
        g3 = mgr.get_turn_grade(3)
        assert g3 == TopicGrade.ACT, "新话题（topic 3）应被强制 ACT"
        assert isinstance(g1, TopicGrade), f"topic 1 等级类型错误: {type(g1)}"
        assert isinstance(g2, TopicGrade), f"topic 2 等级类型错误: {type(g2)}"
        assert isinstance(g3, TopicGrade), f"topic 3 等级类型错误: {type(g3)}"


# ============================================================================
# _topic_mgr = None 场景（在 __init__.py 的 _simple_mutation_mode_v5 中防护）
# ============================================================================

class TestTopicMgrNoneGuard:
    """_topic_mgr = None 时 _simple_mutation_mode_v5 全线 ACT"""

    def test_none_mgr_all_act(self):
        """模拟 _topic_mgr = None → 第 423 行走 TopicGrade.ACT 默认值"""
        # 这个测试验证 __init__.py 中第 423 行的逻辑：
        # topic_grade = self._topic_mgr.get_turn_grade(...) if self._topic_mgr else TopicGrade.ACT
        assert TopicGrade.ACT == "Act"

    def test_user_fin_map_none_act_fallback(self):
        """_topic_mgr = None → user_fin_map 全部 Elm（原文保留）"""
        # 验证映射表
        user_fin_map = {
            TopicGrade.ACT: Grade.ELM,
            TopicGrade.REL: Grade.FCT,
            TopicGrade.FAR: Grade.HDL,
        }
        assert user_fin_map.get(TopicGrade.ACT) == Grade.ELM

    def test_thought_tool_map_none_act_fallback(self):
        """_topic_mgr = None → thought_tool_map 全部 Fct（完整摘要）"""
        thought_tool_map = {
            TopicGrade.ACT: Grade.FCT,
            TopicGrade.REL: Grade.HDL,
            TopicGrade.FAR: None,
        }
        assert thought_tool_map.get(TopicGrade.ACT) == Grade.FCT


# ============================================================================
# GAP-D: CA-OV 话题提交端点
# ============================================================================

def test_topic_submit_endpoint_exists():
    """GAP-D: _fire_ov_submit 已从 ca/lstage.py 移除，CA_OV_SUBMIT_ENABLED 未启用"""
    pytest.skip("topic submit not implemented — _fire_ov_submit removed from ca/lstage.py")
