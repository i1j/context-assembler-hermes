"""
Smoke tests for ContextAssembler – basic connectivity and core logic.
Can run without Ollama by relying on fallback embeddings and mocked LLM calls.
"""
import time
from ca.cache import AssemblyCache, CacheBuilder, BM25Okapi, BM25Snapshot, tokenise
from ca.retrieval import Retriever
import json
import pytest
from ca.store import SQLiteStore
from ca.retrieval import cosine_similarity, _rrf_fuse as rrf_fuse
from ca.embedding import EmbeddingClient
from ca.ooda_parser import OODAParser
from ca.post_process import robust_json_parse, clean_increment


# ------------------------------------------------------------------ fixture
@pytest.fixture
def store(tmp_path):
    db = SQLiteStore(tmp_path / "smoke.db")
    yield db
    db.close()

@pytest.fixture
def emb_client():
    # Use fallback backend to avoid external dependency
    return EmbeddingClient(backend="fallback")

@pytest.fixture
def parser(emb_client):
    return OODAParser(emb_client)


# ------------------------------------------------------------------ storage
def test_write_and_read(store):
    store.write_turn("s1", 0, l0_text="hello", l1_text='{"key":1}')
    recs = store.read_session("s1")
    assert len(recs) == 1
    assert recs[0]["l0_text"] == "hello"
    assert recs[0]["turn_index"] == 0

def test_batch_write(store):
    store.write_turns_batch("s2", [
        {"turn_index": 0, "l0_text": "a"},
        {"turn_index": 1, "l0_text": "b"},
    ])
    records = store.read_session("s2")
    assert len(records) == 2


# ------------------------------------------------------------------ BM25
def test_tokenise():
    tokens = tokenise("Hello 世界")
    assert "hello" in tokens
    assert "世" in tokens
    assert "界" in tokens

def test_bm25_index():
    corpus = ["hello world", "hello python"]
    bm = BM25Okapi(list(enumerate(corpus)))
    scores = bm.get_scores(["hello"])
    assert scores[0] > 0 and scores[1] > 0

def test_bm25_add_document():
    bm = BM25Okapi([(0, "first doc"), (1, "second document")])
    scores = bm.get_scores(["second"])
    assert scores[0] >= 0 and scores[1] > 0


# ------------------------------------------------------------------ retrieval
def test_cosine():
    v1 = [1.0, 0.0]
    v2 = [0.0, 1.0]
    assert cosine_similarity(v1, v2) == 0.0
    assert cosine_similarity(v1, v1) == 1.0

def test_rrf():
    fused = rrf_fuse([[1, 2, 3], [3, 4, 5]])
    assert len(fused) == 5
    assert fused[0] == 3  # appears in both lists => highest


# ------------------------------------------------------------------ embedding
def test_embedding_fallback(emb_client):
    vec = emb_client.embed("test")
    assert len(vec) == 768  # fallback dimension
    # Determinism: same input produces same output
    assert vec == emb_client.embed("test")

def test_embedding_cache(emb_client):
    emb_client.embed("cache_me")
    stats = emb_client.get_stats()
    assert stats["total_calls"] == 1
    emb_client.embed("cache_me")
    assert emb_client.get_stats()["cache_hits"] == 1


# ------------------------------------------------------------------ OODA parser
def test_parse_full(parser):
    text = """核心摘要：新增了向量去重功能
资源与观察：
- 新增文件 ooda_parser.py
- 向量去重模块
事实与约束：
- 阈值设为0.88
决策与结论：
无
后续行动：
- 集成测试"""
    result = parser.parse(text)
    assert result["core_change"].endswith("新增了向量去重功能")
    assert len(result["new_materials"]) == 2
    assert result["consensus"] == [] or result["consensus"] == ['：']

def test_parse_empty_increment(parser):
    text = """核心摘要：本轮无新内容
资源与观察：无
事实与约束：无
决策与结论：无
后续行动：无"""
    result = parser.parse(text)
    assert result["core_change"].endswith("本轮无新内容")

def test_parse_missing_sections(parser):
    text = """核心摘要：Only core
资源与观察：
- item"""
    result = parser.parse(text)
    assert "objective_facts" not in result
    assert "_parse_meta" in result
    assert "sections_missing" in result["_parse_meta"]


# ------------------------------------------------------------------ post process
def test_json_repair():
    broken = '{"core_change": "hello"'
    obj, method = robust_json_parse(broken)
    assert obj["core_change"] == "hello"
    assert method == "bracket_repair"

def test_clean_increment():
    data = {
        "core_change": "test",
        "new_materials": ["   ", "valid", "", "extra"],
        "consensus": ["yes"],
    }
    cleaned = clean_increment(data)
    assert cleaned["core_change"] == "test"
    assert len(cleaned["new_materials"]) == 2
    assert cleaned["new_materials"] == ["valid", "extra"]
    assert "consensus" in cleaned

# 以下内容追加至 tests/test_smoke.py 文件末尾

class TestEdgeScenarios:
    """边界场景：大 session、空 session、首轮、零预算、短输入。"""

    # ------------------------------------------------------------------
    # 大 session
    # ------------------------------------------------------------------
    def test_large_session_retrieval(self):
        """500 轮历史下检索仍能在合理时间内完成，且不抛异常。"""
        import time
        cache = AssemblyCache()
        for i in range(500):
            cache.l1_texts[i] = f"topic {i % 10} document {i}"
            cache.l1_embeddings[i] = [float(i % 100) for _ in range(768)]
        corpus = [(i, cache.l1_texts[i]) for i in range(500)]
        bm25 = BM25Okapi(corpus)
        turn_indices = list(range(500))
        snapshot = BM25Snapshot(bm25, turn_indices, dict(cache.l1_embeddings))

        retriever = Retriever(snapshot)
        start = time.perf_counter()
        results = retriever.retrieve("topic 5", [float(5)] * 768, upgrade_budget=20)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"Retrieval took {elapsed:.2f}s, expected <1s"
        assert len(results) > 0

    # ------------------------------------------------------------------
    # 空 session
    # ------------------------------------------------------------------
    def test_empty_store_handling(self, store):
        """空的 SQLite 存储应返回空缓存，不会崩溃。"""
        builder = CacheBuilder(store)
        cache = builder.build("empty_session")
        assert len(cache.l1_texts) == 0
        assert len(cache.l0_texts) == 0
        records = store.read_session("empty_session")
        assert records == []

    # ------------------------------------------------------------------
    # 首轮对话
    # ------------------------------------------------------------------
    def test_first_turn_l1_generation(self, parser):
        """首轮对话时，previous_summary 为空，解析应正常工作。"""
        text = "核心摘要：启动对话\n资源与观察：- 无\n事实与约束：- 无\n决策与结论：- 无\n后续行动：- 无"
        result = parser.parse(text, previous_summary=None)
        assert result["core_change"].endswith("启动对话")
        # 无 previous_summary 时跳过向量去重
        assert "_parse_meta" in result

    # ------------------------------------------------------------------
    # 零预算
    # ------------------------------------------------------------------
    def test_zero_upgrade_budget(self):
        """预算为 0 时，检索应立即返回空列表。"""
        cache = AssemblyCache()
        cache.l1_texts[0] = "hello world"
        cache.l1_embeddings[0] = [0.1] * 768
        bm25 = BM25Okapi([(0, "hello world")])
        snapshot = BM25Snapshot(bm25, [0], {0: cache.l1_embeddings[0]})
        retriever = Retriever(snapshot)
        results = retriever.retrieve("hello", [0.1] * 768, upgrade_budget=0)
        assert results == []

    # ------------------------------------------------------------------
    # 短输入
    # ------------------------------------------------------------------
    def test_short_input_retrieval(self):
        """极短输入（如单个字）应能正常分词和检索。"""
        text = "短"
        tokens = tokenise(text)
        assert len(tokens) == 1 and tokens[0] == "短"
        # 构建微型索引，确保不抛异常
        bm = BM25Okapi([(0, "短文本"), (1, "长文本示例")])
        scores = bm.get_scores(tokens)
        assert scores[0] > 0