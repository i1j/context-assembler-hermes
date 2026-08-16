"""决策 45：Fct 多事务 OODA 记录（v3）解析 + 单一数据源落库 + think 卡筛选。

v3 契约：affairs[].ooda 阶段项即变更记录，无 changes/stage_tag；
落库 Fct JSON 只有 affairs + 装配元数据（无 legacy 扁平字段）。
v2 旧库记录（带 changes）仍可解析，changes 保留用于只读兼容。
"""
import json

from ca.fct_multi_affair import (
    parse_fct_multi_affair,
    build_fct_multi_affair,
    build_fct_think_context,
)
from ca.store import SQLiteStore, write_turn_v5, write_think_card_v1


MULTI_RESPONSE = """
```json
{
  "affairs": [
    {
      "hdl": "连接池扩容",
      "turns": [7],
      "ooda": {
        "现象与问题": ["连接池耗尽"],
        "背景与约束": ["上限100"],
        "决策与方案": ["连接池扩容到200"],
        "后续行动": ["观察慢查询"]
      }
    },
    {
      "hdl": "检索链路修复",
      "turns": [7],
      "ooda": {
        "现象与问题": ["召回为空"],
        "背景与约束": [],
        "决策与方案": ["检索链路本地化"],
        "后续行动": []
      }
    }
  ]
}
```
"""

V2_LEGACY_RESPONSE = """{"affairs":[{"hdl":"旧事务","turns":[6],"ooda":{
  "现象与问题":["旧问题"],"背景与约束":[],"决策与方案":["旧方案"],"后续行动":[]},
  "changes":[{"stage_tag":"已实施","core_change":"旧方案落地"}]}]}"""


class TestParseFctMultiAffair:
    def test_parses_fenced_json_v3(self):
        result = parse_fct_multi_affair(MULTI_RESPONSE)
        assert result is not None
        assert len(result["affairs"]) == 2
        assert result["affairs"][0]["hdl"] == "连接池扩容"
        assert result["affairs"][1]["ooda"]["现象与问题"] == ["召回为空"]
        # v3：OODA 阶段即变更，无 changes 字段
        assert "changes" not in result["affairs"][0]
        assert "changes" not in result["affairs"][1]

    def test_plain_json_without_fence(self):
        obj = {"affairs": [{"hdl": "a", "ooda": {}}]}
        result = parse_fct_multi_affair(json.dumps(obj, ensure_ascii=False))
        assert result is not None
        assert "changes" not in result["affairs"][0]

    def test_no_affairs_returns_none(self):
        assert parse_fct_multi_affair('{"changes": []}') is None
        assert parse_fct_multi_affair("### 现象与问题\n- 无") is None

    def test_fills_ooda_keys_and_keeps_v2_changes_for_legacy_read(self):
        obj = {"affairs": [{
            "hdl": "a",
            "ooda": {"现象与问题": ["x"], "背景与约束": "bad"},
            "changes": [{"stage_tag": "坏", "core_change": "坏"},
                        {"stage_tag": "已实施", "core_change": "好"}],
        }]}
        result = parse_fct_multi_affair(json.dumps(obj, ensure_ascii=False))
        affair = result["affairs"][0]
        assert set(affair["ooda"].keys()) == {"现象与问题", "背景与约束", "决策与方案", "后续行动"}
        # v2 只读兼容：有效 changes 保留，无效过滤
        assert affair["changes"] == [{"stage_tag": "已实施", "core_change": "好"}]


class TestBuildFctMultiAffair:
    def test_v3_single_source_fct(self):
        parsed = parse_fct_multi_affair(MULTI_RESPONSE)
        fct = build_fct_multi_affair(parsed["affairs"], assemble_status=0)
        assert fct["_fct_format"] == "v3-multi-affair-ooda"
        assert fct["_assemble_status"] == 0
        assert [a["hdl"] for a in fct["affairs"]] == ["连接池扩容", "检索链路修复"]
        # 单一数据源：不写任何 legacy 扁平字段
        assert set(fct.keys()) == {"affairs", "_assemble_status", "_fct_format"}
        assert all("changes" not in a for a in fct["affairs"])

    def test_empty_hdl_fallback(self):
        fct = build_fct_multi_affair(
            [{"hdl": "", "ooda": {"决策与方案": ["做成A"]}}], assemble_status=0)
        assert fct["affairs"][0]["hdl"] == "做成A"

    def test_v2_legacy_changes_preserved_inside_affair(self):
        """旧库 v2 记录只读兼容：per-affair changes 原样保留在 affair 内。"""
        parsed = parse_fct_multi_affair(V2_LEGACY_RESPONSE)
        fct = build_fct_multi_affair(parsed["affairs"], assemble_status=0)
        assert fct["affairs"][0]["changes"] == [
            {"stage_tag": "已实施", "core_change": "旧方案落地"}]
        assert set(fct.keys()) == {"affairs", "_assemble_status", "_fct_format"}


class TestBuildFctThinkContext:
    def test_filters_current_turn_orient_first_and_budget(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        session = "s"
        # 首 think（orient）+ 两段 reasoning 行；再放一个前轮 decision 干扰
        write_turn_v5(store, session, 1, 0, role="user", elm_text="修两件事",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(store, session, 1, 1, role="assistant", elm_text="首段" * 500,
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(store, session, 1, 2, role="assistant", elm_text="第二段",
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(store, session, 0, 1, role="assistant", elm_text="旧轮",
                      block_type="thinking", ooda_stage="decide")
        write_think_card_v1(store, {
            "session_id": session, "turn": 1, "seq": 1, "txn_id": 1,
            "source_kind": "cloud_think", "card_kind": "orient",
            "raw_len": 2000, "preview": "首段" * 80, "status": "raw"})
        write_think_card_v1(store, {
            "session_id": session, "turn": 0, "seq": 1, "txn_id": 0,
            "source_kind": "cloud_think", "card_kind": "decision",
            "raw_len": 2, "preview": "旧", "status": "raw"})

        text = build_fct_think_context(store, session, 1,
                                       max_cards=2, max_chars_per_card=600,
                                       total_budget=1000)
        assert "[思考卡 orient" in text
        assert "修两件事" in text
        assert "首段" in text
        assert "旧轮" not in text
        assert "第二段" not in text or "[思考卡 decision" not in text

    def test_empty_when_no_cards(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        assert build_fct_think_context(store, "s", 1) == ""
