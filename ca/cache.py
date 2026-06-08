"""
ca/cache.py — 内存缓存与 BM25 索引 (v4.4.0 alpha)

功能：
- 不可变 BM25 快照 + 嵌入字典原子替换。
- 支持对话轮与工具轮各自独立的索引。
- Copy‑on‑Write：重建索引在锁外执行。
- 快照锁升级为 RLock，保证读写互斥。
- 冷却重试与并发保护，防止重建任务雪崩。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]")


def _is_cjk_char(c: str) -> bool:
    cp = ord(c)
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F or
            0xFF00 <= cp <= 0xFFEF)


def tokenise(text: str) -> List[str]:
    tokens = []
    for m in _TOKEN_PATTERN.finditer(text.lower()):
        t = m.group(0)
        if not t.strip():
            continue
        if _is_cjk_char(t[0]):
            tokens.extend(t)
        else:
            tokens.append(t)
    return tokens


TurnKey = Union[int, Tuple[int, int]]


class BM25Okapi:
    def __init__(self, corpus: List[Tuple[TurnKey, str]], k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self._lock = threading.RLock()
        self._index: Dict[str, Dict[int, int]] = {}
        self._doc_lengths: List[int] = []
        self._turn_keys: List[TurnKey] = []
        self._n_docs = 0
        self._total_length = 0
        self._avgdl = 0.0
        self._build(corpus)

    def _build(self, corpus: List[Tuple[TurnKey, str]]) -> None:
        sorted_corpus = sorted(
            corpus,
            key=lambda x: (0, x[0]) if isinstance(x[0], int) else (1, x[0][0], x[0][1])
        )
        with self._lock:
            self._turn_keys = [key for key, _ in sorted_corpus]
            self._index.clear()
            self._doc_lengths = []
            self._total_length = 0
            for doc_id, (_, text) in enumerate(sorted_corpus):
                tokens = tokenise(text)
                self._doc_lengths.append(len(tokens))
                self._total_length += len(tokens)
                seen: Dict[str, int] = {}
                for tok in tokens:
                    seen[tok] = seen.get(tok, 0) + 1
                for tok, cnt in seen.items():
                    self._index.setdefault(tok, {})[doc_id] = cnt
            self._n_docs = len(sorted_corpus)
            self._avgdl = self._total_length / self._n_docs if self._n_docs else 0.0

    def get_scores(self, query_tokens: List[str]) -> List[float]:
        with self._lock:
            scores = [0.0] * self._n_docs
            if not query_tokens:
                return scores
            for tok in query_tokens:
                if tok not in self._index:
                    continue
                idf = self._idf(tok)
                for doc_id, freq in self._index[tok].items():
                    dl = self._doc_lengths[doc_id]
                    score = idf * (freq * (self.k1 + 1)) / (freq + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
                    scores[doc_id] += score
            return scores

    def _idf(self, term: str) -> float:
        n = len(self._index.get(term, {}))
        if n == 0:
            return 0.0
        return math.log(1 + (self._n_docs - n + 0.5) / (n + 0.5))

    def get_turn_key(self, doc_id: int) -> Optional[TurnKey]:
        with self._lock:
            if 0 <= doc_id < len(self._turn_keys):
                return self._turn_keys[doc_id]
        return None

    @property
    def document_count(self) -> int:
        return self._n_docs


class BM25Snapshot:
    def __init__(self, bm25: BM25Okapi, turn_indices: List[TurnKey], l1_embeddings: Dict[TurnKey, List[float]],
                 tool_bm25: Optional[BM25Okapi] = None,
                 tool_turn_keys: Optional[List[TurnKey]] = None,
                 tool_l1_embeddings: Optional[Dict[TurnKey, List[float]]] = None):
        self.bm25 = bm25
        self.turn_indices = turn_indices
        self.l1_embeddings = l1_embeddings
        self.tool_bm25 = tool_bm25
        self.tool_turn_keys = tool_turn_keys or []
        self.tool_l1_embeddings = tool_l1_embeddings or {}


class AssemblyCache:
    def __init__(self):
        self._lock = threading.RLock()
        self.l0_texts: Dict[int, str] = {}
        self.l1_texts: Dict[int, str] = {}
        self.l0_embeddings: Dict[int, List[float]] = {}
        self.l1_embeddings: Dict[int, List[float]] = {}

        self.tool_l0_texts: Dict[Tuple[int, int], str] = {}
        self.tool_l1_texts: Dict[Tuple[int, int], str] = {}
        self.tool_l0_embeddings: Dict[Tuple[int, int], List[float]] = {}
        self.tool_l1_embeddings: Dict[Tuple[int, int], List[float]] = {}

        # PR3: 工具组摘要缓存 (assistant{tc} 行)
        self.tool_group_l0_texts: Dict[Tuple[int, int], str] = {}
        self.tool_group_l1_texts: Dict[Tuple[int, int], str] = {}

        self._snapshot: Optional[BM25Snapshot] = None
        self._snapshot_lock = threading.RLock()
        self._dirty = False
        self._rebuild_executor = ThreadPoolExecutor(max_workers=1)

        self._rebuild_future: Optional[Future] = None
        self._rebuild_lock = threading.Lock()

        self._last_failed_rebuild = 0.0
        self._rebuild_cooldown = 5.0
        self._pending_retry = False
        self._retry_timer: Optional[threading.Timer] = None
        self._retry_lock = threading.Lock()

        self._destroyed = False

    def add_turn(self, turn_index, l0_text, l1_text, l0_emb=None, l1_emb=None):
        with self._lock:
            self.l0_texts[turn_index] = l0_text
            self.l1_texts[turn_index] = l1_text
            if l0_emb is not None:
                self.l0_embeddings[turn_index] = l0_emb
            if l1_emb is not None:
                self.l1_embeddings[turn_index] = l1_emb
            self._dirty = True
        self._submit_rebuild()

    def add_tool_turn(self, turn_index, sub_index, l0_text, l1_text, l0_emb=None, l1_emb=None):
        key = (turn_index, sub_index)
        with self._lock:
            self.tool_l0_texts[key] = l0_text
            self.tool_l1_texts[key] = l1_text
            if l0_emb is not None:
                self.tool_l0_embeddings[key] = l0_emb
            if l1_emb is not None:
                self.tool_l1_embeddings[key] = l1_emb
            self._dirty = True
        self._submit_rebuild()

    def _submit_rebuild(self):
        if self._destroyed:
            return
        with self._rebuild_lock:
            if self._rebuild_future is not None and not self._rebuild_future.done():
                return
            self._rebuild_future = self._rebuild_executor.submit(self._rebuild_if_dirty)

    def _rebuild_if_dirty(self):
        try:
            now = time.monotonic()
            with self._retry_lock:
                if now - self._last_failed_rebuild < self._rebuild_cooldown:
                    if not self._pending_retry:
                        self._pending_retry = True
                        delay = self._rebuild_cooldown - (now - self._last_failed_rebuild)
                        self._retry_timer = threading.Timer(delay, self._retry_rebuild)
                        self._retry_timer.daemon = True
                        self._retry_timer.start()
                    return
                self._pending_retry = False

            if self._dirty:
                self.rebuild_bm25_snapshot()
        except Exception as e:
            logger.exception("Rebuild snapshot failed, entering cooldown")
            with self._lock:
                self._last_failed_rebuild = time.monotonic()
                self._dirty = True

    def _retry_rebuild(self):
        if self._destroyed:
            return
        with self._retry_lock:
            self._pending_retry = False
        self._submit_rebuild()

    def rebuild_bm25_snapshot(self):
        with self._lock:
            if not self._dirty:
                return
            l1_copy = dict(self.l1_texts)
            l1_emb_copy = dict(self.l1_embeddings)
            tool_l1_copy = dict(self.tool_l1_texts)
            tool_l1_emb_copy = dict(self.tool_l1_embeddings)
            self._dirty = False

        dialogue_bm25, dialogue_turn_indices, dialogue_l1_emb = None, [], {}
        sorted_dialogue = sorted(l1_copy.items())
        dialogue_corpus = [(idx, txt) for idx, txt in sorted_dialogue if txt.strip()]
        if dialogue_corpus:
            dialogue_bm25 = BM25Okapi(dialogue_corpus)
            dialogue_turn_indices = [idx for idx, _ in dialogue_corpus]
            dialogue_l1_emb = {idx: l1_emb_copy[idx] for idx in dialogue_turn_indices if idx in l1_emb_copy}

        tool_bm25, tool_turn_keys, tool_l1_emb = None, [], {}
        sorted_tool = sorted(tool_l1_copy.items(), key=lambda x: (x[0][0], x[0][1]))
        tool_corpus = [(key, txt) for key, txt in sorted_tool if txt.strip()]
        if tool_corpus:
            tool_bm25 = BM25Okapi(tool_corpus)
            tool_turn_keys = [key for key, _ in tool_corpus]
            tool_l1_emb = {key: tool_l1_emb_copy[key] for key in tool_turn_keys if key in tool_l1_emb_copy}

        snapshot = BM25Snapshot(dialogue_bm25, dialogue_turn_indices, dialogue_l1_emb,
                                tool_bm25, tool_turn_keys, tool_l1_emb)
        with self._snapshot_lock:
            self._snapshot = snapshot
        logger.debug("BM25 snapshot rebuilt, dialogue=%d, tool=%d", len(l1_copy), len(tool_l1_copy))

    def get_bm25_snapshot(self) -> Optional[BM25Snapshot]:
        with self._snapshot_lock:
            return self._snapshot

    def ensure_snapshot(self):
        if self._snapshot is None:
            self.rebuild_bm25_snapshot()

    def get_snapshot_data(self) -> Tuple[Dict[int, str], Dict[int, str]]:
        with self._lock:
            return dict(self.l1_texts), dict(self.l0_texts)

    def get_tool_snapshot_data(self) -> Tuple[Dict[Tuple[int, int], str], Dict[Tuple[int, int], str]]:
        with self._lock:
            return dict(self.tool_l1_texts), dict(self.tool_l0_texts)

    def get_tool_group_snapshot_data(self) -> Tuple[Dict[int, str], Dict[int, str]]:
        """获取工具组摘要数据。key 为 turn_index。"""
        with self._lock:
            return dict(self.tool_group_l1_texts), dict(self.tool_group_l0_texts)

    def cancel_retry_timer(self):
        with self._retry_lock:
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None

    def destroy(self):
        self._destroyed = True
        self.cancel_retry_timer()
        self._rebuild_executor.shutdown(wait=False)
        with self._rebuild_lock:
            if self._rebuild_future:
                self._rebuild_future.cancel()
                self._rebuild_future = None


class CacheBuilder:
    def __init__(self, store):
        self._store = store

    def build(self, session_id: str) -> AssemblyCache:
        cache = AssemblyCache()
        try:
            records = self._store.read_session(session_id)
            with cache._lock:
                for rec in records:
                    if rec.get("turn_type") == "tool":
                        key = (rec["turn_index"], rec.get("tool_sub_index", 0))
                        cache.tool_l0_texts[key] = rec.get("l0_text", "")
                        cache.tool_l1_texts[key] = rec.get("l1_text", "")
                        if rec.get("l0_embedding"):
                            cache.tool_l0_embeddings[key] = rec["l0_embedding"]
                        if rec.get("l1_embedding"):
                            cache.tool_l1_embeddings[key] = rec["l1_embedding"]
                    else:
                        idx = rec["turn_index"]
                        # PR3: 检测 assistant{tc} 行（有 tool_calls_json）→ 工具组缓存
                        tc_json = rec.get("tool_calls_json")
                        if tc_json:
                            cache.tool_group_l1_texts[idx] = rec.get("l1_text", "")
                            cache.tool_group_l0_texts[idx] = rec.get("l0_text", "")
                        else:
                            cache.l0_texts[idx] = rec.get("l0_text", "")
                            cache.l1_texts[idx] = rec.get("l1_text", "")
                            if rec.get("l0_embedding"):
                                cache.l0_embeddings[idx] = rec["l0_embedding"]
                            if rec.get("l1_embedding"):
                                cache.l1_embeddings[idx] = rec["l1_embedding"]
                cache._dirty = True
            cache.rebuild_bm25_snapshot()
        except Exception as e:
            logger.error("Failed to build cache from DB: %s, resetting to empty", e)
            with cache._lock:
                cache.l0_texts.clear()
                cache.l1_texts.clear()
                cache.l0_embeddings.clear()
                cache.l1_embeddings.clear()
                cache.tool_l0_texts.clear()
                cache.tool_l1_texts.clear()
                cache.tool_l0_embeddings.clear()
                cache.tool_l1_embeddings.clear()
                cache.tool_group_l0_texts.clear()
                cache.tool_group_l1_texts.clear()
                cache._dirty = True
            cache.rebuild_bm25_snapshot()
        return cache
