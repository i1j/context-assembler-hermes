#!/usr/bin/env python3
"""flash pilot 验证脚本（任务书 40 §二 验证 ①②③④）。

验证项：
  ① strand 质量：hdl ≤30 字规范率 / ooda 四组完整率（对照 4B 现数据 99%/91%）
  ② reality 结构：数量/大小分布/字段完整（对照金标准 exp_reality_winker_refined.json 25 reality）
  ③ 归属合理性抽样：输出采样供人工评估（pilot 验证抽样 ≥80%）
  ④ 成本：单块 flash 耗时 × 505 → 全量可行性预估

用法：
    python3 scripts/flash_pilot_verify.py --db <flash_pilot.db>
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

OODA_LABELS = ["现象与问题", "背景与约束", "决策与方案", "后续行动"]
HDL_MAX_LEN = 30
GOLDEN = BASE / "exp_reality_winker_refined.json"

# 4B 现数据对照基线（任务书 §二，2026-08-03 审计）
BASELINE_4B = {"hdl_ok": 0.99, "ooda_full": 0.91}
# 通过门槛（任务书 §六）——2026-08-05 用户决策：ooda 四组完整率接受 flash
# 如实报告空组（宁缺勿错优先），32% 为行为基线，不再作为硬门槛；
# hdl 规范率 99% 仍为硬门槛。
GATE = {"ooda_full": 0.0, "hdl_ok": 0.99, "sample_ok": 0.80}


def verify_strands(conn) -> dict:
    rows = conn.execute(
        "SELECT hdl, ooda_json, status FROM strands WHERE status IN "
        "('completed','discarded')"
    ).fetchall()
    total = len(rows)
    completed = [r for r in rows if r[2] == "completed"]
    n = len(completed)
    hdl_ok = 0
    ooda_full = 0
    long_hdls = []
    for hdl, ooda_raw, _st in completed:
        if hdl and len(hdl) <= HDL_MAX_LEN:
            hdl_ok += 1
        else:
            long_hdls.append((hdl, len(hdl or "")))
        try:
            ooda = json.loads(ooda_raw or "{}")
        except (json.JSONDecodeError, TypeError):
            ooda = {}
        if all(ooda.get(g) for g in OODA_LABELS):
            ooda_full += 1
    discarded = total - n
    return {
        "n": n,
        "discarded": discarded,
        "hdl_ok": hdl_ok / n if n else 0.0,
        "ooda_full": ooda_full / n if n else 0.0,
        "long_hdls": long_hdls,
    }


def verify_realities(conn) -> dict:
    rows = conn.execute(
        "SELECT name, hdl, current_status, timeline, source_strands "
        "FROM realities ORDER BY reality_id"
    ).fetchall()
    n = len(rows)
    sizes = []
    field_ok = 0
    no_name = 0
    no_hdl = 0
    no_tl = 0
    empty_status = 0
    for name, hdl, cs_raw, tl_raw, ss_raw in rows:
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
        if not name:
            no_name += 1
        if not hdl:
            no_hdl += 1
        if not tl:
            no_tl += 1
        if not four:
            empty_status += 1
    single = sum(1 for s in sizes if s <= 1)
    return {
        "n": n,
        "single_ratio": single / n if n else 0.0,
        "max_size": max(sizes) if sizes else 0,
        "avg_size": sum(sizes) / len(sizes) if sizes else 0.0,
        "field_ok": field_ok,
        "no_name": no_name,
        "no_hdl": no_hdl,
        "no_tl": no_tl,
        "status_incomplete": empty_status,
        "size_dist": {f"size={s}": sizes.count(s) for s in sorted(set(sizes))},
    }


def verify_inject(conn) -> dict:
    rows = conn.execute(
        "SELECT COUNT(*), SUM(empty) FROM inject_log"
    ).fetchone()
    total, empty = rows
    return {
        "blocks": total or 0,
        "empty": empty or 0,
        "matched": (total or 0) - (empty or 0),
    }


def sample_for_review(conn, k: int = 8) -> list[dict]:
    """归属合理性抽样：reality 名称 + 成员 hdl，供人工评估（≥80% 通过）。

    ⚠️ 随机抽样（seed 固定可复现）：原实现 rows[:k] 只取前 N 个 reality
    （第一批 create 产物），不含 refine 阶段大 reality，代表性不足
    （2026-08-05 实测需重写评估脚本补 38 大成员 + 10 随机才覆盖全库）。
    """
    import random

    random.seed(2026)
    strands = dict(
        conn.execute("SELECT strand_id, hdl FROM strands").fetchall())
    rows = conn.execute(
        "SELECT reality_id, name, source_strands FROM realities ORDER BY reality_id"
    ).fetchall()
    picked = random.sample(rows, min(k, len(rows)))
    samples = []
    for rid, name, ss_raw in picked:
        try:
            ss = json.loads(ss_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            ss = []
        members = [strands.get(int(s), f"?{s}") for s in ss]
        samples.append({"reality_id": rid, "name": name, "members": members})
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="flash_pilot.db 路径")
    ap.add_argument("--sample", type=int, default=8,
                    help="归属抽样条数（人工评估）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: {db} 不存在")
        sys.exit(2)
    conn = sqlite3.connect(str(db))

    print("=" * 60)
    print("① strand 质量（对照 4B 现数据 hdl≥99% / ooda四组≥91%）")
    print("=" * 60)
    s = verify_strands(conn)
    print(f"  strand 总数: {s['n']}（另 discarded {s['discarded']}）")
    print(f"  hdl ≤30 字规范率: {s['hdl_ok']:.1%} (4B: {BASELINE_4B['hdl_ok']:.0%}, "
          f"门槛 {GATE['hdl_ok']:.0%})")
    print(f"  ooda 四组完整率: {s['ooda_full']:.1%} (4B: {BASELINE_4B['ooda_full']:.0%}, "
          f"门槛 {GATE['ooda_full']:.0%})")
    for hdl, ln in s["long_hdls"][:10]:
        print(f"    ⚠️ 超长 hdl ({ln}字): {str(hdl)[:40]}")

    print()
    print("=" * 60)
    print("② reality 结构（对照金标准 exp_reality_winker_refined.json 25 reality）")
    print("=" * 60)
    r = verify_realities(conn)
    print(f"  reality 数: {r['n']}（金标准 25）")
    print(f"  大小分布: 单strand={r['single_ratio']:.0%}, max={r['max_size']}, "
          f"avg={r['avg_size']:.1f}")
    print(f"  字段完整（name+hdl+status+timeline）: {r['field_ok']}/{r['n']}")
    print(f"    缺 name: {r['no_name']}, 缺 hdl: {r['no_hdl']}, "
          f"缺 timeline: {r['no_tl']}, status 不完整: {r['status_incomplete']}")
    # 措施 5（2026-08-05）：timeline 深度标注——一次性构建（pilot）深度=1
    # 为预期；真实增量 merge 链路应 >1（hdl 演变 append）。深度=1 不判缺陷，
    # 仅提示验证范围。
    tl_depth = conn.execute(
        "SELECT timeline FROM realities"
    ).fetchall()
    _depths = [len(json.loads(t or "[]")) for (t,) in tl_depth]
    _max_depth = max(_depths) if _depths else 0
    if _max_depth <= 1:
        print(f"  ⚠️ timeline 深度={_max_depth}（pilot 一次性构建，未经历增量 "
              f"merge——真实链路需验证 hdl 演变 append）")
    if golden := json.load(open(GOLDEN)):
        gn = len(golden.get("realities", []))
        gs = len(golden.get("strand_to_reality", {}))
        print(f"  金标准: {gn} reality / {gs} strand 映射")

    print()
    print("=" * 60)
    print("③ 归属合理性抽样（人工评估，≥80% 通过为门槛）")
    print("=" * 60)
    for i, smp in enumerate(sample_for_review(conn, args.sample), 1):
        print(f"  [{i}] reality@{smp['reality_id']}: {smp['name']}")
        for m in smp["members"]:
            print(f"        - {str(m)[:60]}")
    print(f"  → 抽样 {args.sample} 条，请人工评估归属合理性（≥{GATE['sample_ok']:.0%}）")

    print()
    print("=" * 60)
    print("注入快照（决策 39，全库视角日志）")
    print("=" * 60)
    inj = verify_inject(conn)
    if inj["blocks"]:
        print(f"  块数: {inj['blocks']}, 命中: {inj['matched']}, "
              f"空注入: {inj['empty']}")
    else:
        print("  未跑 --inject（无 inject_log）")

    conn.close()


if __name__ == "__main__":
    main()
