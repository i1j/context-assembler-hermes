#!/usr/bin/env python3
"""flash reality 一步到位构建（任务书 40 pilot 阶段 2）。

输入：flash_pilot.db（strands 表，flash 生成，见 reprocess_old_sessions.py --llm flash）
流程：读全部 flash strands → flash 按 reality 模型（决策 37：
      name/hdl/current_status/timeline）stream 构建（首批 build + 后续批 refine，
      复用 refine_reality_cloud.py stream 模式思路）
输出：flash_pilot.db realities 表 + strand_to_reality 表
      并打印结构统计（数量/单 strand 比例/字段完整率）

用法：
    python3 scripts/flash_build_reality.py --db <flash_pilot.db> [--batch 15]

验证对照：scripts/exp_reality_winker_refined.json（金标准 25 reality，pilot 对照）。
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("FLASH_REALITY")

from ca.cloud_llm import load_api_key, call_cloud_llm
from ca.flash_reprocess import (
    OODA_LABELS,
    build_reality_create_prompt,
    build_reality_decision_prompt,
    build_reality_detail_prompt,
    init_flash_db,
    parse_reality_build_result,
    parse_reality_decision,
    parse_reality_detail,
    snapshot_injections,
    write_flash_reality,
    write_strand_to_reality,
)


def load_strands(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    # 全量读取（含 status='discarded'）：discarded 判定是每轮构建的**输出**，
    # 不是输入过滤条件。persist 会把上轮判 discarded 的 strand 写 status，
    # 若此处过滤 → 重跑时每轮少 N 条、丢弃判定不可逆（实测三轮 857→835）。
    rows = conn.execute(
        "SELECT strand_id, session_id, topic_id, hdl, ooda_json, query_text "
        "FROM strands ORDER BY strand_id"
    ).fetchall()
    conn.close()
    items = []
    for sid, sess, tid, hdl, ooda_raw, q in rows:
        try:
            ooda = json.loads(ooda_raw or "{}")
        except (json.JSONDecodeError, TypeError):
            ooda = {}
        items.append(
            {
                "id": sid,
                "session_id": sess,
                "topic_id": tid,
                "hdl": hdl or "",
                "ooda": ooda,
                "query_text": q or "",
            }
        )
    return items


def _dump_fail(tag, raw: Optional[str]) -> str:
    """保存解析失败现场（任务书纪律 5）。返回 dump 路径。"""
    dump_path = os.path.join(os.getcwd(), f"reality_fail_{tag}.txt")
    try:
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(raw or "(raw is None)")
    except OSError as e:
        logger.error("dump 失败: %s", e)
    return dump_path


def _top_candidates(batch: list, realities: list, embed_fn,
                    k: int = 30) -> list:
    """向量粗筛候选 ref（防全量 ref 撑爆 context，2026-08-05）。

    每 strand 语义文本（hdl+query_text+ooda）vs reality name+hdl 云 → top-k。
    返回按 reality_id 排序的候选；embed 失败 → 尾部 k 条裁剪降级（不中断流程）。
    """
    if len(realities) <= k:
        return realities
    if embed_fn is None:
        return realities[-k:]
    try:
        rvecs = embed_fn(
            [f"{r.get('name', '')} {r.get('hdl', '')}" for r in realities])
    except Exception:
        logger.warning("向量粗筛 reality embed 失败 → 尾部裁剪降级")
        return realities[-k:]
    if len(rvecs) != len(realities):
        logger.warning("向量粗筛 reality 向量数不匹配 → 尾部裁剪降级")
        return realities[-k:]
    cand_idx: set = set()
    for s in batch:
        ooda = s.get("ooda") or {}
        text = " ".join([
            str(s.get("hdl", "")),
            str(s.get("query_text", "")),
            " ".join(str(x) for g in OODA_LABELS for x in (ooda.get(g) or [])),
        ])[:800]
        try:
            svec = embed_fn([text])[0]
        except Exception:
            continue
        sims = []
        for i, rv in enumerate(rvecs):
            denom = math.sqrt(sum(a * a for a in rv)) * math.sqrt(
                sum(b * b for b in rv))
            if not denom:
                continue
            dot = sum(a * b for a, b in zip(svec, rv))
            sims.append((i, dot / denom))
        sims.sort(key=lambda x: -x[1])
        cand_idx |= {i for i, _ in sims[:k]}
    return [realities[i] for i in sorted(cand_idx)]


def _merge_timeline(r: dict, old: Optional[dict]) -> dict:
    """timeline 是代码维护职责（决策 37 §4.2）——忽略 flash 返回的 timeline。

    old 存在 → 恢复旧 timeline + hdl 更新时 append 旧 hdl（防重）；
    新建 → timeline 初始化为当前 hdl。
    """
    if old:
        old_tl = list(old.get("timeline") or [])
        old_hdl = old.get("hdl", "")
        new_hdl = r.get("hdl", "")
        if (old_hdl and new_hdl and old_hdl != new_hdl
                and old_hdl not in old_tl):
            old_tl.append(old_hdl)
        r["timeline"] = old_tl
    else:
        r["timeline"] = [r.get("hdl", "")] if r.get("hdl") else []
    return r


def stream_build(strands: list[dict], api_key: str,
                 batch_size: int = 15, embed_fn=None,
                 candidate_k: int = 30) -> dict:
    """分批流式构建：首批 build（create prompt），后续批 refine 两阶段。

    阶段 1 决策层（输出极小）：新 strand → 归属映射（s2r/discarded/affected，
    新建用 NEW* 临时编号）；
    阶段 2 内容层（≤10 条/批）：affected reality 完整详情生成。

    返回 {"realities": [...], "strand_to_reality": {int: int},
          "discarded_strand_ids": [...], "uncovered": [...]}。
    """
    batches = [strands[i:i + batch_size] for i in range(0, len(strands), batch_size)]
    all_s2r: dict = {}
    realities_all: dict = {}  # reality_id → reality（防御性累积）
    all_discarded: set = set()

    def _call(prompt: str, temperature: float) -> Optional[str]:
        # max_tokens=16384：容量翻倍（deepseek-chat 支持 16384/32768，
        # max_tokens 只是上限不影响计费；两阶段后输出量小，留足余量）
        return call_cloud_llm(prompt, api_key=api_key,
                              temperature=temperature, max_tokens=16384)

    for bi, batch in enumerate(batches):
        logger.info("=== 批次 %d/%d (%d strand) ===", bi + 1, len(batches), len(batch))
        t0 = time.monotonic()
        if not realities_all:
            # 第一批：create（全量新 reality 详情，输出 ~20 条不超限）
            prompt = build_reality_create_prompt(batch)
            raw = _call(prompt, 0.2)
            if raw is None:
                raise RuntimeError(f"批次 {bi + 1} 云端调用失败")
            result = parse_reality_build_result(raw)
            if not result:
                logger.warning("批次 %d 解析失败，temperature=0.1 重试", bi + 1)
                raw = _call(prompt, 0.1)
                result = parse_reality_build_result(raw)
            if not result:
                dp = _dump_fail(f"batch{bi + 1}", raw)
                raise RuntimeError(f"批次 {bi + 1} 解析失败（原始输出: {dp}）")
            for sid, rid in result["strand_to_reality"].items():
                all_s2r[sid] = rid
            all_discarded.update(result.get("discarded_strand_ids") or [])
            for r in result["realities"]:
                rid = r["reality_id"]
                realities_all[rid] = _merge_timeline(r, realities_all.get(rid))
        else:
            # refine：两阶段
            ref = list(realities_all.values())
            if len(ref) > candidate_k:
                ref = _top_candidates(batch, ref, embed_fn, k=candidate_k)
            # 阶段 1 决策层：只输出归属映射（输出极小，永不截断）
            prompt = build_reality_decision_prompt(batch, ref)
            raw = _call(prompt, 0.2)
            if raw is None:
                raise RuntimeError(f"批次 {bi + 1} 云端调用失败")
            dec = parse_reality_decision(raw)
            if not dec:
                logger.warning("批次 %d 决策层解析失败，temperature=0.1 重试", bi + 1)
                raw = _call(prompt, 0.1)
                dec = parse_reality_decision(raw)
            if not dec:
                dp = _dump_fail(f"batch{bi + 1}_decision", raw)
                raise RuntimeError(f"批次 {bi + 1} 决策层解析失败（原始输出: {dp}）")
            s2r_raw = dec["s2r"]
            all_discarded.update(dec["discarded"])
            # 代码兜底：affected ∪= s2r 涉及的已有 reality（漏报自动补齐）
            affected = set(dec["affected"])
            for sid, rid in s2r_raw.items():
                if isinstance(rid, int):
                    affected.add(rid)
            # NEW* → 正式 id（全局 max+1 递增分配，批间不冲突）
            new_map: dict = {}
            next_rid = (max(realities_all) + 1) if realities_all else 1
            for sid, rid in list(s2r_raw.items()):
                if isinstance(rid, str):
                    if rid not in new_map:
                        new_map[rid] = next_rid
                        next_rid += 1
                    s2r_raw[sid] = new_map[rid]
            for sid, rid in s2r_raw.items():
                all_s2r[sid] = rid
            # 阶段 2 内容层：affected 分批（≤10）生成详情
            detail_items = []
            for a in sorted(affected, key=lambda x: (not isinstance(x, str), str(x))):
                rid = new_map.get(a, a) if isinstance(a, str) else a
                members = sorted(sid for sid, r in s2r_raw.items() if r == rid)
                detail_items.append({"rid": rid, "old": realities_all.get(rid),
                                     "strand_ids": members})
            for ci in range(0, len(detail_items), 10):
                chunk = detail_items[ci:ci + 10]
                prompt = build_reality_detail_prompt(chunk, batch)
                raw = _call(prompt, 0.2)
                if raw is None:
                    raise RuntimeError(f"批次 {bi + 1} 内容层调用失败")
                details = parse_reality_detail(raw)
                if not details:
                    logger.warning("批次 %d 内容层解析失败，temperature=0.1 重试", bi + 1)
                    raw = _call(prompt, 0.1)
                    details = parse_reality_detail(raw)
                if not details:
                    dp = _dump_fail(f"batch{bi + 1}_detail{ci // 10 + 1}", raw)
                    raise RuntimeError(
                        f"批次 {bi + 1} 内容层解析失败（原始输出: {dp}）")
                got = set()
                for d in details:
                    rid = d["reality_id"]
                    got.add(rid)
                    realities_all[rid] = _merge_timeline(
                        d, realities_all.get(rid))
                missing = {it["rid"] for it in chunk} - got
                if missing:
                    logger.warning("内容层漏生成 reality: %s（保留旧值/空详情）",
                                   sorted(missing))
        # ⚠️ member_strands 权威化：以 strand_to_reality 反推覆盖（flash 返回
        # 的 member 缺 strand 但映射完整——传给下批的成员必须完整）
        member_by_reality: dict = {}
        for sid, rid in all_s2r.items():
            member_by_reality.setdefault(rid, []).append(sid)
        for rid in realities_all:
            if rid in member_by_reality:
                realities_all[rid]["member_strands"] = sorted(member_by_reality[rid])
        dt = time.monotonic() - t0
        realities_ref = list(realities_all.values())
        batch_ids = {s["id"] for s in batch}
        miss = batch_ids - set(all_s2r.keys())
        logger.info(
            "批次 %d 累积 %d reality, 覆盖 %d/%d strand (%.1fs)%s",
            bi + 1, len(realities_ref), len(batch_ids - miss), len(batch_ids),
            dt, f", 缺漏: {sorted(miss)}" if miss else "")
    # 未覆盖且未被判丢弃的 strand = 真缺漏（flash 漏输出），如实上报
    uncovered = (
        {s["id"] for s in strands}
        - set(all_s2r.keys()) - all_discarded
    )
    return {
        "realities": list(realities_all.values()),
        "strand_to_reality": all_s2r,
        "discarded_strand_ids": sorted(all_discarded),
        "uncovered": sorted(uncovered),
    }


def persist(conn, result: dict, profile: str) -> None:
    """写 realities 表 + strand_to_reality 表。

    ⚠️ 权威成员列表 = strand_to_reality 反推（flash 返回的 member_strands
    与 strand_to_reality 不一致时以映射为准，防止丢 strand——smoke3 实测
    member 缺 11-15 但映射完整）。
    ⚠️ 幂等：先清空旧数据（pilot 是整库重跑产物，重复执行应覆盖不累积）。
    ⚠️ flash 判 discarded 的 strand → status='discarded'（宁缺勿错，如实记录）。
    """
    conn.execute("DELETE FROM realities")
    conn.execute("DELETE FROM strand_to_reality")
    conn.execute("DELETE FROM inject_log")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('realities','strand_to_reality','inject_log')")
    conn.commit()
    # discarded 标记（仅覆盖已完成 strand；已映射的跳过）
    for sid in result.get("discarded_strand_ids") or []:
        conn.execute(
            "UPDATE strands SET status='discarded' "
            "WHERE strand_id=? AND status='completed'", (sid,))
    conn.commit()
    # 反推：reality_id → 成员 strand 列表（按 strand_id 升序，稳定输出）
    member_by_reality: dict = {}
    for strand_id, reality_id in result["strand_to_reality"].items():
        member_by_reality.setdefault(reality_id, []).append(strand_id)
    for k in member_by_reality:
        member_by_reality[k].sort()

    id_map = {}  # flash 返回的 reality_id → 本库自增 id
    for r in result["realities"]:
        rid = r.get("reality_id")
        members = member_by_reality.get(rid) or r.get("member_strands") or []
        new_id = write_flash_reality(
            conn,
            name=r.get("name", ""),
            hdl=r.get("hdl", ""),
            current_status=r.get("current_status") or {},
            timeline=r.get("timeline") or [],
            source_strands=members,
            profile=profile,
        )
        id_map[rid] = new_id
    for strand_id, reality_id in result["strand_to_reality"].items():
        target = id_map.get(reality_id, reality_id)
        write_strand_to_reality(conn, strand_id, target)


def report(conn, strands: list[dict]) -> None:
    """结构统计（对照金标准 exp_reality_winker_refined.json 25 reality）。"""
    realities = conn.execute(
        "SELECT reality_id, name, hdl, current_status, timeline, source_strands "
        "FROM realities ORDER BY reality_id"
    ).fetchall()
    n = len(realities)
    print(f"\n=== reality 结构统计（flash pilot）===")
    print(f"reality 数: {n}")
    sizes = []
    field_ok = 0
    for rid, name, hdl, cs_raw, tl_raw, ss_raw in realities:
        try:
            cs = json.loads(cs_raw or "{}")
            tl = json.loads(tl_raw or "[]")
            ss = json.loads(ss_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            cs, tl, ss = {}, [], []
        sizes.append(len(ss))
        four = all(cs.get(k) for k in ("current_state", "key_facts", "goals"))
        if name and hdl and four and tl:
            field_ok += 1
    print(f"大小分布: 单strand={sum(1 for s in sizes if s <= 1)}/{n} "
          f"({100 * sum(1 for s in sizes if s <= 1) / n:.0f}%), "
          f"max={max(sizes) if sizes else 0}")
    print(f"字段完整（name+hdl+current_status三段+timeline）: {field_ok}/{n} "
          f"({100 * field_ok / n:.0f}%)")
    golden = BASE / "exp_reality_winker_refined.json"
    if golden.exists():
        g = json.load(open(golden))
        gn = len(g.get("realities", []))
        print(f"金标准对照: 现 25 reality（flash={n}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="flash_pilot.db 路径")
    ap.add_argument("--batch", type=int, default=15, help="stream 批大小（strand 数）")
    ap.add_argument("--profile", default="winker")
    ap.add_argument("--inject", action="store_true",
                    help="快照注入结果（决策 39：块首提问 → reality 提问云 top-3 → inject_log）")
    ap.add_argument("--inject-only", action="store_true",
                    help="只做注入快照（复用 DB 现有 reality，不重跑 stream_build——"
                         "任务书命令 3 的省时等价物，避免 reality 结果随机漂移）")
    ap.add_argument("--inject-k", type=int, default=3)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: {db_path} 不存在（先跑 reprocess --llm flash）")
        sys.exit(2)

    api_key = load_api_key()
    strands = load_strands(db_path)
    print(f"读入 {len(strands)} 条 flash strand")
    if not strands:
        print("无 strand，退出")
        sys.exit(0)

    if args.inject_only:
        # 只做注入快照（复用 DB 现有 reality/s2r，不重跑 stream_build）
        conn = init_flash_db(db_path)
        s2r = dict(conn.execute(
            "SELECT strand_id, reality_id FROM strand_to_reality").fetchall())
        print(f"注入快照模式（不重跑 reality 构建）：strand_to_reality {len(s2r)} 条")
        if not s2r:
            print("无 strand_to_reality（需先跑 reality 构建）")
            sys.exit(2)
        from ca.embedding import EmbeddingClient

        ec = EmbeddingClient()
        try:
            stats = snapshot_injections(conn, strands, [], s2r, ec.embed,
                                        k=args.inject_k)
        finally:
            ec.close()
        conn.close()
        print(f"注入快照（全库视角，含自块非留块——命中率评估见验证阶段）: {stats}")
        sys.exit(0)

    # EmbeddingClient：refine 向量粗筛候选 ref（决策层输入裁剪，防 context 超限）
    # + --inject 注入快照复用。embed 失败由 _top_candidates 内部降级（不中断）。
    from ca.embedding import EmbeddingClient

    ec = EmbeddingClient()
    try:
        # embed_batch：批量接口（embed 是单文本接口，list 会被 str() 编码）
        result = stream_build(strands, api_key, batch_size=args.batch,
                              embed_fn=ec.embed_batch)
    finally:
        ec.close()

    # 全覆盖校验（自包含评估：检查缺漏，不静默）
    real_ids = {s["id"] for s in strands}
    missing = real_ids - set(result["strand_to_reality"].keys())
    if missing:
        print(f"⚠️ strand_to_reality 缺漏 {len(missing)}: {sorted(missing)}")
    discarded = result.get("discarded_strand_ids") or []
    if discarded:
        print(f"ℹ️ flash 判 discarded（非工作线）{len(discarded)}: {discarded}")
    uncovered = result.get("uncovered") or []
    if uncovered:
        print(f"⚠️ 真缺漏（未覆盖且未判丢弃）{len(uncovered)}: {uncovered}")

    conn = init_flash_db(db_path)
    persist(conn, result, profile=args.profile)
    if args.inject:
        from ca.embedding import EmbeddingClient

        ec = EmbeddingClient()
        try:
            stats = snapshot_injections(
                conn, strands, result["realities"],
                result["strand_to_reality"], ec.embed, k=args.inject_k)
        finally:
            ec.close()
        print(f"注入快照（全库视角，含自块非留块——命中率评估见验证阶段）: {stats}")
    conn.close()
    print(f"已写入 {db_path}（realities + strand_to_reality）")

    conn2 = init_flash_db(db_path)
    report(conn2, strands)
    conn2.close()


if __name__ == "__main__":
    main()
