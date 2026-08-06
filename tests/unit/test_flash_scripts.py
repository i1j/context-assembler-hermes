"""scripts 层函数单测（2026-08-05 收尾修复）。

覆盖:
  - load_strands 全量读取（含 status='discarded'——重跑时丢弃判定不可逆坑）
  - sample_for_review 随机抽样（原 rows[:k] 只取前 N 个 reality，代表性不足）
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import flash_build_reality as fbr
import flash_pilot_verify as fpv


def _mk_db() -> tuple[sqlite3.Connection, Path]:
    """建临时 DB：strands 表（completed + discarded）+ realities 表。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Path(tmp.name)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE strands (
        strand_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        topic_id INTEGER NOT NULL,
        hdl TEXT,
        turns TEXT NOT NULL DEFAULT '[]',
        ooda_json TEXT DEFAULT '{}',
        changes_json TEXT DEFAULT '[]',
        key_facts_json TEXT DEFAULT '[]',
        query_text TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'completed',
        created_at REAL)""")
    conn.execute("""CREATE TABLE realities (
        reality_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, hdl TEXT, current_status TEXT DEFAULT '{}',
        timeline TEXT DEFAULT '[]', source_strands TEXT DEFAULT '[]',
        profile TEXT NOT NULL DEFAULT '', created_at REAL, updated_at REAL)""")
    for i in range(1, 11):
        conn.execute(
            "INSERT INTO strands (session_id, topic_id, hdl, ooda_json, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"s{i}", 1, f"hdl{i}", '{"现象与问题": ["x"]}',
             "completed" if i % 2 else "discarded"))
    for i in range(1, 21):
        conn.execute(
            "INSERT INTO realities (name, source_strands) VALUES (?, ?)",
            (f"reality{i}", json.dumps([i])))
    conn.commit()
    return conn, db


class TestLoadStrands(unittest.TestCase):
    def test_loads_all_status(self):
        """全量读取：discarded 也是输入（丢弃判定是输出不是过滤条件）。"""
        conn, db = _mk_db()
        conn.close()
        items = fbr.load_strands(db)
        db.unlink(missing_ok=True)
        self.assertEqual(len(items), 10)  # 5 completed + 5 discarded 全读

    def test_fields_present(self):
        conn, db = _mk_db()
        conn.close()
        items = fbr.load_strands(db)
        db.unlink(missing_ok=True)
        it = items[0]
        self.assertIn("id", it)
        self.assertIn("session_id", it)
        self.assertIn("topic_id", it)
        self.assertIn("hdl", it)
        self.assertEqual(it["ooda"], {"现象与问题": ["x"]})
        self.assertIn("query_text", it)


class TestSampleForReview(unittest.TestCase):
    def test_random_sample_not_first_k(self):
        """随机抽样：k=8 时不应只取前 8 条（原 rows[:k] 缺陷）。"""
        conn, db = _mk_db()
        samples = fpv.sample_for_review(conn, k=8)
        conn.close()
        db.unlink(missing_ok=True)
        self.assertEqual(len(samples), 8)
        ids = {s["reality_id"] for s in samples}
        self.assertNotEqual(ids, {1, 2, 3, 4, 5, 6, 7, 8},
                            "抽样不应固定为前 8 条（代表性不足）")

    def test_seed_stable(self):
        """seed=2026 固定 → 两次调用结果一致（可复现）。"""
        conn, db = _mk_db()
        a = fpv.sample_for_review(conn, k=6)
        b = fpv.sample_for_review(conn, k=6)
        conn.close()
        db.unlink(missing_ok=True)
        self.assertEqual(
            [s["reality_id"] for s in a], [s["reality_id"] for s in b])


if __name__ == "__main__":
    unittest.main()
