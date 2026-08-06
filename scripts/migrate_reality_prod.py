"""决策 41 阶段1：生产库 reality 化迁移（幂等，可重跑）。

1. 建表：realities（flash schema + 维护字段 + query_centroid_json/query_count）+ strand_to_reality
2. s2r 映射（反向，逐生产 strand）：
   - 生产键不在 flash → 跳过（新块，实时 merge 消化）
   - flash 键 reality 唯一 → 直连
   - flash 键跨 reality → centroid 语义匹配（生产 centroid vs flash hdl+ooda embed，cos≥0.55）
     → 归位；低于阈值 → 宁缺勿错跳过
3. realities 导入：111 行（reality_id 保持）；source_strands 由生产 s2r 反推
   {"session_id": [生产 strand_id...]}（与 s2r 一致）
4. cooc 迁移：cooccurrence_events.reality_a/b（现为 theme_id）→ 真 reality_id
   （经 theme.source_strands → s2r → reality 集合；无映射 → 丢弃并记录）
5. 提问云形心：成员 strand 块首提问（state.db turns[0] 定位）→ embed → 形心
   → realities.query_centroid_json + query_count
6. sqlite_sequence（realities）设为 flash 最大 reality_id + 1

用法：
  python3 scripts/migrate_reality_prod.py [--profile tester] [--flash <path>] [--no-embed]
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

PROD_DEFAULT = Path.home() / ".hermes" / "profiles" / "{profile}" / "ca_cache" / "ca_topics.db"
FLASH_DEFAULT = Path.home() / ".hermes" / "profiles" / "{profile}" / "ca_cache" / "flash_pilot.db"
STATE_DEFAULT = Path.home() / ".hermes" / "profiles" / "{profile}" / "state.db"
MIN_Q_LEN = 4
MATCH_COS = 0.55  # centroid 语义匹配阈值（宁缺勿错，实测 91.8% 成功率）

REALITIES_DDL = """
CREATE TABLE IF NOT EXISTS realities (
    reality_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    hdl             TEXT,
    current_status  TEXT    DEFAULT '{}',
    timeline        TEXT    DEFAULT '[]',
    source_strands  TEXT    DEFAULT '{}',
    profile         TEXT    NOT NULL DEFAULT '',
    centroid_json   TEXT,
    query_centroid_json TEXT,
    query_count     INTEGER DEFAULT 0,
    health_score    REAL,
    flagged_for_review INTEGER DEFAULT 0,
    topic_count     INTEGER DEFAULT 0,
    reviewed_at     REAL,
    last_reviewed_turn INTEGER DEFAULT 0,
    created_at      REAL,
    updated_at      REAL
);
CREATE TABLE IF NOT EXISTS strand_to_reality (
    strand_id   INTEGER NOT NULL,
    reality_id  INTEGER NOT NULL,
    PRIMARY KEY (strand_id)
);
CREATE INDEX IF NOT EXISTS idx_s2r_reality ON strand_to_reality (reality_id);
"""


def cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def load_flash_strand_emb(flash, need_ids):
    """embed flash strand 的 hdl+ooda 文本（缓存）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ca.embedding import EmbeddingClient
    ec = EmbeddingClient()
    emb = {}
    for sid, hdl, ooda in flash.execute(
            "SELECT strand_id, hdl, ooda_json FROM strands"):
        if sid not in need_ids:
            continue
        try:
            o = json.loads(ooda) if ooda else {}
        except Exception:
            o = {}
        items = [hdl or ""]
        for v in o.values():
            if isinstance(v, list):
                items.extend(str(i) for i in v)
            else:
                items.append(str(v))
        text = " ".join(filter(None, items))[:500]
        v = ec.embed(text)
        if v:
            emb[sid] = v
    return emb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="tester")
    ap.add_argument("--flash", default=None)
    ap.add_argument("--no-embed", action="store_true", help="跳过提问云形心计算")
    args = ap.parse_args()

    prod_path = PROD_DEFAULT.as_posix().format(profile=args.profile)
    flash_path = args.flash or FLASH_DEFAULT.as_posix().format(profile=args.profile)
    state_path = STATE_DEFAULT.as_posix().format(profile=args.profile)

    if not Path(prod_path).exists():
        print(f"生产库不存在: {prod_path}"); sys.exit(1)
    if not Path(flash_path).exists():
        print(f"flash_pilot.db 不存在: {flash_path}"); sys.exit(1)

    prod = sqlite3.connect(prod_path)
    flash = sqlite3.connect(flash_path)
    state = sqlite3.connect(state_path) if Path(state_path).exists() else None

    # ── 1. 建表 ──
    prod.executescript(REALITIES_DDL)
    prod.commit()
    print("[1] realities + strand_to_reality 表就绪")

    # ── 2. s2r 反向映射 ──
    s2r_flash = {r[0]: r[1] for r in flash.execute(
        "SELECT strand_id, reality_id FROM strand_to_reality")}
    fkeys = {}
    for sid, sess, tpc in flash.execute(
            "SELECT strand_id, session_id, topic_id FROM strands"):
        fkeys.setdefault((sess, tpc), []).append(sid)

    prod.execute("DELETE FROM realities")
    prod.execute("DELETE FROM strand_to_reality")

    direct, matched, skipped_nokey, skipped_low = 0, 0, 0, 0
    cross_ids = set()  # 跨 reality 块中需要 flash embed 的 strand
    pending_cross = []  # (psid, sess, tpc, centroid_json)
    for psid, sess, tpc, cent in prod.execute(
            "SELECT strand_id, session_id, topic_id, centroid_json "
            "FROM strand_summaries WHERE profile=?", (args.profile,)):
        fsids = fkeys.get((sess, tpc))
        if not fsids:
            skipped_nokey += 1
            continue
        rids = {s2r_flash.get(f) for f in fsids}
        rids.discard(None)
        if len(rids) == 1:
            prod.execute("INSERT OR IGNORE INTO strand_to_reality VALUES (?,?)",
                         (psid, next(iter(rids))))
            direct += 1
            continue
        # 跨 reality → centroid 匹配（延迟到 flash embed 后）
        pending_cross.append((psid, sess, tpc, cent))
        for f in fsids:
            cross_ids.add(f)
    prod.commit()
    print(f"[2a] 直连 {direct}，新块跳过 {skipped_nokey}，跨块待匹配 {len(pending_cross)}")

    if pending_cross:
        flash_emb = load_flash_strand_emb(flash, cross_ids)
        for psid, sess, tpc, cent in pending_cross:
            pv = None
            try:
                pv = json.loads(cent) if cent else None
            except Exception:
                pv = None
            best_r, best_c = None, 0.0
            for fsid in fkeys.get((sess, tpc), []):
                fv = flash_emb.get(fsid)
                if not fv or pv is None:
                    continue
                c = cos(pv, fv)
                if c > best_c:
                    best_r, best_c = s2r_flash.get(fsid), c
            if best_r is not None and best_c >= MATCH_COS:
                prod.execute("INSERT OR IGNORE INTO strand_to_reality VALUES (?,?)",
                             (psid, best_r))
                matched += 1
            else:
                skipped_low += 1
        prod.commit()
        print(f"[2b] centroid 匹配 {matched}（阈值 {MATCH_COS}），低分跳过 {skipped_low}")

    n_s2r = prod.execute("SELECT COUNT(*) FROM strand_to_reality").fetchone()[0]
    print(f"     s2r 总计: {n_s2r}")

    # ── 3. realities 导入（source_strands 由生产 s2r 反推）──
    for rid, name, hdl, cs, tl, created, updated in flash.execute(
            "SELECT reality_id, name, hdl, current_status, timeline, "
            "       created_at, updated_at FROM realities"):
        rows = prod.execute(
            "SELECT s.session_id, s.strand_id FROM strand_to_reality st "
            "JOIN strand_summaries s ON s.strand_id = st.strand_id "
            "WHERE st.reality_id=? ORDER BY s.strand_id", (rid,)).fetchall()
        ss_dict = {}
        for sess, psid in rows:
            ss_dict.setdefault(sess, []).append(psid)
        prod.execute(
            "INSERT INTO realities (reality_id, name, hdl, current_status, timeline, "
            "source_strands, profile, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, name, hdl, cs, tl, json.dumps(ss_dict, ensure_ascii=False),
             args.profile, created or time.time(), updated or time.time()))
    prod.commit()
    n_realities = prod.execute("SELECT COUNT(*) FROM realities").fetchone()[0]
    print(f"[3] realities 导入: {n_realities} 行")

    # ── 4. cooc 迁移：theme_id → reality_id ──
    theme2realities = {}
    for tid, src in prod.execute(
            "SELECT theme_id, source_strands FROM themes WHERE source_strands IS NOT NULL"):
        rids = set()
        try:
            ss = json.loads(src)
        except Exception:
            ss = {}
        if isinstance(ss, dict):
            for sids in ss.values():
                for sid in sids or []:
                    r = prod.execute(
                        "SELECT reality_id FROM strand_to_reality WHERE strand_id=?",
                        (sid,)).fetchone()
                    if r:
                        rids.add(r[0])
        if rids:
            theme2realities[tid] = rids

    cooc_rows = prod.execute(
        "SELECT id, session_id, topic_id, profile, reality_a, reality_b, created_at "
        "FROM cooccurrence_events").fetchall()
    old_ids = [r[0] for r in cooc_rows]
    migrated, dropped, inserted = 0, 0, 0
    for cid, sess, tpc, prof, a, b, created in cooc_rows:
        ra_set = theme2realities.get(a, set())
        rb_set = theme2realities.get(b, set())
        if not ra_set or not rb_set:
            dropped += 1
            continue
        for ra in ra_set:
            for rb in rb_set:
                lo, hi = (ra, rb) if ra < rb else (rb, ra)
                prod.execute(
                    "INSERT OR IGNORE INTO cooccurrence_events "
                    "(session_id, topic_id, profile, reality_a, reality_b, created_at) "
                    "VALUES (?,?,?,?,?,?)", (sess, tpc, prof, lo, hi, created))
                inserted += 1
        migrated += 1
    if old_ids:
        prod.execute(
            "DELETE FROM cooccurrence_events WHERE id IN (%s)" % ",".join("?" * len(old_ids)),
            old_ids)
    prod.commit()
    n_cooc = prod.execute("SELECT COUNT(*) FROM cooccurrence_events").fetchone()[0]
    print(f"[4] cooc 迁移: {migrated} 对处理（{dropped} 丢弃无映射）→ 现 {n_cooc} 行")

    # ── 5. 提问云形心 ──
    if not args.no_embed and state is not None:
        strand_q = {}
        for psid, sess, turns in prod.execute(
                "SELECT strand_id, session_id, turns FROM strand_summaries "
                "WHERE profile=? AND turns IS NOT NULL", (args.profile,)):
            try:
                t0 = json.loads(turns)[0]
            except Exception:
                t0 = None
            if not t0:
                continue
            rows = state.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content IS NOT NULL ORDER BY id LIMIT ?", (sess, t0)).fetchall()
            q = rows[-1][0].strip() if rows else ""
            if len(q) >= MIN_Q_LEN:
                strand_q[psid] = q
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ca.embedding import EmbeddingClient
        ec = EmbeddingClient()
        emb = {}
        for psid, q in strand_q.items():
            v = ec.embed(q[:500])
            if v:
                emb[psid] = v
        for rid, src in prod.execute("SELECT reality_id, source_strands FROM realities"):
            try:
                ss = json.loads(src)
            except Exception:
                ss = {}
            vecs = []
            if isinstance(ss, dict):
                for sids in ss.values():
                    for sid in sids or []:
                        if sid in emb:
                            vecs.append(emb[sid])
            if not vecs:
                continue
            dim = len(vecs[0])
            cent = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
            prod.execute(
                "UPDATE realities SET query_centroid_json=?, query_count=? WHERE reality_id=?",
                (json.dumps(cent, ensure_ascii=False), len(vecs), rid))
        prod.commit()
        n_cent = prod.execute(
            "SELECT COUNT(*) FROM realities WHERE query_centroid_json IS NOT NULL").fetchone()[0]
        print(f"[5] 提问云形心: {n_cent}/{n_realities} reality（embed {len(emb)} 提问）")
    else:
        print("[5] 跳过提问云形心（--no-embed 或 state.db 不可用）")

    # ── 6. 序列 ──
    max_rid = prod.execute("SELECT MAX(reality_id) FROM realities").fetchone()[0] or 0
    prod.execute("UPDATE sqlite_sequence SET seq=? WHERE name='realities'", (max_rid,))
    prod.commit()

    print(f"\n✅ 迁移完成: realities={n_realities}, s2r={n_s2r}, cooc={n_cooc}, "
          f"下一个 reality_id={max_rid + 1}")
    prod.close(); flash.close()
    if state:
        state.close()


if __name__ == "__main__":
    main()
