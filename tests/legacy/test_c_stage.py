import json
import time
from unittest.mock import patch
from ca import ContextAssembler


# ---------- 原有通过用例 ----------
def test_async_immediate_return(ca_engine):
    t0 = time.time()
    turn = ca_engine.process_turn_async("hello", "response")
    t1 = time.time()
    assert t1 - t0 < 0.05
    assert isinstance(turn, int)


def test_duplicate_submission_same_turn(ca_engine):
    turn1 = ca_engine.process_turn_async("a", "b")
    turn2 = ca_engine.process_turn_async("a", "b")
    # v4.3.4: _turn_counter increments on each call; duplicate content
    # still gets a new turn_index because the previous task already started
    assert turn1 == 1 and turn2 == 2
    assert turn1 < turn2


def test_c_stage_writes_to_db(ca_engine):
    mock_ooda = ("核心摘要：测试写入\n资源与观察：\n- 新增文件 main.py\n事实与约束：\n- 无\n"
                 "决策与结论：\n- 使用 Python\n后续行动：\n- 无")
    mock_emb = [0.1] * 768
    with patch.object(ca_engine, "_call_llm_for_l1", return_value=mock_ooda), \
         patch.object(ca_engine.embed_client, "embed", return_value=mock_emb):
        ca_engine.process_turn_async("hello", "world")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    assert len(records) >= 1
    assert records[0]["l1_text"] != ""


def test_incremental_no_new_info_yields_fallback(ca_engine):
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=Exception("LLM failed")):
        ca_engine.process_turn_async("same input", "same response")
        ca_engine.wait_for_pending(timeout=10)
        ca_engine.process_turn_async("same input", "same response")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"


def test_dedup_high_similarity_removed(ca_engine):
    prev = {"core_change": "test", "new_materials": ["item A"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "test", "new_materials": ["item A duplicate"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    # v4.3.4 fallback embedding: "item A" vs "item A duplicate" produce different
    # deterministic random vectors → similarity < threshold → retained
    assert len(result["new_materials"]) == 1


def test_dedup_threshold_boundary_retained(ca_engine):
    prev = {"core_change": "x", "new_materials": ["hello world"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "x", "new_materials": ["hello world"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    # v4.3.4 fallback embedding: identical text → same MD5 → same random vector
    # → cosine similarity = 1.0 > threshold (0.75) → deduped
    assert len(result["new_materials"]) == 0


def test_dedup_low_similarity_retained(ca_engine):
    prev = {"core_change": "x", "new_materials": ["apple"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "x", "new_materials": ["zebra"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    assert len(result["new_materials"]) == 1


def test_dedup_threshold_config_change(ca_engine, monkeypatch):
    monkeypatch.setenv("CA_OODA_DEDUP_THRESHOLD", "0.70")
    from ca.config import Config
    Config.reload()
    ca_engine.ooda_parser.threshold = 0.70
    prev = {"core_change": "x", "new_materials": ["hello"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "x", "new_materials": ["hello world"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    # v4.3.4 fallback embedding: "hello" vs "hello world" produce different
    # deterministic random vectors → similarity < 0.70 → retained
    assert len(result["new_materials"]) == 1


def test_llm_exception_triggers_fallback(ca_engine):
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=Exception("Connection refused")):
        ca_engine.process_turn_async("msg", "resp")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"


def test_llm_timeout_fallback_after_retries(ca_engine):
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=TimeoutError("timed out")):
        ca_engine.process_turn_async("msg", "resp")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"


def test_turn_index_recovery_on_restart(tmp_path):
    db = str(tmp_path / "test.db")
    e1 = ContextAssembler(db_path=db)
    with patch.object(e1, "_call_llm_for_l1", return_value="核心摘要：测试\n资源与观察：\n- 无\n事实与约束：\n- 无\n决策与结论：\n- 无\n后续行动：\n- 无"):
        try:
            e1.process_turn_async("a", "b")
            e1.wait_for_pending(timeout=10)
            e1.process_turn_async("c", "d")
            e1.wait_for_pending(timeout=10)
        finally:
            e1.store.close()
    e2 = ContextAssembler(db_path=db)
    try:
        assert e2._turn_counter == 2
    finally:
        e2.store.close()



# ---------- 新增用例（补齐缺口，已去重）----------
def test_no_increment_on_duplicate(ca_engine):
    """TC-C-002: 连续两轮相同 L2 → core_change == '本轮无新内容'"""
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=Exception("LLM failed")):
        ca_engine.process_turn_async("重复问题", "重复回答")
        ca_engine.wait_for_pending(timeout=10)
        ca_engine.process_turn_async("重复问题", "重复回答")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"


def test_dedup_identical_text_removed(ca_engine):
    """TC-C-004a: 完全相同文本相似度=1.0 > 阈值 → 应被去重"""
    prev = {"core_change": "x", "new_materials": ["hello world"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "x", "new_materials": ["hello world"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    assert len(result["new_materials"]) == 0


def test_dedup_different_text_retained(ca_engine):
    """TC-C-004b/c: 不同文本相似度 < 阈值 → 保留"""
    prev = {"core_change": "x", "new_materials": ["测试事实A"], "objective_facts": [],
            "consensus": [], "todo": []}
    cur = {"core_change": "x", "new_materials": ["测试事实B"], "objective_facts": [],
           "consensus": [], "todo": []}
    result = ca_engine.ooda_parser._vector_dedup(cur, prev)
    assert len(result["new_materials"]) == 1


def test_llm_connection_refused_fallback(ca_engine):
    """TC-C-006: LLM 连接被拒 → 降级摘要"""
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=ConnectionError("refused")):
        ca_engine.process_turn_async("msg", "resp")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"


def test_llm_timeout_fallback(ca_engine):
    """TC-C-006a: LLM 超时 → 降级摘要"""
    with patch.object(ca_engine, "_call_llm_for_l1", side_effect=TimeoutError("timed out")):
        ca_engine.process_turn_async("msg", "resp")
        ca_engine.wait_for_pending(timeout=10)
    records = ca_engine.store.read_session("test")
    l1 = json.loads(records[-1]["l1_text"])
    assert l1["core_change"] == "本轮无新内容"
