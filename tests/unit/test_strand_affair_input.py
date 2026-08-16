"""决策 45：strand 输入直接消费 Fct affairs（单一数据源，OODA 阶段项即变更）。"""
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


V3_FCT = {
    "_assemble_status": 0,
    "_fct_format": "v3-multi-affair-ooda",
    "affairs": [
        {"hdl": "事务A", "turns": [7],
         "ooda": {"现象与问题": ["问题A"], "背景与约束": [],
                  "决策与方案": ["方案A"], "后续行动": []}},
        {"hdl": "事务B", "turns": [7],
         "ooda": {"现象与问题": [], "背景与约束": ["约束B"],
                  "决策与方案": ["方案B"], "后续行动": ["跟进B"]}},
    ],
}


class TestCollectTurnAffairs:
    def test_v3_fct_single_source_derives_legacy_view(self, tmp_path):
        """v3 Fct 无 legacy 扁平字段；collect_turn_fcts 从 affairs 现场派生视图。"""
        store = SQLiteStore(tmp_path / "s.db")
        _write_fct(store, "s", 7, V3_FCT)
        entry = collect_turn_fcts(store, "s", [7])[0]

        assert [a["hdl"] for a in entry["affairs"]] == ["事务A", "事务B"]
        assert entry["affairs"][1]["ooda"]["后续行动"] == ["跟进B"]
        # v3：affair 无 changes 字段
        assert not entry["affairs"][0].get("changes")

        # 旧消费者视图由 OODA 阶段项派生
        assert entry["changes"] == ["问题A", "方案A", "约束B", "方案B", "跟进B"]
        assert entry["ooda_tags"] == {
            "问题A": "现象与问题",
            "方案A": "决策与方案",
            "约束B": "背景与约束",
            "方案B": "决策与方案",
            "跟进B": "后续行动",
        }
        assert entry["todos"] == ["跟进B"]
        assert entry["new_materials"] == ["问题A"]
        assert entry["key_facts_supp"] == ["约束B"]
        assert entry["consensus"] == ["方案A", "方案B"]
        assert entry["tags"] == {}

    def test_v3_fct_without_hdl_uses_first_affair_hdl(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        write_turn_v5(store, "s", 7, 0, role="user", elm_text="任务",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(store, "s", 7, 1, role="assistant", elm_text="答复",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", is_fin=1,
                      fct_text=json.dumps(V3_FCT, ensure_ascii=False), hdl_text="")
        entry = collect_turn_fcts(store, "s", [7])[0]
        assert entry["hdl"] == "事务A"

    def test_v2_legacy_affair_changes_keep_tags(self, tmp_path):
        """v2 旧库记录（affairs 带 changes）只读兼容：标签保留在派生视图。"""
        store = SQLiteStore(tmp_path / "s.db")
        _write_fct(store, "s", 6, {
            "affairs": [{
                "hdl": "旧事务", "turns": [6],
                "ooda": {"现象与问题": ["旧问题"], "背景与约束": [],
                         "决策与方案": ["旧方案"], "后续行动": []},
                "changes": [{"stage_tag": "已实施", "core_change": "旧方案落地"}],
            }],
        })
        entry = collect_turn_fcts(store, "s", [6])[0]
        assert entry["tags"] == {"旧方案落地": "已实施"}
        assert "旧问题" in entry["changes"]
        assert "旧方案落地" in entry["changes"]


class TestFormatTurnsForPrompt:
    def test_v3_affairs_rendered_as_numbered_candidates_without_status_tags(self, tmp_path):
        entry = {
            "turn": 7,
            "hdl": "块hdl",
            "changes": ["问题A", "方案A"],
            "tags": {}, "ooda_tags": {}, "todos": [], "user_requests": [],
            "affairs": [
                {"hdl": "连接池扩容", "turns": [7],
                 "ooda": {"现象与问题": ["池耗尽"], "背景与约束": [],
                          "决策与方案": ["扩容到200"], "后续行动": []}},
                {"hdl": "检索修复", "turns": [7],
                 "ooda": {"现象与问题": ["召回空"], "背景与约束": [],
                          "决策与方案": ["本地化"], "后续行动": []}},
            ],
        }
        text = _format_turns_for_prompt([entry])
        assert "# 轮次 7" in text
        assert "事务清单（编号即候选 strand）" in text
        assert "1. 连接池扩容" in text
        assert "2. 检索修复" in text
        assert "### 现象与问题" in text
        assert "### 决策与方案" in text
        assert "扁平兜底" not in text, "affairs 路径不应重复 legacy changes"
        # 决策 45：无 changes 小节、无【已完成】等状态标签
        assert "### changes" not in text
        assert "[已实施]" not in text
        assert "【已实施】" not in text

    def test_v2_legacy_affair_changes_still_rendered(self):
        """旧库 v2 记录（带 changes/stage_tag）只读兼容展示。"""
        entry = {
            "turn": 6, "hdl": "h", "changes": [], "tags": {},
            "ooda_tags": {}, "todos": [], "user_requests": [],
            "affairs": [{
                "hdl": "旧事务", "turns": [6],
                "ooda": {"现象与问题": ["旧问题"], "背景与约束": [],
                         "决策与方案": ["旧方案"], "后续行动": []},
                "changes": [{"stage_tag": "已实施", "core_change": "旧方案落地"}],
            }],
        }
        text = _format_turns_for_prompt([entry])
        assert "### changes（旧版兼容）" in text
        assert "[已实施] 旧方案落地" in text

    def test_legacy_turn_without_affairs_unchanged(self):
        entry = {
            "turn": 8, "hdl": "h", "changes": ["旧格式变更"],
            "tags": {"旧格式变更": "已实施"}, "ooda_tags": {},
            "todos": [], "user_requests": [],
        }
        text = _format_turns_for_prompt([entry])
        assert "旧格式变更" in text
        assert "事务清单" not in text
