"""决策 44 续：strand 输入直接消费 Fct affairs（效率优先，不反推事务边界）。"""
import json

from ca.store import SQLiteStore, collect_turn_fcts, write_turn_v5
from ca.topic_summary import _format_turns_for_prompt


def _write_fct(store, session, turn, fct: dict):
    write_turn_v5(store, session, turn, 0, role="user", elm_text="任务",
                  block_type="user_message", ooda_stage="orient")
    write_turn_v5(store, session, turn, 1, role="assistant", elm_text="答复",
                  finish_reason="stop", block_type="agent_reply",
                  ooda_stage="decide", is_fin=1,
                  fct_text=json.dumps(fct, ensure_ascii=False), hdl_text="块hdl")


class TestCollectTurnAffairs:
    def test_affairs_exposed_from_fct(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        _write_fct(store, "s", 7, {
            "changes": [{"stage_tag": "已实施", "core_change": "扁平兜底"}],
            "affairs": [
                {"hdl": "事务A", "turns": [7],
                 "ooda": {"现象与问题": ["问题A"], "背景与约束": [],
                          "决策与方案": ["方案A"], "后续行动": []},
                 "changes": [{"stage_tag": "已实施", "core_change": "方案A落地"}]},
                {"hdl": "事务B", "turns": [7],
                 "ooda": {"现象与问题": [], "背景与约束": [],
                          "决策与方案": ["方案B"], "后续行动": ["跟进B"]},
                 "changes": [{"stage_tag": "计划", "core_change": "方案B将实施"}]},
            ],
        })
        entry = collect_turn_fcts(store, "s", [7])[0]
        assert [a["hdl"] for a in entry["affairs"]] == ["事务A", "事务B"]
        assert entry["affairs"][1]["ooda"]["后续行动"] == ["跟进B"]


class TestFormatTurnsForPrompt:
    def test_affairs_rendered_as_numbered_candidates(self, tmp_path):
        entry = {
            "turn": 7,
            "hdl": "块hdl",
            "changes": ["扁平兜底"],
            "tags": {}, "ooda_tags": {}, "todos": [], "user_requests": [],
            "affairs": [
                {"hdl": "连接池扩容", "turns": [7],
                 "ooda": {"现象与问题": ["池耗尽"], "背景与约束": [],
                          "决策与方案": ["扩容到200"], "后续行动": []},
                 "changes": [{"stage_tag": "已实施", "core_change": "扩容到200"}]},
                {"hdl": "检索修复", "turns": [7],
                 "ooda": {"现象与问题": ["召回空"], "背景与约束": [],
                          "决策与方案": ["本地化"], "后续行动": []},
                 "changes": [{"stage_tag": "计划", "core_change": "本地化"}]},
            ],
        }
        text = _format_turns_for_prompt([entry])
        assert "# 轮次 7" in text
        assert "事务清单（编号即候选 strand）" in text
        assert "1. 连接池扩容" in text
        assert "2. 检索修复" in text
        assert "### 决策与方案" in text
        assert "扁平兜底" not in text, "affairs 路径不应重复 legacy changes"

    def test_legacy_turn_without_affairs_unchanged(self):
        entry = {
            "turn": 8, "hdl": "h", "changes": ["旧格式变更"],
            "tags": {"旧格式变更": "已实施"}, "ooda_tags": {},
            "todos": [], "user_requests": [],
        }
        text = _format_turns_for_prompt([entry])
        assert "旧格式变更" in text
        assert "事务清单" not in text
