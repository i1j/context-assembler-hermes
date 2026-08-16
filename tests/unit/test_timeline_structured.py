"""第三轮 T5：R-2 生长序 + R-4 timeline 结构化（决策 42）。

覆盖：
  - append_timeline_hdl 输出 {hdl, ts} 结构化条目（R-4）
  - 存量 str 末条防重兼容（str / dict 末条比较）
  - update_reality timeline_entry 补 ts（含存量 str timeline 不崩）
  - merge group 乱序 strand → 输出按 turns 升序（R-2 生长序约束 1）
"""

import json
import time

from ca.reality import append_timeline_hdl, run_reality_merge
from ca.store import create_reality, load_all_realities, update_reality


class TestAppendTimelineHdl:
    def test_append_empty_returns_dict_entry(self):
        """append_timeline_hdl([], "h") → [{"hdl": "h", "ts": ...}]。"""
        result = append_timeline_hdl([], "新hdl")
        assert len(result) == 1
        assert result[0]["hdl"] == "新hdl"
        assert isinstance(result[0]["ts"], float)

    def test_legacy_str_append_dict(self):
        """存量 ["旧hdl"] + append → dict 追加且防重生效。"""
        tl = ["旧hdl"]
        result = append_timeline_hdl(tl, "新hdl")
        assert result == ["旧hdl", {"hdl": "新hdl", "ts": result[1]["ts"]}]
        # 防重：再 append 同 hdl 不追加（dict 末条比较取 hdl 字段）
        result2 = append_timeline_hdl(result, "新hdl")
        assert result2 == result

    def test_legacy_str_dedupe(self):
        """存量 str 末条相同 → 不追加。"""
        tl = ["已识别5.3 GB孤儿数据"]
        result = append_timeline_hdl(tl, "已识别5.3 GB孤儿数据")
        assert result == ["已识别5.3 GB孤儿数据"]

    def test_dict_last_dedupe(self):
        """dict 末条 hdl 相同 → 不追加。"""
        tl = [{"hdl": "旧hdl", "ts": 1.0}]
        result = append_timeline_hdl(tl, "旧hdl")
        assert result == tl

    def test_empty_hdl_no_append(self):
        assert append_timeline_hdl([], "") == []


class TestUpdateRealityTimelineTs:
    def test_timeline_entry_gets_ts(self, tmp_path):
        """update_reality(timeline_entry=...) → timeline 含 ts。"""
        db = tmp_path / "ca_topics.db"
        rid = create_reality(
            profile="tester", name="R1", hdl="H1",
            current_status={"current_state": [], "key_facts": [],
                            "goals": [], "context": []},
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s1", "overview": "o"},
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None, db_path=db)
        ok = update_reality(
            reality_id=rid,
            timeline_entry={"topic_id": 2, "turns": [5],
                            "session_id": "s1", "overview": "v2"},
            db_path=db)
        assert ok
        r = load_all_realities(db_path=db)[0]
        entries = [e for e in r["timeline"] if isinstance(e, dict)]
        assert len(entries) >= 2
        # update_reality 追加的条目补 ts（R-4）；create_reality 首条不在本任务范围
        assert isinstance(entries[-1].get("ts"), float)
        assert entries[-1]["turns"] == [5]

    def test_source_strand_list_merged(self, tmp_path):
        """update_reality 接受 list[source_strand]：s2r↔source_strands 一致性（决策 41 审计门）。"""
        db = tmp_path / "ca_topics.db"
        rid = create_reality(
            profile="tester", name="R1", hdl="H1",
            current_status={}, timeline_entry=None,
            source_strand={"session_id": "s1", "strand_id": 1},
            centroid_json=None, db_path=db)
        ok = update_reality(
            reality_id=rid, db_path=db,
            source_strand=[
                {"session_id": "s1", "strand_id": 2},
                {"session_id": "s1", "strand_id": 3},
                {"session_id": "s2", "strand_id": 7},
            ])
        assert ok
        r = load_all_realities(db_path=db)[0]
        assert r["source_strands"] == {"s1": [1, 2, 3], "s2": [7]}

    def test_legacy_str_timeline_no_crash(self, tmp_path):
        """存量 str timeline + update_reality → 不崩（seq 计算兼容）。"""
        import sqlite3
        db = tmp_path / "ca_topics.db"
        rid = create_reality(
            profile="tester", name="R1", hdl="H1",
            current_status={}, timeline_entry=None, source_strand=None,
            centroid_json=None, db_path=db)
        # 直接写入存量 str timeline（模拟 96% 字符串 timeline 的旧库）
        conn = sqlite3.connect(db)
        conn.execute("UPDATE realities SET timeline=? WHERE reality_id=?",
                     ('["旧hdl1", "旧hdl2"]', rid))
        conn.commit()
        conn.close()

        ok = update_reality(reality_id=rid, changes=["新变更"], db_path=db)
        assert ok
        r = load_all_realities(db_path=db)[0]
        dicts = [e for e in r["timeline"] if isinstance(e, dict)]
        assert len(dicts) == 1
        assert dicts[0]["changes"] == ["新变更"]
        assert dicts[0]["seq"] == 1  # str 条目不占 seq


class TestMergeGrowthOrder:
    def test_merge_group_sorted_by_start_turn(self, tmp_path):
        """merge group 乱序 strand → 输出按 turns 升序（R-2 约束 1）。"""
        db = tmp_path / "ca_topics.db"
        create_reality(
            profile="test", name="R1", hdl="H1",
            current_status={"current_state": ["状态"],
                            "key_facts": [], "goals": ["目标"],
                            "context": []},
            timeline_entry=None, source_strand=None,
            centroid_json=None, db_path=db)
        realities = load_all_realities(db_path=db)
        priority = [{"reality_id": 1, "name": "R1", "hdl": "H1"}]

        # 乱序传入：晚期 strand 在前，早期 strand 在后
        late = {
            "hdl": "LATE", "turns": [5, 7], "topic_id": 5,
            "session_id": "sess-A", "strand_id": 5,
            "ooda": {"现象与问题": ["晚期现象"],
                     "背景与约束": [], "决策与方案": [], "后续行动": []},
            "changes": ["晚期变更"],
            "query_text": "late",
        }
        early = {
            "hdl": "EARLY", "turns": [1, 2], "topic_id": 2,
            "session_id": "sess-A", "strand_id": 2,
            "ooda": {"现象与问题": ["早期现象"],
                     "背景与约束": [], "决策与方案": [], "后续行动": []},
            "changes": ["早期变更"],
            "query_text": "early",
        }

        def fake_llm(prompt):
            if "assignments" in prompt:
                return json.dumps({"assignments": {
                    "EARLY": {"action": "merge", "target": 0},
                    "LATE": {"action": "merge", "target": 0},
                }})
            return json.dumps({
                "merge": True,
                "name": "R1 更新",
                "hdl": "新锚点",
                "current_status": {
                    "current_state": ["已更新"], "key_facts": [],
                    "goals": [], "context": [],
                },
            })

        stats = run_reality_merge(
            [late, early], realities, embed_client=None,
            max_chars=4000, llm_call=fake_llm, db_path=db,
            profile="test", priority_realities=priority,
        )
        assert stats["merged"] == 2, stats
        r1 = next(r for r in load_all_realities(db_path=db)
                  if r["reality_id"] == 1)
        # timeline_entry 取排序后 group[0] = 早期 strand
        entry = next(e for e in r1["timeline"]
                     if isinstance(e, dict) and "turns" in e)
        assert entry["turns"] == [1, 2]
        assert entry["topic_id"] == 2
        # changes 按归并顺序（早期在前）
        changes_entry = next(e for e in r1["timeline"]
                             if isinstance(e, dict) and "changes" in e)
        assert changes_entry["changes"] == ["早期变更", "晚期变更"]
