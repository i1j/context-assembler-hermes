"""第三轮 T6：设计 Step 5 交叉验证 prompt Reality 化。

覆盖：
  - _CROSS_VALIDATE_PROMPT 无 entry_title/entry_facts 字样，含 reality 字段
  - 构造 reality 候选 → .format 输出含 name/hdl/current_status
  - _load_refinement_candidates 返回 hdl/current_status 键（realities 语义）
"""

import json

from ca.refinement import _CROSS_VALIDATE_PROMPT, IdleRefinementDaemon
from ca.store import create_reality, _get_topic_conn


class TestCrossValidatePromptReality:
    def test_prompt_has_no_entry_semantics(self):
        """prompt 不含 entry_title/entry_facts（theme 时代语义），含 reality 字段。"""
        assert "entry_title" not in _CROSS_VALIDATE_PROMPT
        assert "entry_facts" not in _CROSS_VALIDATE_PROMPT
        assert "{reality_name}" in _CROSS_VALIDATE_PROMPT
        assert "{reality_hdl}" in _CROSS_VALIDATE_PROMPT
        assert "{reality_cs}" in _CROSS_VALIDATE_PROMPT
        # 对齐 reality current_status 四段语义
        assert "current_state" in _CROSS_VALIDATE_PROMPT
        assert "key_facts" in _CROSS_VALIDATE_PROMPT
        assert "goals" in _CROSS_VALIDATE_PROMPT
        assert "context" in _CROSS_VALIDATE_PROMPT

    def test_format_output_contains_reality_fields(self):
        """构造 reality 候选 → .format 输出含 name/hdl/current_status。"""
        cs = {"current_state": ["连接池上限调至 200"],
              "key_facts": ["连接池耗尽导致超时"],
              "goals": ["压测报告待输出"],
              "context": []}
        prompt = _CROSS_VALIDATE_PROMPT.format(
            reality_name=json.dumps("连接池优化", ensure_ascii=False),
            reality_hdl=json.dumps("完成参数优化并验证", ensure_ascii=False),
            reality_cs=json.dumps(cs, ensure_ascii=False),
            fct_data=json.dumps([{"turn": 1, "fct": "连接池耗尽"}],
                                ensure_ascii=False),
        )
        assert "连接池优化" in prompt
        assert "完成参数优化并验证" in prompt
        assert "连接池耗尽导致超时" in prompt
        assert "压测报告待输出" in prompt

    def test_candidates_include_hdl_and_current_status(self, tmp_path):
        """_load_refinement_candidates 返回 hdl/current_status 键（realities 语义）。"""
        db = tmp_path / "ca_topics.db"
        create_reality(
            profile="tester", name="连接池优化", hdl="完成参数优化并验证",
            current_status={"current_state": ["连接池上限调至 200"],
                            "key_facts": ["连接池耗尽导致超时"],
                            "goals": ["压测报告待输出"], "context": []},
            timeline_entry=None, source_strand=None,
            centroid_json=None, db_path=db)
        conn = _get_topic_conn(db)
        daemon = IdleRefinementDaemon()
        try:
            candidates = daemon._load_refinement_candidates(conn, set(), 5)
        finally:
            conn.close()
        assert len(candidates) == 1
        c = candidates[0]
        assert c["hdl"] == "完成参数优化并验证"
        assert c["current_status"]["key_facts"] == ["连接池耗尽导致超时"]
        assert c["current_status"]["goals"] == ["压测报告待输出"]
