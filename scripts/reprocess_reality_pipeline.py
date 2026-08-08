#!/usr/bin/env python3
"""winker 话题管线数据彻底重跑（reality 时代，2026-08-08）。

管线（用户指定顺序，逐 session 按时间先后）：
  ① 判断话题切换位置  TopicGradeManager.detect + grade_on_switch
                      （v7.1 semantic_fct_embeddings：重跑无运行时 cache →
                       _compute_centroids 走 _extract_fct_semantic_text 回退，
                       与 F-stage 剥键名语义文本输入一致 = 去 json 框 embedding）
  ② 识别注入 reality  pick_injection_realities(块首提问, q_emb, profile,
                      exclude_session_id=当前session, limit=3)
  ③ 生成 strand       summarize_topic_chunk(candidate_themes=注入集) → 4B
                      → write_strand_summary + update_strand_centroid
  ④ 聚合 reality      run_reality_merge(priority_realities=注入集)
  ⑤ 生成 graphify 边  sync_realities_to_graph + record_block_cooccurrences
                      + sync_cooccurrences_to_graph
  ⑥ 每 session 一轮精炼  IdleRefinementDaemon()._run_refinement_cycle()

用法：
    HERMES_HOME=/home/i1j/.hermes/profiles/winker \
    python3 scripts/reprocess_reality_pipeline.py --profile winker \
        [--db <ca_topics.db>] [--limit N] [--sessions a,b] [--no-refine] [--backup]

注意：
  - 默认备份后清空重建（用户确认破坏性操作）；--backup 显式确认
  - HERMES_HOME 必须指向目标 profile（Config.HERMES_PROFILE 由 basename 推导，
    refinement fallback 尊重 env；不设会误读其它 profile 库）
  - 安全校验：profile 目录下必须存在 state.db 与 ca_cache
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("CA_REGEN")

PROFILES_ROOT = Path.home() / ".hermes" / "profiles"


# ── session 发现与数据读取（复用 reprocess_old_sessions.py 骨架）──

def discover_sessions(ca_cache: Path, min_size: int = 50000) -> List[str]:
    """发现 ca_cache 中所有有实际数据的 session。"""
    sessions = []
    for f in sorted(ca_cache.glob("*.db")):
        if f.name in ("ca_topics.db", "flash_pilot.db"):
            continue
        if any(kw in f.name for kw in (".bak", "exp", "test_", "_test_",
                                       "cr009_", "recall_", "rel_test",
                                       "far_test", "noswitch")):
            continue
        if f.stat().st_size < min_size:
            continue
        sid = f.stem
        try:
            conn = sqlite3.connect(str(f))
            cnt = conn.execute("SELECT COUNT(*) FROM turn_stream").fetchone()[0]
            conn.close()
            if cnt > 3:
                sessions.append(sid)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            continue
    return sessions


def get_turn_ca_rows(ca_cache: Path, session_id: str, turn: int) -> List[tuple]:
    """从 per-session CA DB 读取该轮的 ca_rows（(seq, role, fin, tc, Elm, Fct, Hdl)）。"""
    db = ca_cache / f"{session_id}.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        try:
            cur = conn.execute(
                "SELECT seq, role, finish_reason, tool_calls_json, Elm, Fct, Hdl "
                "FROM turn_stream WHERE turn=? ORDER BY seq", (turn,))
            return list(cur.fetchall())
        except sqlite3.OperationalError:
            cur = conn.execute(
                "SELECT seq, role, finish_reason, tool_calls_json, NULL as Elm, "
                "Fct, Hdl FROM turn_stream WHERE turn=? ORDER BY seq", (turn,))
            return list(cur.fetchall())
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def session_first_ts(ca_cache: Path, session_id: str) -> float:
    """session 最早 written_at（时间排序锚点）。"""
    db = ca_cache / f"{session_id}.db"
    try:
        conn = sqlite3.connect(str(db))
        ts = conn.execute(
            "SELECT MIN(written_at) FROM turn_stream WHERE written_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return ts or 0.0
    except Exception:
        return 0.0


def block_first_query(ca_cache: Path, session_id: str, turns: List[int]) -> str:
    """块首提问 = 块内最早 turn 的 seq=0 user Elm（对齐 __init__:738）。"""
    for t in sorted(turns):
        for _seq, _role, _fin, _tc, _elm, _fct, _hdl in get_turn_ca_rows(
                ca_cache, session_id, t):
            if _seq == 0 and _role == "user" and _elm:
                return str(_elm).strip()[:500]
    return ""


# ── 管线执行 ──

class _LightStore:
    """TopicGradeManager 需要的轻量 store（.conn + .session_id）。"""

    def __init__(self, db_path: Path, session_id: str):
        self.conn = sqlite3.connect(str(db_path), timeout=10,
                                    check_same_thread=False)
        self.session_id = session_id

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def run_session_pipeline(
    sid: str,
    ca_cache: Path,
    db_path: Path,
    profile: str,
    graph_path: Path,
    embed_fn,
    llm_profile: str,
) -> Dict[str, Any]:
    """对单个 session 执行完整管线：detect → 逐块 inject+strand+merge+graphify。"""
    from topic_manager import TopicGradeManager

    stats = {"session_id": sid, "topics": 0, "strands": 0,
             "merged": 0, "created": 0, "failed": 0, "blocks": 0}

    # ① 话题切换判断（detect + grade_on_switch，真实 embed）
    store = _LightStore(ca_cache / f"{sid}.db", sid)
    topic_mgr = TopicGradeManager(store, embed_fn)
    topic_mgr._turn_to_topic = {}
    topic_mgr._topic_text_profiles = {}
    topic_mgr._next_topic_id = 1
    topic_mgr._topic_data = {}
    topic_mgr._current_topic_id = None
    topic_mgr._last_processed_turn = 0

    try:
        conn = sqlite3.connect(str(ca_cache / f"{sid}.db"))
        turns = [r[0] for r in conn.execute(
            "SELECT DISTINCT turn FROM turn_stream ORDER BY turn").fetchall()]
        conn.close()
    except sqlite3.OperationalError:
        store.close()
        return stats
    logger.info("  Session %s: %d turns", sid, len(turns))

    for t in turns:
        ca_rows = get_turn_ca_rows(ca_cache, sid, t)
        # user_msg：seq=0 user Elm（detect 需要 user 文本做 Jaccard/确认判断）
        user_msg = ""
        for row in ca_rows:
            if row[0] == 0 and row[1] == "user":
                user_msg = str(row[4] or "").strip()
                break
        switched = topic_mgr.detect(t, ca_rows, user_msg)
        if switched:
            q_emb = None
            try:
                q_emb = embed_fn.embed(user_msg) if user_msg else None
            except Exception as exc:
                logger.warning("  embed failed on switch turn %d: %s", t, exc)
            topic_mgr.grade_on_switch(q_emb, user_msg)

    # 话题块分组
    topic_groups: Dict[int, List[int]] = {}
    for t, tid in topic_mgr._turn_to_topic.items():
        topic_groups.setdefault(tid, []).append(t)
    logger.info("  Topics detected: %d", len(topic_groups))
    stats["topics"] = len(topic_groups)

    from ca.store import (collect_turn_fcts, load_all_realities,
                          get_wiki_threshold, write_strand_summary,
                          update_strand_centroid, upsert_session_meta)
    from ca.topic_summary import summarize_topic_chunk, _flatten_strand_ooda
    from ca.inject import pick_injection_realities
    from ca.reality import run_reality_merge
    from ca.graphify_sync import (sync_realities_to_graph,
                                  sync_cooccurrences_to_graph)
    from ca.store import query_cooccurrences, record_block_cooccurrences

    _store = _LightStore(ca_cache / f"{sid}.db", sid)
    try:
        for tid, tlist in sorted(topic_groups.items()):
            turns_data = collect_turn_fcts(_store, sid, tlist)
            if not turns_data:
                logger.info("  Topic %d: no Fct data, skip", tid)
                continue
            query_text = block_first_query(ca_cache, sid, tlist)

            # ② 识别注入 reality（块首提问 → 提问云形心 4B 拣选）
            inject_entries: List[dict] = []
            try:
                q_emb = embed_fn.embed(query_text) if query_text else None
                if q_emb:
                    inject_entries = pick_injection_realities(
                        query_text, q_emb, profile,
                        limit=3, exclude_session_id=sid, db_path=db_path)
                    logger.info("  Topic %d: injected %d reality (query=%.40s)",
                                tid, len(inject_entries), query_text)
            except Exception as exc:
                logger.warning("  Topic %d: injection failed: %s", tid, exc)

            # ③ 生成 strand（4B）
            summary = summarize_topic_chunk(
                turns_data, title="",
                candidate_themes=inject_entries or None,
                max_chars=4000,
            )
            if not summary:
                logger.warning("  Topic %d: summarization failed", tid)
                continue

            _EMPTY_TITLES = {"", "无", "无新增", "无新内容"}
            _title = summary.get("title", "")
            _strands = summary.get("strands", [])
            _changes = summary.get("changes", [])
            _key_facts = summary.get("key_facts", [])
            _has_content = bool(
                any(st.get("ooda") for st in _strands if isinstance(st, dict))
            ) or bool(_changes) or bool(_key_facts)
            if (not summary.get("consumable", True)
                    or not _title or _title.strip() in _EMPTY_TITLES
                    or not _has_content):
                write_strand_summary(
                    sid, tid, profile,
                    hdl=f"skip: {_title[:60] if _title else 'empty'}",
                    turns=tlist, ooda_json="{}", changes_json="[]",
                    key_facts_json="[]", status="skip", db_path=db_path)
                logger.info("  Topic %d: hollow, skip", tid)
                continue

            theme_ready: List[dict] = []
            written = 0
            for st in _strands:
                if not isinstance(st, dict):
                    continue
                st_hdl = st.get("hdl", "") or summary.get("hdl", "")
                st_turns = st.get("turns") or tlist
                st_ooda = st.get("ooda") or {}
                strand_id = write_strand_summary(
                    sid, tid, profile,
                    hdl=st_hdl, turns=st_turns,
                    ooda_json=json.dumps(st_ooda, ensure_ascii=False),
                    changes_json=json.dumps(
                        _flatten_strand_ooda([st]), ensure_ascii=False),
                    key_facts_json=json.dumps(
                        summary.get("key_facts", []), ensure_ascii=False),
                    status=summary.get("status", "completed"),
                    db_path=db_path,
                )
                if strand_id is None:
                    continue
                try:
                    embed_text = " ".join(filter(None, [
                        st_hdl,
                        *[item for group in st_ooda.values()
                          if isinstance(group, list)
                          for item in group if isinstance(item, str)],
                    ]))
                    if embed_text.strip():
                        vec = embed_fn.embed(embed_text[:500])
                        if vec:
                            update_strand_centroid(
                                strand_id,
                                json.dumps(vec, ensure_ascii=False),
                                db_path=db_path)
                except Exception as exc:
                    logger.warning("  strand %d centroid failed: %s",
                                   strand_id, exc)
                theme_ready.append({
                    "hdl": st_hdl, "turns": st_turns,
                    "topic_id": tid, "session_id": sid,
                    "strand_id": strand_id, "ooda": st_ooda,
                    "changes": _flatten_strand_ooda([st]),
                    "key_facts": summary.get("key_facts", []),
                    "theme_ref": st.get("theme_ref"),
                    "query_text": query_text,
                })
                written += 1
            stats["strands"] += written
            stats["blocks"] += 1
            if written:
                upsert_session_meta(sid, profile, tlist[-1], tid,
                                    db_path=db_path)

            # ④ 聚合 reality（priority = 注入集）
            if theme_ready:
                try:
                    existing = load_all_realities(db_path=db_path)
                    merge_stats = run_reality_merge(
                        theme_ready, existing,
                        embed_client=embed_fn,
                        threshold=get_wiki_threshold(db_path=db_path),
                        max_chars=4000,
                        profile=profile,
                        priority_realities=inject_entries or None,
                        db_path=db_path,
                    )
                    stats["merged"] += merge_stats.get("merged", 0)
                    stats["created"] += merge_stats.get("created", 0)
                    stats["failed"] += merge_stats.get("failed", 0)
                    logger.info("  Topic %d: reality merge %s", tid, merge_stats)

                    # ⑤ graphify 边（增量 sync + 块级共现）
                    rid_list = merge_stats.get("reality_ids") or []
                    if rid_list:
                        sync_realities_to_graph(rid_list, graph_path,
                                                db_path=db_path)
                        record_block_cooccurrences(
                            sid, tid, rid_list, profile=profile,
                            db_path=db_path)
                        edges, _, _ = query_cooccurrences(
                            profile=profile, db_path=db_path)
                        if edges:
                            sync_cooccurrences_to_graph(edges, graph_path,
                                                        db_path=db_path)
                except Exception as exc:
                    logger.warning("  Topic %d: reality merge failed: %s",
                                   tid, exc)
    finally:
        _store.close()
        store.close()

    logger.info("  Session %s done: %s", sid, stats)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="winker 话题管线数据彻底重跑（reality 时代）")
    parser.add_argument("--profile", default="winker")
    parser.add_argument("--db", default="",
                        help="聚合库路径（默认 <profile>/ca_cache/ca_topics.db）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 个 session（时间序，试跑用）")
    parser.add_argument("--sessions", default="",
                        help="session 白名单（逗号分隔）")
    parser.add_argument("--no-refine", action="store_true",
                        help="跳过每 session 精炼轮")
    parser.add_argument("--backup", action="store_true",
                        help="备份并清空重建聚合库（破坏性，需显式确认）")
    args = parser.parse_args()

    profile = args.profile
    profile_dir = PROFILES_ROOT / profile
    ca_cache = profile_dir / "ca_cache"
    if not ca_cache.exists():
        logger.error("ca_cache not found: %s", ca_cache)
        sys.exit(2)

    db_path = Path(args.db) if args.db else ca_cache / "ca_topics.db"
    if not db_path.exists():
        logger.error("聚合库不存在: %s", db_path)
        sys.exit(2)

    # 安全校验：HERMES_HOME 必须指向目标 profile（refinement/store fallback）
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if not env_home or Path(env_home).resolve() != profile_dir.resolve():
        logger.error(
            "HERMES_HOME 未指向 %s（当前=%r）——refinement 等会误读其它 profile 库。\n"
            "  正确用法: HERMES_HOME=%s python3 scripts/reprocess_reality_pipeline.py %s",
            profile_dir, env_home, profile_dir, f"--profile {profile}")
        sys.exit(2)

    from ca.config import Config
    from ca.embedding import EmbeddingClient
    logger.info("Config.HERMES_PROFILE=%s (LLM=%s, EMBED=%s)",
                Config.HERMES_PROFILE, Config.LLM_ENDPOINT, Config.EMBED_ENDPOINT)

    # 备份 + 清空重建（显式确认）
    if args.backup:
        bak = db_path.with_name(f"{db_path.name}.bak_pre_regen_{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(str(db_path), str(bak))
        logger.info("备份: %s", bak)
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            for tbl in ("strand_summaries", "realities", "strand_to_reality",
                        "cooccurrence_events", "session_meta"):
                try:
                    n = conn.execute(f"DELETE FROM {tbl}").rowcount
                    logger.info("清空 %s: %d 行", tbl, n)
                except sqlite3.OperationalError as exc:
                    logger.warning("清空 %s 失败: %s", tbl, exc)
            conn.execute("DELETE FROM sqlite_sequence")
            conn.commit()
        finally:
            conn.close()

    # session 列表（时间先后）
    sessions = discover_sessions(ca_cache)
    if args.sessions:
        wl = set(args.sessions.split(","))
        sessions = [s for s in sessions if s in wl]
    sessions.sort(key=lambda s: session_first_ts(ca_cache, s))
    if args.limit:
        sessions = sessions[:args.limit]
    if not sessions:
        logger.info("无 session 可处理")
        return
    logger.info("处理 %d 个 session（时间序）: %s",
                len(sessions), [s[:12] for s in sessions])

    graph_path = (PROJECT_ROOT / "graphify-out" / "graph.json")
    if profile != "tester":
        graph_path = profile_dir / "plugins" / "ca_assembler" / \
            "graphify-out" / "graph.json"
    logger.info("graph.json: %s", graph_path)

    embed = EmbeddingClient()
    total = {"strands": 0, "merged": 0, "created": 0, "failed": 0, "blocks": 0}
    try:
        for sid in sessions:
            st = run_session_pipeline(sid, ca_cache, db_path, profile,
                                      graph_path, embed, profile)
            for k in total:
                total[k] += st.get(k, 0)

            # ⑥ 每 session 一轮精炼（默认执行；cycle 直接调用不受 REFINEMENT_ENABLED 限制）
            if not args.no_refine:
                try:
                    from ca.refinement import IdleRefinementDaemon
                    logger.info("  ── 精炼轮 for %s ──", sid)
                    IdleRefinementDaemon()._run_refinement_cycle()
                except Exception as exc:
                    logger.warning("  精炼轮失败: %s", exc)
    finally:
        embed.close()

    logger.info("=== 完成 === 总: %s", total)


if __name__ == "__main__":
    main()
