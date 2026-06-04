import json
import re
from ca.cache import CacheBuilder
from ca.retrieval import Retriever
from ca.embedding import EmbeddingClient


# ---------- 原有用例 ----------
def test_empty_cache_returns_original(ca_engine):
    msgs = [{"role": "user", "content": "hi"}]
    result = ca_engine.assemble("hi", msgs)
    assert result == msgs


def test_assemble_with_history_does_not_crash(ca_engine):
    ca_engine.store.write_turn("default", 0, l0_text="l0", l1_text='{"core_change":"测试摘要"}')
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "后续问题"},
    ]
    result = ca_engine.assemble("后续问题", msgs)
    assert len(result) >= len(msgs)


def test_assemble_p95_latency(tmp_path):
    from ca import ContextAssembler
    import time
    db = str(tmp_path / "test.db")
    engine = ContextAssembler(db_path=db)
    engine.embed_client = EmbeddingClient(backend="fallback")
    try:
        for i in range(50):
            engine.store.write_turn("default", i, l0_text=f"l0_{i}", l1_text='{"core_change":"test"}')
        latencies = []
        msgs = [{"role": "user", "content": "query"}]
        for _ in range(100):
            t0 = time.perf_counter()
            engine.assemble("query", msgs)
            latencies.append(time.perf_counter() - t0)
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        assert p95 < 0.2
    finally:
        engine.store.close()


def test_head_middle_tail_layering(ca_engine):
    for i in range(5):
        ca_engine.store.write_turn("default", i, l0_text=f"l0_{i}",
                                   l1_text=json.dumps({"core_change": f"turn_{i}"}))
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    msgs.append({"role": "user", "content": "final question"})
    result = ca_engine.assemble("final question", msgs)
    contents = [m["content"] for m in result if "[~/" in str(m.get("content", ""))]
    assert len(contents) >= 3


def test_dual_retrieval_bm25_and_vector(ca_engine):
    for i in range(3):
        ca_engine.store.write_turn("default", i, l0_text=f"keyword_{i}",
                                   l1_text=json.dumps({"core_change": f"摘要{i}"}))
    cache = CacheBuilder(ca_engine.store).build("default")
    cache.ensure_snapshot()
    snapshot = cache.get_bm25_snapshot()
    assert snapshot is not None, "BM25 snapshot should exist after building cache from DB records"
    r = Retriever(snapshot)
    # v4.3.4: BM25 index built from l1_texts only, so query must match l1_text content
    result = r.retrieve("core_change")
    assert len(result) >= 1
    assert all(isinstance(idx, int) for idx in result)


def test_rrf_fusion_stable(ca_engine):
    for i in range(3):
        ca_engine.store.write_turn("default", i, l0_text=f"stable_{i}",
                                   l1_text=json.dumps({"core_change": f"sum_{i}"}))
    cache = CacheBuilder(ca_engine.store).build("default")
    cache.ensure_snapshot()
    snapshot = cache.get_bm25_snapshot()
    assert snapshot is not None, "BM25 snapshot should exist after building cache from DB records"
    r = Retriever(snapshot)
    first = r.retrieve("stable")
    for _ in range(10):
        assert r.retrieve("stable") == first


def test_budget_gate_respects_limit(ca_engine):
    for i in range(10):
        ca_engine.store.write_turn("default", i, l0_text="short",
                                   l1_text=json.dumps({"core_change": "longer summary text"}))
    msgs = [{"role": "user", "content": "test"}]
    result = ca_engine.assemble("test", msgs, context_length=500)
    total_tokens = sum(len(str(m.get("content", ""))) // 4 for m in result)
    assert total_tokens <= 500


def test_hard_truncation_when_over_95_percent(ca_engine):
    msgs = [{"role": "user", "content": "x" * 40000}]
    result = ca_engine.assemble("query", msgs, context_length=1000)
    total_chars = sum(len(str(m.get("content", ""))) for m in result)
    assert total_chars < 1000


def test_no_truncation_at_exact_95_percent(ca_engine):
    content = "a" * 950
    msgs = [{"role": "user", "content": content}]
    result = ca_engine.assemble("q", msgs, context_length=1000)
    assert result == msgs


def test_tool_calls_preserved(ca_engine):
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "call", "tool_calls": [{"name": "test"}]},
        {"role": "tool", "content": "result"},
        {"role": "user", "content": "hi"},
    ]
    result = ca_engine.assemble("hi", msgs)
    assert any(m.get("tool_calls") for m in result)
    assert any(m["role"] == "tool" for m in result)


# ---------- 补充：[~/N] 标记格式验证 ----------
def test_marker_format_is_correct(ca_engine):
    """TC-A-006 补充: 标记格式为 [~/N]"""
    ca_engine.store.write_turn("default", 0, l0_text="l0", l1_text='{"core_change":"摘要0"}')
    ca_engine.store.write_turn("default", 1, l0_text="l1", l1_text='{"core_change":"摘要1"}')
    msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "final"}]
    result = ca_engine.assemble("final", msgs)
    markers = [m["content"] for m in result if "[~/" in str(m.get("content", ""))]
    for marker_text in markers:
        assert re.search(r"\[~/[0-9]+\]", marker_text), f"标记格式错误: {marker_text}"
