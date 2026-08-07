#!/usr/bin/env python3
"""sysadmin 结构重构：有内容 strand → reality（零 LLM/零 embed，2026-08-08）。

决策 41 reality 化的 sysadmin 落地（无 flash 金标准 → 结构构建）：
- 只对有内容 strand（hdl 非 "skip: 无新内容" 且 ooda 非空）建 reality
- 按 (session_id, topic_id) 块键分组（同块多 strand 归并，生成阶段宁分不并）
- reality 字段零 LLM 派生：
    name        = 组内最早 strand 的 hdl（固定事物名近似）
    hdl         = 组内最晚 strand 的 hdl（最新状态锚点，生长序）
    current_status = 四段映射（fill_current_status_fallback 规则）：
        current_state ← ooda「决策与方案」+「现象与问题」
        goals         ← ooda「后续行动」
        key_facts     ← key_facts_json（回退 changes[:5]）
        context       ← []
    timeline    = [{seq, topic_id, turns, session_id, overview, ts}]
    source_strands = {session_id: [strand_id...]}
- s2r：每条有内容 strand 一条
- 幂等：realities/s2r 已有数据时先打印拒绝（防误跑覆盖；--force 才清空重建）

用法：python3 scripts/migrate_strands_to_realities.py --profile sysadmin [--dry-run]
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

SKIP_HDL_PREFIX = "skip:"
OODA_SECTIONS = ("现象与问题", "背景与约束", "决策与方案", "后续行动")


def parse_json(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        v = default
    return v if isinstance(v, list) else default


def ooda_items(ooda_json) -> int:
    try:
        d = json.loads(ooda_json) if ooda_json else {}
    except (json.JSONDecodeError, TypeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    return sum(len(v) for v in d.values() if isinstance(v, list))


def min_turn(turns_json) -> int:
    try:
        t = json.loads(turns_json) if turns_json else []
    except (json.JSONDecodeError, TypeError):
        t = []
    nums = [int(x) for x in t if str(x).isdigit()]
    return min(nums) if nums else 0


def build_current_status(ooda_json, kf_json, changes_json) -> dict:
    try:
        ooda = json.loads(ooda_json) if ooda_json else {}
    except (json.JSONDecodeError, TypeError):
        ooda = {}
    if not isinstance(ooda, dict):
        ooda = {}
    cs = {
        "current_state": [
            str(x) for x in ooda.get("决策与方案") or [] if str(x).strip()]
        or [str(x) for x in ooda.get("现象与问题") or [] if str(x).strip()],
        "goals": [str(x) for x in ooda.get("后续行动") or [] if str(x).strip()],
        "key_facts": parse_json(kf_json, []),
        "context": [],
    }
    if not cs["key_facts"]:
        cs["key_facts"] = parse_json(changes_json, [])[:5]
    return cs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="sysadmin")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = (Path.home() / ".hermes" / "profiles" / args.profile
               / "ca_cache" / "ca_topics.db")
    print(f"DB: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    n_real = conn.execute("SELECT COUNT(*) FROM realities").fetchone()[0]
    n_s2r = conn.execute("SELECT COUNT(*) FROM strand_to_reality").fetchone()[0]
    if (n_real or n_s2r) and not args.dry_run:
        print(f"拒绝：realities={n_real} s2r={n_s2r} 已有数据（防误跑覆盖）。"
              f"如需重建请先手工清表。")
        conn.close()
        return 1

    rows = conn.execute(
        "SELECT strand_id, session_id, topic_id, hdl, turns, ooda_json, "
        "changes_json, key_facts_json, centroid_json FROM strand_summaries "
        "ORDER BY session_id, topic_id, strand_id").fetchall()

    content = []
    for r in rows:
        hdl = (r["hdl"] or "").strip()
        if hdl.lower().startswith(SKIP_HDL_PREFIX):
            continue
        if ooda_items(r["ooda_json"]) == 0 and not parse_json(r["key_facts_json"], []):
            continue  # 无内容（ooda 空且无 kf）
        content.append(r)
    print(f"有内容 strand: {len(content)} / {len(rows)}"
          f"（跳过 {len(rows) - len(content)} 无内容/skip）")

    blocks = {}
    for r in content:
        key = (r["session_id"], r["topic_id"])
        if key not in blocks:
            blocks[key] = []
        blocks[key].append(r)
    print(f"块数 (session_id, topic_id): {len(blocks)}")
    multi = sum(1 for b in blocks.values() if len(b) > 1)
    print(f"  多 strand 块: {multi}（同块归并）")

    if args.dry_run:
        print("[dry-run] 不写库")
        conn.close()
        return 0

    now = time.time()
    n_created = 0
    n_s2r_rows = 0
    for key, members in sorted(blocks.items()):
        sid, tpc = key
        members_sorted = sorted(members, key=lambda m: min_turn(m["turns"]))
        first, last = members_sorted[0], members_sorted[-1]
        name = (first["hdl"] or "").strip()[:100]
        hdl = (last["hdl"] or "").strip()
        cs = build_current_status(last["ooda_json"], last["key_facts_json"],
                                  last["changes_json"])
        turns_all = []
        for m in members_sorted:
            try:
                turns_all.extend(json.loads(m["turns"]) if m["turns"] else [])
            except (json.JSONDecodeError, TypeError):
                pass
        timeline = [{
            "seq": 1,
            "topic_id": tpc,
            "turns": turns_all,
            "session_id": sid,
            "overview": hdl,
            "ts": now,
        }]
        source_strands = {sid: [m["strand_id"] for m in members_sorted]}
        centroid = next((m["centroid_json"] for m in members_sorted
                         if m["centroid_json"]), None)
        cur = conn.execute(
            "INSERT INTO realities (name, hdl, current_status, timeline, "
            "source_strands, profile, centroid_json, query_count, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, hdl, json.dumps(cs, ensure_ascii=False),
             json.dumps(timeline, ensure_ascii=False),
             json.dumps(source_strands, ensure_ascii=False),
             args.profile, centroid, 0, now, now))
        rid = cur.lastrowid
        for m in members_sorted:
            conn.execute("INSERT OR IGNORE INTO strand_to_reality VALUES (?,?)",
                         (m["strand_id"], rid))
            n_s2r_rows += 1
        n_created += 1

    conn.commit()
    print(f"创建 realities: {n_created}，s2r: {n_s2r_rows}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
