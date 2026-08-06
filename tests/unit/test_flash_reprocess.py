"""flash 全链路重跑 pilot 核心逻辑单测（任务书 40）。

覆盖:
  - flash strand prompt: hdl≤30 规范 / OODA 四组 / 宁多勿少 / 归属判断
  - parse_flash_strands: 围栏剥离 / S7 前缀 / 畸形元素跳过
  - reality 一步到位 prompt: name/hdl/current_status/timeline 四段（决策 37）
  - parse_reality_build_result: 宽容解析 / strand_to_reality str→int 归一
  - init_flash_db: flash_pilot.db schema（strands/realities/strand_to_reality/inject_log）
"""

import json
import sqlite3
import sys
import unittest
from pathlib import Path

# ca/ 可导入
_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.flash_reprocess as fr


def _turns_data() -> list[dict]:
    return [
        {
            "turn": 7,
            "hdl": "连接池优化",
            "changes": ["发现连接池耗尽", "扩容连接池到 200"],
            "tags": {"发现连接池耗尽": "现象与问题", "扩容连接池到 200": "已实施"},
            "ooda_tags": {
                "发现连接池耗尽": "现象与问题",
                "扩容连接池到 200": "决策与方案",
            },
            "todos": [],
            "consensus": ["确定上限 200"],
            "key_facts_supp": [],
            "new_materials": [],
        }
    ]


class TestFlashStrandPrompt(unittest.TestCase):
    def test_prompt_contains_ooda_labels_and_rules(self):
        p = fr.build_flash_strand_prompt(_turns_data(), query_text="连接池怎么调？")
        self.assertIn("现象与问题", p)
        self.assertIn("背景与约束", p)
        self.assertIn("决策与方案", p)
        self.assertIn("后续行动", p)
        # hdl 命名规范（≤30 字，禁代码符号）
        self.assertIn("30", p)
        # 宁多勿少
        self.assertIn("宁多勿少", p)
        # 块首提问注入
        self.assertIn("连接池怎么调", p)
        # 轮次内容
        self.assertIn("扩容连接池到 200", p)

    def test_query_text_omitted_when_empty(self):
        p = fr.build_flash_strand_prompt(_turns_data(), query_text="")
        self.assertNotIn("【块首提问】", p)


class TestParseFlashStrands(unittest.TestCase):
    def test_normal(self):
        text = json.dumps(
            {
                "strands": [
                    {
                        "hdl": "连接池与超时配置优化",
                        "turns": [7, 8],
                        "ooda": {
                            "现象与问题": ["连接池耗尽"],
                            "决策与方案": ["扩容到 200"],
                        },
                    }
                ],
                "key_facts": ["上限 200"],
                "consumable": True,
            },
            ensure_ascii=False,
        )
        r = fr.parse_flash_strands(text)
        self.assertEqual(len(r["strands"]), 1)
        self.assertEqual(r["strands"][0]["hdl"], "连接池与超时配置优化")
        self.assertEqual(r["strands"][0]["turns"], [7, 8])
        self.assertEqual(r["key_facts"], ["上限 200"])
        self.assertTrue(r["consumable"])

    def test_fenced_json(self):
        text = "```json\n" + json.dumps(
            {"strands": [{"hdl": "A", "turns": [1], "ooda": {}}]}
        ) + "\n```"
        r = fr.parse_flash_strands(text)
        self.assertEqual(len(r["strands"]), 1)

    def test_malformed_element_skipped(self):
        text = json.dumps(
            {
                "strands": [
                    "垃圾元素",
                    {"hdl": "好strand", "turns": [3], "ooda": {"现象与问题": ["x"]}},
                ]
            }
        )
        r = fr.parse_flash_strands(text)
        self.assertEqual(len(r["strands"]), 1)
        self.assertEqual(r["strands"][0]["hdl"], "好strand")

    def test_turns_string_variants(self):
        # 云端可能返回字符串 turns
        text = json.dumps({"strands": [{"hdl": "A", "turns": "[7, 8]", "ooda": {}}]})
        r = fr.parse_flash_strands(text)
        self.assertEqual(r["strands"][0]["turns"], [7, 8])

    def test_invalid_returns_none(self):
        self.assertIsNone(fr.parse_flash_strands("not json at all"))
        self.assertIsNone(fr.parse_flash_strands(""))
        self.assertIsNone(fr.parse_flash_strands(None))

    def test_ooda_group_order_normalized(self):
        """四组顺序固定（现象/背景/决策/后续），缺失组不补空。"""
        text = json.dumps(
            {"strands": [{"hdl": "A", "turns": [1], "ooda": {"后续行动": ["做X"]}}]}
        )
        r = fr.parse_flash_strands(text)
        ooda = r["strands"][0]["ooda"]
        self.assertEqual(list(ooda.keys()), ["后续行动"])
        self.assertNotIn("现象与问题", ooda)


class TestRealityPrompts(unittest.TestCase):
    def test_create_prompt_has_reality_model_fields(self):
        strands = [{"id": 1, "hdl": "A", "ooda": {"现象与问题": ["x"]}}]
        p = fr.build_reality_create_prompt(strands)
        self.assertIn("name", p)
        self.assertIn("hdl", p)
        self.assertIn("current_status", p)
        self.assertIn("timeline", p)
        self.assertIn("current_state", p)
        self.assertIn("goals", p)
        # 决策 37 模型：name 固定 / hdl 可改 / 宁分不并
        self.assertIn("宁分不并", p)

    def test_refine_prompt_references_previous(self):
        strands = [{"id": 2, "hdl": "B", "ooda": {}}]
        realities = [{"reality_id": 1, "name": "R1", "hdl": "状态1"}]
        p = fr.build_reality_refine_prompt(strands, realities)
        self.assertIn("R1", p)
        self.assertIn("strand_to_reality", p)

    def test_parse_reality_build_result(self):
        text = json.dumps(
            {
                "realities": [
                    {
                        "reality_id": "R1",  # 云端 R 前缀（refine 批次实测格式）
                        "name": "模型管理",
                        "hdl": "已明确LoRA定位",
                        "current_status": {
                            "current_state": ["等待路径"],
                            "key_facts": ["2026-08-01: 确认"],
                            "goals": ["获取文件"],
                            "context": [],
                        },
                        "timeline": ["初始状态"],
                        "member_strands": ["S1", "SS2"],
                    }
                ],
                "strand_to_reality": {"1": "R1", "S2": "R1", "3": "R2"},
                "stats": {},
            },
            ensure_ascii=False,
        )
        r = fr.parse_reality_build_result(text)
        self.assertEqual(len(r["realities"]), 1)
        self.assertEqual(r["realities"][0]["reality_id"], 1)
        self.assertEqual(r["realities"][0]["name"], "模型管理")
        self.assertEqual(r["realities"][0]["member_strands"], [1, 2])
        self.assertEqual(r["realities"][0]["timeline"], ["初始状态"])
        # str→int 归一（json.load 后 key 全 str；S/R 前缀容忍）
        self.assertEqual(r["strand_to_reality"], {1: 1, 2: 1, 3: 2})
        self.assertIn("strand_to_reality", r)

    def test_parse_reality_build_invalid(self):
        self.assertIsNone(fr.parse_reality_build_result("oops"))
        self.assertIsNone(fr.parse_reality_build_result(None))


class TestFlashDb(unittest.TestCase):
    def test_init_schema_and_roundtrip(self):
        db = Path("/tmp/__flash_pilot_test.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            # schema 表
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for t in ("strands", "realities", "strand_to_reality", "inject_log"):
                self.assertIn(t, tables)

            # strands 写入
            sid = fr.write_flash_strand(
                conn,
                session_id="sess1",
                topic_id=1,
                hdl="测试strand",
                turns=[7, 8],
                ooda={"现象与问题": ["x"]},
                query_text="测试提问",
            )
            self.assertIsNotNone(sid)
            row = conn.execute(
                "SELECT hdl, query_text FROM strands WHERE strand_id=?",
                (sid,),
            ).fetchone()
            self.assertEqual(row[0], "测试strand")
            self.assertEqual(row[1], "测试提问")

            # reality + 映射写入
            rid = fr.write_flash_reality(
                conn,
                name="测试reality",
                hdl="状态",
                current_status={
                    "current_state": ["a"],
                    "key_facts": [],
                    "goals": [],
                    "context": [],
                },
                timeline=["初始"],
                source_strands=[sid],
            )
            self.assertIsNotNone(rid)
            fr.write_strand_to_reality(conn, sid, rid)

            # 注入日志写入
            fr.write_inject_log(
                conn,
                session_id="sess1",
                topic_id=1,
                query="测试提问",
                picked=[rid],
                empty=False,
            )
            log = conn.execute("SELECT COUNT(*) FROM inject_log").fetchone()[0]
            self.assertEqual(log, 1)
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_strand_query_text_fallback(self):
        """query_text 缺省 → 空串（非 None，schema NOT NULL 安全）。"""
        db = Path("/tmp/__flash_pilot_test2.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            sid = fr.write_flash_strand(conn, session_id="s", topic_id=1, hdl="A")
            row = conn.execute(
                "SELECT query_text FROM strands WHERE strand_id=?", (sid,)
            ).fetchone()
            self.assertEqual(row[0], "")
            conn.close()
        finally:
            db.unlink(missing_ok=True)


class TestInjectSnapshot(unittest.TestCase):
    def test_build_question_cloud_and_match(self):
        """提问云 = 成员 strand query_text embed 均值；top-3 命中。"""
        # mock embed：按内容哈希的确定性伪向量（成员同云必命中）
        def fake_embed(text):
            seed = sum(ord(c) for c in (text or "")) + 1  # +1 防 seed=0
            v = [float((seed * (i + 3)) % 97) / 97.0 + 0.01 for i in range(8)]
            return v

        strands = [
            {"id": 1, "query_text": "comfyui 模型下载"},
            {"id": 2, "query_text": "comfyui 模型下载"},
            {"id": 3, "query_text": "comfyui 工作流配置"},
        ]
        cloud = fr.build_question_cloud(strands, fake_embed)
        self.assertEqual(len(cloud), 8)
        # 同块同提问去重：id 1/2 同提问只计一次 → 云 = (q1 + q3)/2
        expected_mean = (
            fake_embed("comfyui 模型下载")[0] + fake_embed("comfyui 工作流配置")[0]
        ) / 2.0
        self.assertAlmostEqual(cloud[0], expected_mean, places=6)

        top = fr.match_question_clouds(
            query="comfyui 模型下载问题", q_emb=fake_embed("comfyui 模型下载问题"),
            clouds={1: cloud}, k=3)
        self.assertEqual(top[0][0], 1)
        self.assertGreater(top[0][1], 0.0)

    def test_empty_query_cloud_no_match(self):
        top = fr.match_question_clouds("", [], {}, k=3)
        self.assertEqual(top, [])


class TestRealitySectionLimits(unittest.TestCase):
    """flash reality current_status 超限兜底（对齐 4B 路径 reality.py 守卫）。"""

    def test_enforce_limits_on_write(self):
        """写入前强制截断超限段（key_facts > 5 → 锚点优先保留 5 条）。"""
        db = Path("/tmp/__flash_limits_test.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            cs = {
                "current_state": ["状态1", "状态2", "状态3", "状态4", "状态5", "状态6"],
                "key_facts": [
                    "2026-07-01: 事实A", "2026-07-02: 事实B", "2026-07-03: 事实C",
                    "2026-07-04: 事实D", "2026-07-05: 事实E", "2026-07-06: 事实F",
                    "纯描述无锚点内容",
                ],
                "goals": ["目标1", "目标2"],
                "context": [],
            }
            sid = fr.write_flash_strand(conn, session_id="s", topic_id=1, hdl="A")
            rid = fr.write_flash_reality(
                conn, name="测试", hdl="状态", current_status=cs,
                timeline=["初始"], source_strands=[sid])
            row = conn.execute(
                "SELECT current_status FROM realities WHERE reality_id=?", (rid,)
            ).fetchone()
            stored = json.loads(row[0])
            self.assertLessEqual(len(stored.get("key_facts") or []), 5)
            self.assertLessEqual(len(stored.get("current_state") or []), 5)
            # 锚点优先：带日期的条目被保留
            self.assertTrue(
                any("2026-07-0" in kf for kf in (stored.get("key_facts") or []))
            )
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_within_limit_unchanged(self):
        """未超限不截断。"""
        db = Path("/tmp/__flash_limits_test2.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            cs = {
                "current_state": ["状态1", "状态2"],
                "key_facts": ["2026-07-01: 事实A"],
                "goals": ["目标1"],
                "context": ["/path/x"],
            }
            sid = fr.write_flash_strand(conn, session_id="s", topic_id=1, hdl="A")
            rid = fr.write_flash_reality(
                conn, name="测试", hdl="状态", current_status=cs,
                timeline=["初始"], source_strands=[sid])
            row = conn.execute(
                "SELECT current_status FROM realities WHERE reality_id=?", (rid,)
            ).fetchone()
            stored = json.loads(row[0])
            self.assertEqual(len(stored.get("key_facts")), 1)
            self.assertEqual(len(stored.get("current_state")), 2)
            conn.close()
        finally:
            db.unlink(missing_ok=True)


class TestQueryTextLimit(unittest.TestCase):
    """措施 3（2026-08-05）：query_text 截断上限，防超长块首提问噪声。

    实测 S38 query_text=29269 字（整段 ComfyUI 错误报告）——提问云
    构建时单样本巨无霸会主导均值。落库与云构建双端截断。
    """

    def test_write_strand_truncates_query(self):
        db = Path("/tmp/__flash_query_limit.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            long_q = "超长提问" * 300  # 1500 字
            sid = fr.write_flash_strand(
                conn, session_id="s", topic_id=1, hdl="A", query_text=long_q)
            row = conn.execute(
                "SELECT query_text FROM strands WHERE strand_id=?", (sid,)
            ).fetchone()
            self.assertLessEqual(len(row[0]), fr.QUERY_TEXT_MAX)
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_normal_query_unchanged(self):
        db = Path("/tmp/__flash_query_limit2.db")
        db.unlink(missing_ok=True)
        try:
            conn = fr.init_flash_db(db)
            q = "comfyui 工作流怎么配置"
            sid = fr.write_flash_strand(
                conn, session_id="s", topic_id=1, hdl="A", query_text=q)
            row = conn.execute(
                "SELECT query_text FROM strands WHERE strand_id=?", (sid,)
            ).fetchone()
            self.assertEqual(row[0], q)
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_question_cloud_truncates(self):
        """提问云构建同样截断（消费端防御）。"""
        long_q = "超长提问" * 300
        calls = {"n": 0, "text": ""}

        def fake_embed(text):
            calls["n"] += 1
            calls["text"] = text
            return [0.1] * 8

        fr.build_question_cloud(
            [{"id": 1, "query_text": long_q}], fake_embed)
        self.assertLessEqual(len(calls["text"]), fr.QUERY_TEXT_MAX)


class TestRealityTwoStage(unittest.TestCase):
    """refine 两阶段（2026-08-05 全量重构）：决策层 + 内容层。"""

    def _strand(self, sid, hdl="测试工作线", ooda=None):
        return {
            "id": sid, "session_id": "s1", "topic_id": 1, "hdl": hdl,
            "ooda": ooda or {"现象与问题": ["问题描述"], "决策与方案": ["方案A"]},
            "query_text": "提问",
        }

    def test_decision_prompt_contains_refs(self):
        strands = [self._strand(1), self._strand(2)]
        refs = [{"reality_id": 7, "name": "连接池优化", "hdl": "已扩容到200",
                 "member_strands": [1, 2]}]
        p = fr.build_reality_decision_prompt(strands, refs)
        self.assertIn("R7", p)
        self.assertIn("连接池优化", p)
        self.assertIn("S1", p)
        self.assertIn("strand_to_reality", p)
        self.assertIn("affected_reality_ids", p)

    def test_parse_reality_decision(self):
        raw = """```json
        {
          "strand_to_reality": {"S1": 7, "S2": "NEW1", "SS3": 8},
          "discarded_strand_ids": [4, "S5"],
          "affected_reality_ids": [7, "NEW1"],
          "notes": "承接"
        }
        ```"""
        d = fr.parse_reality_decision(raw)
        self.assertIsNotNone(d)
        self.assertEqual(d["s2r"], {1: 7, 2: "NEW1", 3: 8})
        self.assertEqual(d["discarded"], [4, 5])
        self.assertEqual(d["affected"], {7, "NEW1"})

    def test_parse_reality_decision_invalid(self):
        self.assertIsNone(fr.parse_reality_decision("oops"))
        self.assertIsNone(fr.parse_reality_decision(None))
        # s2r 缺失或空 → None（空映射视为失败）
        self.assertIsNone(fr.parse_reality_decision('{"notes": "x"}'))
        self.assertIsNone(fr.parse_reality_decision('{"strand_to_reality": {}}'))

    def test_detail_prompt_contains_members(self):
        items = [{"rid": 7, "old": {"reality_id": 7, "name": "连接池",
                                     "hdl": "旧状态", "member_strands": [1]},
                  "strand_ids": [1, 2]}]
        batch = [self._strand(1, "旧成员"), self._strand(2, "新成员")]
        p = fr.build_reality_detail_prompt(items, batch)
        self.assertIn("reality 7", p)
        self.assertIn("旧成员", p)
        self.assertIn("新成员", p)
        self.assertIn("current_status", p)

    def test_parse_reality_detail(self):
        raw = """[
          {"reality_id": "R7", "name": "连接池", "hdl": "已扩容",
           "current_status": {"current_state": ["ok"], "goals": ["继续"]}},
          {"reality_id": 8, "name": "x", "hdl": "y"}
        ]"""
        d = fr.parse_reality_detail(raw)
        self.assertIsNotNone(d)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[0]["reality_id"], 7)
        self.assertEqual(d[1]["reality_id"], 8)
        self.assertEqual(d[0]["name"], "连接池")

    def test_parse_reality_detail_invalid(self):
        self.assertIsNone(fr.parse_reality_detail("oops"))
        self.assertIsNone(fr.parse_reality_detail('{"realities": []}'))
        self.assertIsNone(fr.parse_reality_detail("[]"))
        self.assertIsNone(fr.parse_reality_detail(None))


if __name__ == "__main__":
    unittest.main()
