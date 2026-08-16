"""决策 44 续：Fct 多事务 OODA 输出解析 + think 卡输入筛选测试。"""
import json

from ca.fct_multi_affair import (
    parse_fct_multi_affair,
    flatten_affairs_to_legacy,
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
        "决策与方案": ["扩容到200"],
        "后续行动": ["观察慢查询"]
      },
      "changes": [
        {"stage_tag": "已实施", "core_change": "连接池扩容到200"}
      ]
    },
    {
      "hdl": "检索链路修复",
      "turns": [7],
      "ooda": {
        "现象与问题": ["召回为空"],
        "背景与约束": [],
        "决策与方案": ["本地化检索"],
        "后续行动": []
      },
      "changes": [
        {"stage_tag": "计划", "core_change": "检索链路本地化"}
      ]
    }
  ]
}
```
"""


class TestParseFctMultiAffair:
    def test_parses_fenced_json(self):
        result = parse_fct_multi_affair(MULTI_RESPONSE)
        assert result is not None
        assert len(result["affairs"]) == 2
        assert result["affairs"][0]["hdl"] == "连接池扩容"
        assert result["affairs"][1]["ooda"]["现象与问题"] == ["召回为空"]

    def test_plain_json_without_fence(self):
        obj = {"affairs": [{"hdl": "a", "ooda": {}, "changes": []}]}
        result = parse_fct_multi_affair(json.dumps(obj, ensure_ascii=False))
        assert result is not None

    def test_no_affairs_returns_none(self):
        assert parse_fct_multi_affair('{"changes": []}') is None
        assert parse_fct_multi_affair("### 现象与问题\n- 无") is None

    def test_filters_invalid_changes_and_fills_ooda_keys(self):
        obj = {"affairs": [{
            "hdl": "a",
            "ooda": {"现象与问题": ["x"], "背景与约束": "bad"},
            "changes": [{"stage_tag": "坏", "core_change": "坏"},
                        {"stage_tag": "已实施", "core_change": "好"}],
        }]}
        result = parse_fct_multi_affair(json.dumps(obj, ensure_ascii=False))
        affair = result["affairs"][0]
        assert set(affair["ooda"].keys()) == {"现象与问题", "背景与约束", "决策与方案", "后续行动"}
        assert affair["changes"] == [{"stage_tag": "已实施", "core_change": "好"}]


class TestFlattenAffairsToLegacy:
    def test_flatten_backward_compatible(self):
        parsed = parse_fct_multi_affair(MULTI_RESPONSE)
        legacy = flatten_affairs_to_legacy(parsed["affairs"])
        assert [c["core_change"] for c in legacy["changes"]] == [
            "连接池扩容到200", "检索链路本地化"]
        assert legacy["core_change"] == "连接池扩容到200；检索链路本地化"
        assert "连接池耗尽" in legacy["new_materials"]
        assert "扩容到200" in legacy["consensus"]
        assert [a["hdl"] for a in legacy["affairs"]] == [
            "连接池扩容", "检索链路修复"]
        assert legacy["affairs"][0]["ooda"]["现象与问题"] == ["连接池耗尽"]
        assert legacy["_fct_format"] == "v2-multi-affair"

    def test_empty_hdl_fallback(self):
        affairs = [{"hdl": "", "ooda": {"决策与方案": ["做成A"]},
                    "changes": [{"stage_tag": "计划", "core_change": "做成A"}]}]
        legacy = flatten_affairs_to_legacy(affairs)
        assert legacy["affairs"][0]["hdl"] == "做成A"
        assert legacy["changes"][0]["core_change"] == "做成A"


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
