"""
ca/retrieval.py — 双路检索器 (v4.4.0 alpha)

功能：
- 支持对话轮与工具轮独立检索。
- RRF 融合统一使用 TurnKey 类型（对话轮转为 (idx,0)）。
- 动态阈值可配置。
- 嵌入维度不一致时降级为纯 BM25。
"""

from __future__ import annotations

import math
import logging
from typing import Dict, List, Optional, Set, Tuple, Union

from .cache import BM25Snapshot, BM25Okapi, tokenise
from .config import Config

logger = logging.getLogger(__name__)

TurnKey = Union[int, Tuple[int, int]]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cosine_similarity_batch(query: List[float],
                            targets: Dict[TurnKey, List[float]]) -> List[Tuple[TurnKey, float]]:
    results = []
    qn = math.sqrt(sum(q * q for q in query))
    if qn == 0:
        return [(k, 0.0) for k in targets]
    for k, v in targets.items():
        if len(query) != len(v):
            continue
        dot = sum(x * y for x, y in zip(query, v))
        tn = math.sqrt(sum(x * x for x in v))
        score = dot / (qn * tn) if tn > 0 else 0.0
        results.append((k, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _dynamic_allocation(bm25_hits: int, vec_hits: int, max_k: int) -> Tuple[int, int]:
    threshold = Config.BM25_HIT_THRESHOLD
    if bm25_hits >= threshold:
        bm25_k = min(6, max_k)
        vec_k = max(0, max_k - bm25_k)
    elif bm25_hits >= max(2, threshold // 2):
        bm25_k = min(4, max_k)
        vec_k = max(0, max_k - bm25_k)
    else:
        bm25_k = min(2, max_k)
        vec_k = max(0, max_k - bm25_k)
    vec_k = min(vec_k, vec_hits)
    return bm25_k, vec_k


class Retriever:
    def __init__(self, snapshot: BM25Snapshot):
        self._bm25 = snapshot.bm25
        self._turn_indices = snapshot.turn_indices
        self._fct_embeddings = snapshot.fct_embeddings
        self._tool_bm25 = snapshot.tool_bm25
        self._tool_turn_keys = snapshot.tool_turn_keys
        self._tool_fct_embeddings = snapshot.tool_fct_embeddings

    def retrieve(self, user_input: str,
                 query_embedding: Optional[List[float]] = None,
                 max_upgrade_k: int = 10,
                 upgrade_budget: int = 10) -> List[TurnKey]:
        if upgrade_budget <= 0:
            return []
        actual_k = min(max_upgrade_k, upgrade_budget)

        bm25_keys: List[TurnKey] = []
        bm25_hits = 0
        if self._bm25:
            query_tokens = tokenise(user_input)
            scores = self._bm25.get_scores(query_tokens)
            scored = []
            for doc_id, score in enumerate(scores):
                if score > 0:
                    key = self._bm25.get_turn_key(doc_id)
                    if key is not None:
                        if isinstance(key, int):
                            key = (key, 0)
                        scored.append((key, score))
                        bm25_hits += 1
            scored.sort(key=lambda x: x[1], reverse=True)
            bm25_keys = [k for k, _ in scored]

        vec_keys: List[TurnKey] = []
        vec_hits = len(self._fct_embeddings)
        if self._fct_embeddings and query_embedding:
            normalized_embeddings = {}
            for k, v in self._fct_embeddings.items():
                if isinstance(k, int):
                    normalized_embeddings[(k, 0)] = v
                else:
                    normalized_embeddings[k] = v
            valid_embeddings = {k: v for k, v in normalized_embeddings.items()
                                if len(v) == len(query_embedding)}
            if len(valid_embeddings) != len(normalized_embeddings):
                logger.warning("Filtered %d dialogue embedding(s) with mismatched dimensions",
                               len(normalized_embeddings) - len(valid_embeddings))
            if valid_embeddings:
                ranked = cosine_similarity_batch(query_embedding, valid_embeddings)
                vec_keys = [k for k, _ in ranked]
                vec_hits = len(vec_keys)
            else:
                vec_keys = []
                vec_hits = 0

        bm25_k, vec_k = _dynamic_allocation(bm25_hits, vec_hits, actual_k)
        fused = _rrf_fuse([bm25_keys[:bm25_k], vec_keys[:vec_k]], k=Config.RETRIEVAL_RRF_K)
        seen = set()
        result = []
        for key in fused:
            if key not in seen:
                seen.add(key)
                result.append(key)
                if len(result) >= actual_k:
                    break
        return result

    def retrieve_tools(self, query_text: str,
                       query_embedding: Optional[List[float]] = None,
                       max_k: int = 3,
                       upgrade_budget: Optional[int] = None) -> List[Tuple[int, int]]:
        if max_k <= 0 or not self._tool_bm25:
            return []

        actual_k = min(max_k, len(self._tool_turn_keys))
        bm25_scores = self._tool_bm25.get_scores(tokenise(query_text))
        bm25_hits = sum(1 for s in bm25_scores if s > 0)

        vec_hits = len(self._tool_fct_embeddings)
        vec_keys: List[TurnKey] = []
        if self._tool_fct_embeddings and query_embedding:
            valid_embeddings = {k: v for k, v in self._tool_fct_embeddings.items()
                                if len(v) == len(query_embedding)}
            if len(valid_embeddings) != len(self._tool_fct_embeddings):
                logger.warning("Filtered %d tool embedding(s) with mismatched dimensions",
                               len(self._tool_fct_embeddings) - len(valid_embeddings))
            if valid_embeddings:
                ranked = cosine_similarity_batch(query_embedding, valid_embeddings)
                vec_keys = [k for k, _ in ranked]
                vec_hits = len(vec_keys)
            else:
                vec_keys = []
                vec_hits = 0

        bm25_k, vec_k = _dynamic_allocation(bm25_hits, vec_hits, actual_k)

        bm25_ranked = sorted(
            [(key, bm25_scores[i]) for i, key in enumerate(self._tool_turn_keys) if bm25_scores[i] > 0],
            key=lambda x: x[1], reverse=True
        )[:bm25_k]
        vec_ranked = [(k, 1.0) for k in vec_keys[:vec_k]]

        fused = _rrf_fuse([bm25_ranked, vec_ranked], k=Config.RETRIEVAL_RRF_K)
        seen = set()
        result = []
        for key in fused:
            if key not in seen:
                seen.add(key)
                result.append(key)
                if len(result) >= actual_k:
                    break
        return result



def _rrf_fuse(ranked_lists: List, k: int = 60) -> List[TurnKey]:
    scores: Dict[TurnKey, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            if isinstance(item, tuple):
                doc_id, _ = item
            else:
                doc_id = item
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.keys(), key=lambda d: scores[d], reverse=True)


class TopicRetriever:
    """Per-topic 双路检索器（v4.6.0）

    在 assemble() 中话题分割完成后，对 topic 级做 BM25 + Vector 双路检索。
    - BM25：用 topic 聚合文本（5字段拼接）作为语料
    - Vector：用 topic 形心（均值向量）计算余弦相似度
    - RRF 融合 → 返回检索命中的 topic_id 集合
    """

    def __init__(self, snapshot: BM25Snapshot,
                 turn_to_topic: Dict[int, int],
                 topic_data: Dict[int, Dict]):
        self._snapshot = snapshot
        self._turn_to_topic = turn_to_topic
        self._topic_data = topic_data

    def retrieve(self, user_input: str,
                 query_embedding: Optional[List[float]] = None,
                 max_k: int = 10) -> Set[int]:
        """返回检索命中的 topic_id 集合。"""
        if max_k <= 0:
            return set()

        # 筛选非 BG topic（BG 不参与检索）
        active_topics = {tid for tid, td in self._topic_data.items()
                         if not td["is_bg"] and td.get("agg_text", "").strip()}
        if not active_topics:
            return set()

        # ── BM25 per-topic ──
        topic_list: List[int] = sorted(active_topics)
        topic_corpus = [(tid, self._topic_data[tid]["agg_text"]) for tid in topic_list
                        if self._topic_data[tid].get("agg_text", "").strip()]
        if not topic_corpus:
            return set()

        bm25 = BM25Okapi(topic_corpus)
        query_tokens = tokenise(user_input)
        scores = bm25.get_scores(query_tokens)

        bm25_ranked: List[Tuple[int, float]] = []
        bm25_hits = 0
        for doc_id, score in enumerate(scores):
            tid = bm25.get_turn_key(doc_id)
            if tid is not None and score > 0:
                bm25_ranked.append((tid, score))
                bm25_hits += 1
        bm25_ranked.sort(key=lambda x: x[1], reverse=True)

        # ── Vector per-topic ──
        vec_ranked: List[Tuple[int, float]] = []
        vec_hits = 0
        if query_embedding is not None:
            centroids = {}
            for tid in topic_list:
                cent = self._topic_data[tid].get("centroid")
                if cent and len(cent) == len(query_embedding):
                    centroids[tid] = cent
            if centroids:
                vec_results = cosine_similarity_batch(query_embedding, centroids)
                vec_ranked = [(k, s) for k, s in vec_results]
                vec_hits = len(vec_ranked)

        # ── 动态分配 + RRF ──
        actual_k = min(max_k, len(topic_list))
        bm25_k, vec_k = _dynamic_allocation(bm25_hits, vec_hits, actual_k)

        fused = _rrf_fuse([bm25_ranked[:bm25_k], vec_ranked[:vec_k]], k=Config.RETRIEVAL_RRF_K)
        return set(fused[:actual_k])
