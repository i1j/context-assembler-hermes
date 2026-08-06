"""
ca/refinement.py — L4 空闲精炼管线 (v5.14)

设计决策: 34-idle-refinement.md
  viking://resources/projects/context-assembler/wiki/decisions/34-idle-refinement.md

职责：
  - 空闲守护线程（周期检查 + 条件触发）
  - Entry 内精炼（去冗余/纠错/矛盾合并 — 1 次 4B per entry）
  - Fct↔Wiki 交叉验证（重读 Fct → 4B 对比）
  - 僵尸清理（空 centroid / 死 source）
  - Entry 健康评分

集成：
  由 CAContextAssemblerPlugin._run_session_start_cleanup() 末尾懒启动。
  访问 ca_topics.db 直接通过 _get_topic_conn()（文件级 SQLite），不依赖引擎实例。
  嵌入向量通过独立的 EmbeddingClient 实例完成。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .store import (
    _get_topic_conn,
    write_refinement_meta,
    get_last_refinement_meta,
)

logger = logging.getLogger(__name__)

# ── 哨兵常量 ──
_EMPTY_TITLES = {"", "无", "无新增", "无新内容"}


# ═══════════════════════════════════════════════════════════════
# 空闲守护线程
# ═══════════════════════════════════════════════════════════════

class IdleRefinementDaemon:
    """L4 空闲精炼守护线程管理。

    生命周期：
      - start() — 懒启动单一线程（幂等）
      - stop()  — 设置停止事件，join(timeout)
      - 进程退出时 daemon=True 自动终结。
    """

    def __init__(self, plugin_ref: Any = None) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # plugin_ref: CAContextAssemblerPlugin 实例引用，用于获取 embed_client
        self._plugin_ref = plugin_ref

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._idle_loop,
            daemon=True,
            name="CA-L4-Refinement",
        )
        self._thread.start()
        logger.info("[CA_L4] Idle refinement daemon started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            logger.info("[CA_L4] Idle refinement daemon stopped")
        self._thread = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── 空闲循环 ──

    def _idle_loop(self) -> None:
        """主循环：休眠 → 检查 → 执行 → 休眠。"""
        while not self._stop_event.is_set():
            if self._should_run():
                self._run_refinement_cycle()
            # 休眠期间可被 stop_event 提前唤醒
            self._stop_event.wait(Config.REFINEMENT_CHECK_INTERVAL)

    def _should_run(self) -> bool:
        """检查是否满足触发条件。

        条件：全局所有 session 的最大轮次之和 - last_refined_turn
            >= REFINEMENT_MIN_NEW_TURNS
        """
        if not Config.REFINEMENT_ENABLED:
            return False

        last_meta = get_last_refinement_meta()
        last_refined_turn = last_meta.get("last_refined_turn", 0) if last_meta else 0

        current_global_turn = self._compute_global_turn_max()
        new_turns = current_global_turn - last_refined_turn

        if current_global_turn == 0:
            return False  # 还没有任何数据

        if new_turns >= Config.REFINEMENT_MIN_NEW_TURNS:
            logger.info("[CA_L4] Trigger: %d new turns (threshold=%d)",
                        new_turns, Config.REFINEMENT_MIN_NEW_TURNS)
            return True

        logger.debug("[CA_L4] Not triggered: %d new turns < %d",
                     new_turns, Config.REFINEMENT_MIN_NEW_TURNS)
        return False

    def _compute_global_turn_max(self) -> int:
        """遍历 ca_cache 目录，计算所有 session 的 max_turn 之和。"""
        from .store import max_turn_v5, SQLiteStore

        try:
            from hermes_constants import get_hermes_home
            cache_dir = Path(get_hermes_home()) / "ca_cache"
        except ImportError:
            cache_dir = Path.home() / ".hermes" / "ca_cache"

        if not cache_dir.exists():
            return 0

        total = 0
        for fpath in cache_dir.iterdir():
            if fpath.suffix != ".db":
                continue
            try:
                store = SQLiteStore(str(fpath))
                sid = fpath.stem
                mt = max_turn_v5(store, sid)
                total += mt
                store.close()
            except Exception:
                pass
        return total

    # ── 精炼轮次 ──

    def _run_refinement_cycle(self) -> None:
        """执行一轮完整精炼。"""
        t0 = time.monotonic()
        tasks_run: List[str] = []
        entries_reviewed = 0
        entries_modified = 0
        entries_split = 0
        fcts_cross_checked = 0
        inconsistencies = 0
        associations_added = 0
        graphify_synced = 0

        try:
            conn = _get_topic_conn()

            # Step 0: 并发会话保护
            active_sessions = self._get_active_sessions()

            # Step 1: Entry 内精炼
            if Config.REFINEMENT_INTERNAL_REFINE:
                tasks_run.append("internal_refine")
                refined, modified = self._run_internal_refine(conn, active_sessions)
                entries_reviewed += refined
                entries_modified += modified

            # Step 2: Fct↔Wiki 交叉验证
            if Config.REFINEMENT_CROSS_VALIDATE:
                tasks_run.append("cross_validate")
                checked, fixed = self._run_cross_validate(conn, active_sessions)
                fcts_cross_checked += checked
                inconsistencies += fixed
                entries_modified += fixed

            # Step 3: 僵尸清理
            if Config.REFINEMENT_HEALTH_SCORE:
                tasks_run.append("zombie_cleanup")
                fixed = self._run_zombie_cleanup(conn)
                entries_modified += fixed

            # Step 4: 健康评分
            if Config.REFINEMENT_HEALTH_SCORE:
                tasks_run.append("health_score")
                scored = self._run_health_score(conn)
                entries_reviewed += scored

            # Step 5: Graphify 增量同步
            if Config.REFINEMENT_GRAPHIFY_SYNC and entries_modified > 0:
                tasks_run.append("graphify_sync")
                self._run_graphify_sync()
                graphify_synced = 1

            # 写 refinement_meta 记录
            duration = time.monotonic() - t0
            write_refinement_meta(
                last_refined_turn=self._compute_global_turn_max(),
                tasks_run=tasks_run,
                entries_reviewed=entries_reviewed,
                entries_modified=entries_modified,
                entries_split=entries_split,
                fcts_cross_checked=fcts_cross_checked,
                inconsistencies=inconsistencies,
                associations_added=associations_added,
                graphify_synced=graphify_synced,
                duration_sec=round(duration, 3),
                status="completed",
            )
            logger.info("[CA_L4] Cycle done: %d entries reviewed, %d modified "
                        "(%d cross-checked, %d inconsistencies, %.1fs)",
                        entries_reviewed, entries_modified,
                        fcts_cross_checked, inconsistencies, duration)

        except Exception as exc:
            duration = time.monotonic() - t0
            logger.warning("[CA_L4] Cycle failed after %.1fs: %s", duration, exc)
            try:
                write_refinement_meta(
                    last_refined_turn=self._compute_global_turn_max(),
                    tasks_run=tasks_run,
                    status="aborted",
                    duration_sec=round(duration, 3),
                )
            except Exception:
                pass

    # ── Step 0: 并发会话保护 ──

    def _get_active_sessions(self) -> set[str]:
        """获取当前活跃的 session ID 集合。"""
        if self._plugin_ref is not None:
            plugin = self._plugin_ref
            if hasattr(plugin, "_session_id") and plugin._session_id:
                return {plugin._session_id}
        return set()

    # ── Step 1: Entry 内精炼 ──

    def _load_refinement_candidates(self, conn, active_sessions: set[str],
                                     max_entries: int = 5) -> List[Dict[str, Any]]:
        """枚举当前轮可精炼的 reality（决策 41：theme → realities）。

        条件：
          - source_strands 非空
          - 未被活跃 session 引用
          - 按 updated_at 升序（最久未更新优先）
        entry 结构对齐旧 theme：title=name；changes=timeline 的 changes 条目；
        key_facts/open_items=current_status 段。
        """
        rows = conn.execute(
            "SELECT reality_id, name, hdl, current_status, timeline, "
            "       source_strands, centroid_json, updated_at "
            "FROM realities "
            "ORDER BY updated_at ASC LIMIT ?",
            (max_entries,),
        ).fetchall()

        candidates = []
        for r in rows:
            eid, name, hdl, cs_json, tl_json, src_json, cent_json, updated = r
            source_strands = self._safe_json(src_json, {})
            # 跳过全部 source 都在活跃 session 中的 reality
            if isinstance(source_strands, dict) and source_strands.keys() & active_sessions:
                continue
            if not isinstance(source_strands, dict):
                source_strands = {}
            cs = self._safe_json(cs_json, {})
            if not isinstance(cs, dict):
                cs = {}
            tl = self._safe_json(tl_json, [])
            changes = []
            if isinstance(tl, list):
                for e in tl:
                    if isinstance(e, dict) and e.get("changes"):
                        changes.extend(str(c) for c in e["changes"] if str(c).strip())
            overview = " ".join(filter(None, [name or "", hdl or ""]))
            candidates.append({
                "entry_id": eid,
                "title": name or "",
                "overview": overview,
                "changes": changes,
                "key_facts": cs.get("key_facts") or [],
                "open_items": cs.get("goals") or [],
                "source_strands": source_strands,
                "centroid_json": cent_json,
                "updated_at": updated,
            })
        return candidates

    def _run_internal_refine(self, conn, active_sessions: set[str]) -> tuple[int, int]:
        """为每个 entry 做内精炼。

        Returns:
            (reviewed, modified)
        """
        from .topic_summary import call_llm_for_summary

        reviewed = 0
        modified = 0
        max_per_cycle = Config.REFINEMENT_MAX_ENTRIES_PER_CYCLE
        candidates = self._load_refinement_candidates(conn, active_sessions, max_per_cycle)

        if not candidates:
            logger.info("[CA_L4] No entries to refine")
            return (0, 0)

        for entry in candidates:
            if self._stop_event.is_set():
                break
            reviewed += 1
            try:
                changed = self._refine_single_entry(conn, entry)
                if changed:
                    modified += 1
            except Exception as exc:
                logger.warning("[CA_L4] Internal refine entry %d failed: %s",
                               entry["entry_id"], exc)

        logger.info("[CA_L4] Internal refine: %d reviewed, %d modified",
                    reviewed, modified)
        return (reviewed, modified)

    def _refine_single_entry(self, conn, entry: Dict[str, Any]) -> bool:
        """对单个 entry 执行 4B 精炼。

        1. 构造 prompt（已有 entry 内容）
        2. 4B 调用 → 纠错/去重/合并
        3. 对比差异
        4. 有差异 → upsert + 重 embed
        """
        from .topic_summary import call_llm_for_summary

        # CR-2b: call_llm_for_summary 固定读 Config.TOPIC_SUMMARY_MAX_TOKENS
        # （topic_summary.py，v6.4.2 架构债），改 L1_MAX_TOKENS 对 num_predict 无效。
        # 原覆盖逻辑已删除（CR-2: 还会把 os.environ["CA_L1_MAX_TOKENS"] 永久钉在
        # "100" 污染进程，finally 只恢复属性不恢复 env）。
        prompt = _REFINE_INTERNAL_PROMPT.format(
            title=json.dumps(entry["title"], ensure_ascii=False),
            overview=json.dumps(entry["overview"], ensure_ascii=False),
            changes=json.dumps(entry["changes"], ensure_ascii=False),
            key_facts=json.dumps(entry["key_facts"], ensure_ascii=False),
            open_items=json.dumps(entry["open_items"], ensure_ascii=False),
        )

        result = call_llm_for_summary(prompt)
        if not result:
            logger.info("[CA_L4]   entry %d: 4B returned None, skipping",
                        entry["entry_id"])
            return False

        # 提取精炼结果
        new_overview = result.get("overview", entry["overview"])
        new_changes = result.get("changes", entry["changes"])
        new_key_facts = result.get("key_facts", entry["key_facts"])
        new_open_items = result.get("open_items", entry["open_items"])

        # 检查是否真的有变化
        if (new_overview == entry["overview"]
                and new_changes == entry["changes"]
                and new_key_facts == entry["key_facts"]
                and sorted(new_open_items) == sorted(entry["open_items"])):
            logger.info("[CA_L4]   entry %d: no changes after refine, skipping",
                        entry["entry_id"])
            # 更新 last_reviewed_turn anyway
            self._update_entry_refinement_meta(conn, entry["entry_id"])
            return False

        # 写入（决策 41：realities 表）
        centroid_json = self._compute_centroid(entry["entry_id"],
                                                new_overview, new_key_facts)
        # 精炼回写 reality：current_status 更新 + timeline 追加 changes 条目
        from .store import load_all_realities, update_reality
        current = next((r for r in load_all_realities() if r["reality_id"] == entry["entry_id"]), None)
        new_cs = dict(current.get("current_status") or {}) if current else {}
        if new_key_facts is not None:
            new_cs["key_facts"] = list(new_key_facts)
        if new_open_items is not None:
            new_cs["goals"] = list(new_open_items)
        update_reality(
            reality_id=entry["entry_id"],
            name=entry["title"] or None,
            current_status=new_cs,
            changes=[str(c) for c in new_changes if str(c).strip()],
            centroid_json=centroid_json,
            db_path=conn,
        )
        self._update_entry_refinement_meta(conn, entry["entry_id"])
        logger.info("[CA_L4]   entry %d: refined (overview=%d chars, %d changes, %d facts)",
                    entry["entry_id"],
                    len(new_overview), len(new_changes), len(new_key_facts))
        return True

    def _compute_centroid(self, entry_id: int, overview: str,
                          key_facts: list) -> str:
        """embed(overview + key_facts) 返回 centroid JSON。"""
        try:
            from .embedding import EmbeddingClient
            text = " ".join(filter(None, [overview, *key_facts]))
            if not text.strip():
                return "null"
            embed_client = EmbeddingClient()
            vec = embed_client.embed(text)
            if vec:
                return json.dumps(vec, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[CA_L4]   entry %d: centroid recompute failed: %s",
                          entry_id, exc)
        return "null"

    def _update_entry_refinement_meta(self, conn, entry_id: int) -> None:
        """更新 reality 的精炼元数据列（决策 41）。"""
        now = time.time()
        try:
            conn.execute(
                "UPDATE realities SET "
                "  reviewed_at=?, "
                "  last_reviewed_turn=? "
                "WHERE reality_id=?",
                (now, self._compute_global_turn_max(), entry_id),
            )
            conn.commit()
        except Exception:
            pass

    # ── Step 2: Fct↔Wiki 交叉验证 ──

    def _run_cross_validate(self, conn, active_sessions: set[str]) -> tuple[int, int]:
        """对每 entry 的 source_strands 做交叉验证。

        Returns:
            (checked, fixed)
        """
        from .topic_summary import call_llm_for_summary

        checked = 0
        fixed = 0
        max_per_cycle = Config.REFINEMENT_MAX_ENTRIES_PER_CYCLE
        candidates = self._load_refinement_candidates(conn, active_sessions, max_per_cycle)

        for entry in candidates:
            if self._stop_event.is_set():
                break
            source_strands = entry["source_strands"]
            if not isinstance(source_strands, dict) or not source_strands:
                continue

            for sid, strand_ids in source_strands.items():
                # 跳过活跃 session 的 source
                if sid in active_sessions:
                    continue
                if not isinstance(strand_ids, list):
                    continue
                try:
                    inconsistency = self._check_single_source(conn, entry, sid, strand_ids)
                    if inconsistency:
                        fixed += 1
                        # 修了 entry 后更新
                        self._update_entry_refinement_meta(conn, entry["entry_id"])
                    checked += len(strand_ids)
                except Exception as exc:
                    logger.warning("[CA_L4]   cross-val entry %d session %s failed: %s",
                                  entry["entry_id"], sid[:8], exc)

        if checked > 0:
            logger.info("[CA_L4] Cross-validate: %d strands checked, %d inconsistencies fixed",
                        checked, fixed)
        return (checked, fixed)

    def _check_single_source(self, conn, entry: Dict[str, Any], session_id: str,
                             strand_ids: list[int]) -> bool:
        """检查单个 session 的 Fct 数据是否与 entry key_facts 一致。

        v6.4: 入参从 topic_id 列表改为 strand_id 列表——
        先从 strand_summaries 解析各 strand 的 turns，再读 Fct。

        Returns:
            True if inconsistency detected and entry was updated.

        CR-8: strand_ids 为空 → 提前返回（否则 `IN ()` 生成 SQL 语法错误）。
        """
        from .store import collect_turn_fcts, SQLiteStore, _get_topic_conn
        from .topic_summary import call_llm_for_summary

        if not strand_ids:
            return False

        try:
            from hermes_constants import get_hermes_home
            cache_dir = Path(get_hermes_home()) / "ca_cache"
        except ImportError:
            cache_dir = Path.home() / ".hermes" / "ca_cache"

        db_path = cache_dir / f"{session_id}.db"
        if not db_path.exists():
            return False

        # v6.4: strand_id → turns 解析（strand_summaries.turns 是 JSON 数组）
        turns: list[int] = []
        try:
            tconn = _get_topic_conn()
            placeholders = ",".join("?" for _ in strand_ids)
            rows = tconn.execute(
                f"SELECT turns FROM strand_summaries WHERE strand_id IN ({placeholders})",
                strand_ids,
            ).fetchall()
            for (turns_json,) in rows:
                try:
                    turns.extend(json.loads(turns_json) if turns_json else [])
                except (json.JSONDecodeError, TypeError):
                    continue
        except sqlite3.Error as exc:
            logger.warning("[CA_L4]   strand→turns resolve failed: %s", exc)
            return False
        if not turns:
            return False

        store = SQLiteStore(str(db_path))
        try:
            turns_data = collect_turn_fcts(store, session_id, turns)
        finally:
            store.close()

        if not turns_data:
            return False

        # 构造 prompt
        prompt = _CROSS_VALIDATE_PROMPT.format(
            entry_title=json.dumps(entry["title"], ensure_ascii=False),
            entry_facts=json.dumps(entry["key_facts"], ensure_ascii=False),
            fct_data=json.dumps(turns_data, ensure_ascii=False),
        )

        result = call_llm_for_summary(prompt)
        if not result:
            return False

        inconsistent = result.get("inconsistent", False)
        if not inconsistent:
            return False

        # 有差异 → 修正 entry
        new_facts = result.get("corrected_facts", entry["key_facts"])
        new_changes = result.get("corrected_changes", entry["changes"])
        new_open = result.get("corrected_open_items", entry["open_items"])

        if (new_facts == entry["key_facts"]
                and new_changes == entry["changes"]
                and new_open == entry["open_items"]):
            return False

        centroid_json = self._compute_centroid(entry["entry_id"],
                                                entry["overview"], new_facts)
        from .store import load_all_realities, update_reality
        current = next((r for r in load_all_realities() if r["reality_id"] == entry["entry_id"]), None)
        new_cs = dict(current.get("current_status") or {}) if current else {}
        new_cs["key_facts"] = list(new_facts)
        new_cs["goals"] = list(new_open)
        update_reality(
            reality_id=entry["entry_id"],
            current_status=new_cs,
            changes=[str(c) for c in new_changes if str(c).strip()],
            centroid_json=centroid_json,
            db_path=conn,
        )
        logger.info("[CA_L4]   entry %d: cross-validate fixed %d facts",
                    entry["entry_id"], len(new_facts))
        return True

    # ── Step 3: 僵尸清理 ──

    def _run_zombie_cleanup(self, conn) -> int:
        """清理空 centroid / 死 source。

        Returns:
            fixed count
        """
        fixed = 0

        rows = conn.execute(
            "SELECT reality_id, centroid_json, source_strands FROM realities"
        ).fetchall()

        for eid, cent_json, src_json in rows:
            try:
                # 空 centroid → 重 embed（通过健康评分另行处理）。
                # CR-7: 不计数（原 fixed+=1 虚高 entries_modified → 误触发 graphify 同步）。
                if not cent_json or cent_json == "null" or cent_json == "[]":
                    pass

                # 死 source
                src_strands = self._safe_json(src_json, {})
                if not isinstance(src_strands, dict):
                    conn.execute(
                        "UPDATE realities SET source_strands='{}' WHERE reality_id=?",
                        (eid,),
                    )
                    fixed += 1
                    continue

                cleaned = self._clean_dead_sources(src_strands)
                if cleaned != src_strands:
                    conn.execute(
                        "UPDATE realities SET source_strands=? WHERE reality_id=?",
                        (json.dumps(cleaned, ensure_ascii=False), eid),
                    )
                    fixed += 1
            except Exception:
                pass

        if fixed > 0:
            conn.commit()
            logger.info("[CA_L4] Zombie cleanup: %d entries touched", fixed)
        return fixed

    def _clean_dead_sources(self, source_ids: dict) -> dict:
        """移除引用已删除 session DB 的 source。"""
        try:
            from hermes_constants import get_hermes_home
            cache_dir = Path(get_hermes_home()) / "ca_cache"
        except ImportError:
            cache_dir = Path.home() / ".hermes" / "ca_cache"

        cleaned = {}
        for sid, tids in source_ids.items():
            db_path = cache_dir / f"{sid}.db"
            if db_path.exists():
                cleaned[sid] = tids
        return cleaned

    # ── Step 4: 健康评分 ──

    def _run_health_score(self, conn) -> int:
        """为所有 reality 计算健康评分并标记（决策 41）。"""
        rows = conn.execute(
            "SELECT reality_id, name, current_status, timeline, "
            "       centroid_json, source_strands, updated_at, created_at "
            "FROM realities"
        ).fetchall()

        scored = 0
        for r in rows:
            try:
                eid, name, cs_json, tl_json, cent_json, src_json, updated, created = r
                source_strands = self._safe_json(src_json, {})
                cs = self._safe_json(cs_json, {})
                if not isinstance(cs, dict):
                    cs = {}
                tl = self._safe_json(tl_json, [])
                changes = []
                if isinstance(tl, list):
                    for e in tl:
                        if isinstance(e, dict) and e.get("changes"):
                            changes.extend(str(c) for c in e["changes"] if str(c).strip())
                facts = cs.get("key_facts") or []
                n_sources = len(source_strands) if isinstance(source_strands, dict) else 0

                score = self._compute_health_score(
                    n_sources=n_sources,
                    centroid_json=cent_json or "",
                    changes=changes,
                    key_facts=facts,
                    updated_at=updated or 0,
                    created_at=created or 0,
                )
                flagged = 1 if score < 0.3 else 0

                # 查 topic_count（决策 41: strand_to_reality 计数）
                cur = conn.execute(
                    "SELECT COUNT(*) FROM strand_to_reality WHERE reality_id=?",
                    (eid,),
                )
                topic_count = cur.fetchone()[0]

                conn.execute(
                    "UPDATE realities SET "
                    "  health_score=?, flagged_for_review=?, topic_count=? "
                    "WHERE reality_id=?",
                    (score, flagged, topic_count, eid),
                )
                scored += 1
            except Exception:
                pass

        if scored > 0:
            conn.commit()
            logger.info("[CA_L4] Health scored: %d entries (%.1f%% flagged)",
                        scored, scored and 100 * sum(
                            1 for row in rows
                            if self._compute_health_score(
                                n_sources=len(self._safe_json(row[6], {})),
                                centroid_json=row[5] or "",
                                changes=self._safe_json(row[3], []),
                                key_facts=self._safe_json(row[4], []),
                                updated_at=row[7] or 0,
                                created_at=row[8] or 0,
                            ) < 0.3
                        ) / scored)
        return scored

    @staticmethod
    def _compute_health_score(
        n_sources: int,
        centroid_json: str,
        changes: list,
        key_facts: list,
        updated_at: float,
        created_at: float,
    ) -> float:
        score = 1.0

        # 来源数量
        if n_sources >= 3:
            score *= 1.0
        elif n_sources == 2:
            score *= 0.9
        elif n_sources == 1:
            score *= 0.7
        else:
            score *= 0.3

        # 更新天数
        now = time.time()
        if updated_at > 0:
            days = (now - updated_at) / 86400
            if days > 90:
                score *= 0.6
            elif days > 30:
                score *= 0.8

        # centroid 有效
        if not centroid_json or centroid_json in ("null", "[]"):
            score *= 0.5

        # 有 changes/facts
        if not changes and not key_facts:
            score *= 0.3

        return round(min(max(score, 0), 1), 3)

    # ── Step 5: Graphify 增量同步 ──

    def _run_graphify_sync(self) -> None:
        """触发 wiki_to_graph.py 全量同步。"""
        try:
            import subprocess
            import sys
            script = Path(__file__).resolve().parent.parent / "scripts" / "wiki_to_graph.py"
            if script.exists():
                subprocess.Popen(
                    [sys.executable, str(script)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("[CA_L4] Graphify sync triggered")
        except Exception as exc:
            logger.warning("[CA_L4] Graphify sync failed: %s", exc)

    # ── 工具 ──

    @staticmethod
    def _safe_json(raw: Any, default: Any) -> Any:
        if raw is None:
            return default
        if isinstance(raw, (list, dict)):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return default
        return default


# ═══════════════════════════════════════════════════════════════
# 4B Prompt 模板
# ═══════════════════════════════════════════════════════════════

_REFINE_INTERNAL_PROMPT = """你是一个知识库精炼助手。你会收到一个现有知识条目，请执行以下操作：

1. **去冗余**：移除重复、模糊或低价值的信息
2. **纠错**：修正逻辑矛盾、过期信息
3. **合并**：将相似的 changes 或 key_facts 合并为更精炼的表达
4. **简化**：在不丢失核心信息的前提下让 overview 更简洁

**规则**：
- 保留条目原有的 title（不要改）
- changes = 过去发生了哪些实质性变更（每个是一个字符串）
- key_facts = 当前已确认的核心结论（每个是一个字符串）
- open_items = 仍待解决的问题（每个是一个字符串）
- 每条信息应该独立、具体、可验证
- 如果原内容已经很好，只做最小改动

=== 现有条目 ===

title: {title}
overview: {overview}
changes: {changes}
key_facts: {key_facts}
open_items: {open_items}

=== 输出格式（JSON，不要有其他文字） ===

{{
  "overview": "精炼后的 overview",
  "changes": ["change1", "change2", ...],
  "key_facts": ["fact1", "fact2", ...],
  "open_items": ["item1", "item2", ...]
}}
"""

_CROSS_VALIDATE_PROMPT = """你是一个数据一致性检测助手。你收到：
A) 一个知识条目（wiki entry）的标题和已有 key_facts
B) 该条目的原始 Fct 数据（话题摘要的原始输入）

请判断两边的核心内容是否一致。

- 如果 Fct 中包含 wiki key_facts 中没有的重要信息 → "inconsistent": true
- 如果 wiki key_facts 中包含了 Fct 不支持或矛盾的信息 → "inconsistent": true
- 如果两者一致 → "inconsistent": false

当 inconsistent=true，请输出修正后的 facts / changes / open_items。

=== 已有 wiki entry ===

title: {entry_title}
facts: {entry_facts}

=== 原始 Fct 数据 ===

{fct_data}

=== 输出格式（JSON，不要有其他文字） ===

{{
  "inconsistent": true,
  "corrected_facts": ["fact1", "fact2", ...],
  "corrected_changes": ["change1", ...],
  "corrected_open_items": ["item1", ...]
}}

当 inconsistent=false 时：
{{
  "inconsistent": false
}}
"""
