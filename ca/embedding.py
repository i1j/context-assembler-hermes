"""
ca/embedding.py — 嵌入客户端 (v4.4.0 alpha)

功能：
- 支持 Ollama / sentence-transformers / fallback 后端。
- 线程安全 LRU 缓存，fallback 伪向量不缓存。
- 连接池过期时自动清除并重试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EMBED_DIM = 768

try:
    import urllib3
    from urllib3.exceptions import HTTPError as Urllib3HTTPError
    _URLLIB3 = True
except ImportError:
    _URLLIB3 = False
    Urllib3HTTPError = Exception


class EmbeddingClient:
    _OLLAMA_TIMEOUT = float(os.getenv("CA_EMBED_TIMEOUT", "10"))
    _OLLAMA_MAX_RETRIES = int(os.getenv("CA_EMBED_MAX_RETRIES", "0"))
    _BATCH_PARALLEL_TIMEOUT = float(os.getenv("CA_EMBED_BATCH_TIMEOUT", "15"))

    def __init__(self, backend="", model="", url=""):
        self._backend = backend or os.getenv("CA_EMBED_BACKEND", "ollama")
        self._model = model or os.getenv("CA_EMBED_MODEL", "qwen3-embedding:0.6b")
        self._url = (url or os.getenv("CA_EMBED_ENDPOINT", "http://127.0.0.1:11435")).rstrip("/")
        self._client = None
        self._dim = _EMBED_DIM
        self._dim_detected = False
        self._http_pool = None
        if _URLLIB3 and self._backend == "ollama":
            self._http_pool = urllib3.PoolManager(
                num_pools=4, maxsize=8, retries=0,
                timeout=urllib3.Timeout(connect=5.0, read=self._OLLAMA_TIMEOUT))
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="EmbedWorker")
        self._closed = False
        self._cache_lock = threading.RLock()
        self._cache: Dict[str, List[float]] = {}
        self._cache_maxsize = 256
        self._cache_order: List[str] = []
        self._stats: Dict[str, Any] = {"total_calls": 0, "cache_misses": 0,
                                       "total_time_sec": 0.0, "failures": 0,
                                       "fallback_used": 0}
        self._stats_lock = threading.Lock()

    def close(self):
        if not self._closed:
            self._executor.shutdown(wait=True)
            if self._http_pool:
                self._http_pool.clear()
            self._closed = True
            logger.info("EmbeddingClient shutdown complete.")

    def __del__(self):
        self.close()

    def embed(self, text):
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return self._embed_fallback("")
        return self._encode_cached(text)

    def embed_batch(self, texts):
        if not isinstance(texts, list):
            return []
        return [self._encode_cached(t) for t in texts]

    def embed_batch_parallel(self, texts):
        if not isinstance(texts, list):
            return []
        if not texts:
            return []
        futures = [self._executor.submit(self._encode_cached, t) for t in texts]
        results = []
        for i, f in enumerate(futures):
            try:
                results.append(f.result(timeout=self._BATCH_PARALLEL_TIMEOUT))
            except (FutureTimeoutError, Exception) as e:
                logger.warning("Parallel embed %d/%d failed: %s", i + 1, len(texts), e)
                results.append(self._embed_fallback(""))
        return results

    def warmup(self, texts=None):
        if texts is None:
            texts = ["用户：你好", "助理：你好", "系统初始化"]
        self.embed_batch(texts)
        logger.info("Embedding cache warmed up.")

    def get_stats(self):
        with self._stats_lock:
            s = dict(self._stats)
        total = s["total_calls"]
        hits = total - s["cache_misses"]
        s["cache_hits"] = hits
        s["cache_hit_rate"] = hits / total if total else 0.0
        s["avg_time_ms"] = (s["total_time_sec"] / total * 1000) if total else 0.0
        return s

    def _encode_cached(self, text: str) -> List[float]:
        with self._cache_lock:
            if text in self._cache:
                self._cache_order.remove(text)
                self._cache_order.insert(0, text)
                try:
                    if int(os.getenv("CA_DEBUG_LEVEL", "0")) >= 2:
                        logger.debug("Embedding cache hit for: %.50s...", text)
                except ValueError:
                    pass
                with self._stats_lock:
                    self._stats["total_calls"] += 1
                return self._cache[text]

        start = time.perf_counter()
        is_real = False
        try:
            if self._backend == "ollama":
                result, is_real = self._embed_ollama(text), True
            elif self._backend == "sentence-transformers":
                result, is_real = self._embed_st(text), True
            else:
                result, is_real = self._embed_fallback(text), False
        except Exception as e:
            logger.error("Embedding error: %s", e)
            raise  # 不返回伪向量 — 调用方 catch 后走降级（None → BM25-only）

        if is_real:
            with self._cache_lock:
                if text in self._cache:
                    return self._cache[text]
                self._cache[text] = result
                self._cache_order.insert(0, text)
                if len(self._cache) > self._cache_maxsize:
                    oldest = self._cache_order.pop()
                    del self._cache[oldest]
                with self._stats_lock:
                    self._stats["cache_misses"] += 1
                    self._stats["total_calls"] += 1
                    elapsed = time.perf_counter() - start
                    self._stats["total_time_sec"] += elapsed
        else:
            with self._stats_lock:
                self._stats["total_calls"] += 1
                self._stats["failures"] += 1
                elapsed = time.perf_counter() - start
                self._stats["total_time_sec"] += elapsed
            with self._stats_lock:
                self._stats["fallback_used"] += 1
        return result

    def _embed_ollama(self, text):
        payload = json.dumps({"model": self._model, "prompt": text[:4096], "keep_alive": -1}).encode()
        for attempt in range(self._OLLAMA_MAX_RETRIES + 1):
            try:
                if self._http_pool:
                    resp = self._http_pool.request("POST", f"{self._url}/api/embeddings",
                                                   body=payload, headers={"Content-Type": "application/json"})
                    if resp.status != 200:
                        raise ConnectionError(f"HTTP {resp.status}")
                    data = json.loads(resp.data)
                else:
                    import urllib.request
                    req = urllib.request.Request(f"{self._url}/api/embeddings", data=payload,
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=self._OLLAMA_TIMEOUT) as resp:
                        data = json.loads(resp.read())
                emb = data.get("embedding", [])
                if not emb or not isinstance(emb, list):
                    raise ValueError("Invalid embedding response")
                self._detect_dimension(emb)
                return emb
            except (ConnectionError, Urllib3HTTPError, TimeoutError) as e:
                if self._http_pool:
                    self._http_pool.clear()
                if attempt < self._OLLAMA_MAX_RETRIES:
                    wait = 1.5 ** attempt
                    logger.warning("Ollama connection error (attempt %d/%d), clearing pool: %s",
                                   attempt + 1, self._OLLAMA_MAX_RETRIES + 1, e)
                    time.sleep(wait)
                else:
                    raise
            except Exception as e:
                if attempt < self._OLLAMA_MAX_RETRIES:
                    time.sleep(1.5 ** attempt)
                else:
                    raise

    def _embed_st(self, text):
        if not self._client:
            from sentence_transformers import SentenceTransformer
            self._client = SentenceTransformer(self._model)
        vec = self._client.encode(text, normalize_embeddings=True)
        self._detect_dimension(vec.tolist())
        return vec.tolist()

    def _embed_fallback(self, text):
        if not text:
            text = "fallback_zero"
        h = hashlib.md5(text.encode()).digest()
        seed = int.from_bytes(h[:4], "big")
        rng = random.Random(seed)
        dim = self._dim if self._dim_detected else _EMBED_DIM
        return [rng.random() * 2 - 1 for _ in range(dim)]

    def _detect_dimension(self, emb):
        if not self._dim_detected and emb:
            self._dim = len(emb)
            self._dim_detected = True
            logger.info("Embedding dimension detected: %d", self._dim)
