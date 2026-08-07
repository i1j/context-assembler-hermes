"""
ca/health.py — 健康检查与监控 (v4.4.0 alpha)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .embedding import EmbeddingClient
from .store import SQLiteStore


def _escape_label(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"')


class HealthCheck:
    @staticmethod
    def check_store(store: SQLiteStore) -> Dict[str, Any]:
        result = {"component": "SQLiteStore", "db_path": str(store._db_path)}
        try:
            start = time.perf_counter()
            conn = store._get_conn()
            conn.execute("SELECT 1")
            latency = (time.perf_counter() - start) * 1000
            result["latency_ms"] = round(latency, 2)
            # v5.0 已移除 checkpoint daemon（BUG-10: 引用已删属性 → 恒 unhealthy），
            # 指标不再有意义，仅保留 WAL/db size。
            wal_path = store._db_path.with_suffix(".db-wal")
            result["wal_size_mb"] = round(wal_path.stat().st_size / (1024 * 1024), 2) if wal_path.exists() else 0.0
            result["db_size_mb"] = round(store._db_path.stat().st_size / (1024 * 1024), 2) if store._db_path.exists() else 0.0
            result["status"] = "healthy"
        except Exception as e:
            result["status"] = "unhealthy"
            result["error"] = str(e)
        return result

    @staticmethod
    def check_embedding_client(client: EmbeddingClient) -> Dict[str, Any]:
        stats = client.get_stats()
        failures = stats.get("failures", 0)
        hit_rate = stats.get("cache_hit_rate", 0.0)
        status = "degraded" if failures > 10 or hit_rate < 0.5 else "healthy"
        return {"status": status, "component": "EmbeddingClient",
                "backend": client._backend, "model": client._model,
                "cache_size": len(client._cache),
                "cache_hit_rate": hit_rate, "failures": failures,
                "fallback_used": stats.get("fallback_used", 0)}

    @staticmethod
    def check_all(store, embedding_client=None) -> Dict[str, Any]:
        components = {"store": HealthCheck.check_store(store)}
        statuses = [components["store"]["status"]]
        if embedding_client:
            emb = HealthCheck.check_embedding_client(embedding_client)
            components["embedding"] = emb
            statuses.append(emb["status"])
        if "unhealthy" in statuses:
            overall = "unhealthy"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "healthy"
        return {"overall_status": overall, "timestamp": time.time(), "components": components}

    @staticmethod
    def get_prometheus_metrics(store, embedding_client=None) -> str:
        lines = []
        sh = HealthCheck.check_store(store)
        healthy = 1 if sh["status"] == "healthy" else 0
        safe_path = _escape_label(str(store._db_path))
        lines.append(f'# HELP ca_store_healthy Database health (1=healthy)\n'
                     f'# TYPE ca_store_healthy gauge\n'
                     f'ca_store_healthy{{db_path="{safe_path}"}} {healthy}')
        if "latency_ms" in sh:
            lines.append(f'# HELP ca_store_latency_ms Ping latency\n'
                         f'# TYPE ca_store_latency_ms gauge\n'
                         f'ca_store_latency_ms {sh["latency_ms"]}')
        if "wal_size_mb" in sh:
            lines.append(f'# HELP ca_store_wal_size_mb WAL file size\n'
                         f'# TYPE ca_store_wal_size_mb gauge\n'
                         f'ca_store_wal_size_mb {sh["wal_size_mb"]}')
        if embedding_client:
            eh = HealthCheck.check_embedding_client(embedding_client)
            lines.append(f'# HELP ca_embedding_cache_hit_rate Cache hit ratio\n'
                         f'# TYPE ca_embedding_cache_hit_rate gauge\n'
                         f'ca_embedding_cache_hit_rate {eh["cache_hit_rate"]}')
            lines.append(f'# HELP ca_embedding_failures Total failures\n'
                         f'# TYPE ca_embedding_failures gauge\n'
                         f'ca_embedding_failures {eh["failures"]}')
            lines.append(f'# HELP ca_embedding_fallback_used Total fallback invocations\n'
                         f'# TYPE ca_embedding_fallback_used gauge\n'
                         f'ca_embedding_fallback_used {eh["fallback_used"]}')
        return "\n".join(lines) + "\n"
