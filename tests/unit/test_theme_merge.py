"""theme 同步归并链路 — ca/theme.py run_theme_merge（v6.5 重构版）。

设计决策对照（wiki theme 重构方案 v4/v5）:
  → M2 两段式：向量召回（0.70 门槛 top-K）→ 4B 决策 → 按 theme 4B 更新
  → 单向否决（merge 窄）：无候选 strand 直接 new；4B 拒绝 merge → 转 create
  → 4B 失败 → 代码兜底（create: title=hdl；merge: 仅追加 timeline_overview）
  → timeline 仅 overview 条目，按主题块追加
  → 向量规范：只 embed 语义文本（hdl + ooda），不 embed JSON

覆盖:
  - find_theme_candidates: 余弦 / threshold 过滤 / top-K 排序
  - run_theme_merge 全流程: merge / create / 无候选 new / 4B 拒绝转 create
  - 4B 失败兜底: create 兜底 / merge 兜底
  - parse_theme_response: 宽松解析
"""

import json
import sys
from pathlib import Path

import pytest

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.theme as theme_mod
from ca.store import _get_topic_conn


# ── 伪嵌入：字符集哈希 → 确定性向量 ──
def _fake_embed(text: str) -> list:
    """确定性伪嵌入：唯一字符集 → md5 哈希维度（共享字符多 → 余弦高）。"""
    import hashlib
    vec = [0.0] * 128
    for ch in set(text):
        idx = int(hashlib.md5(ch.encode("utf-8")).hexdigest(), 16) % 128
        vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm > 0 else vec


class FakeEmbedClient:
    def embed(self, text: str):
        return _fake_embed(text)


class FakeLLMCall:
    """可编程 fake 4B：按 prompt 内容返回不同 JSON。"""

    def __init__(self, assignments=None, merge_result=None, create_result=None,
                 fail_all=False, calls=None):
        self.assignments = assignments
        self.merge_result = merge_result
        self.create_result = create_result
        self.fail_all = fail_all
        self.calls = calls if calls is not None else []

    def __call__(self, prompt: str):
        self.calls.append(prompt)
        if self.fail_all:
            return None
        if "assignments" in prompt:
            return json.dumps({"assignments": self.assignments or {}})
        if "已有主题" in prompt:
            return json.dumps(self.merge_result or {
                "merge": True, "title": "合并后主题", "overview": "融合概述",
                "ooda": {"决策与方案": ["融合决策"]},
                "key_facts": ["融合结论"], "open_items": [],
                "timeline_overview": "本块进展",
            })
        return json.dumps(self.create_result or {
            "title": "新主题", "overview": "初始概述",
            "ooda": {"决策与方案": ["初始决策"]},
            "key_facts": ["初始结论"], "open_items": [],
            "timeline_overview": "首块进展",
        })


@pytest.fixture
def theme_db(tmp_path):
    from ca.store import _get_topic_conn
    db = tmp_path / "ca_topics.db"
    conn = _get_topic_conn(db)
    yield db
    conn.close()


def _strand(hdl: str, topic_id: int = 1, sid: str = "s1", strand_id: int = 1,
            **ooda_items) -> dict:
    ooda = {"决策与方案": ooda_items.get("决策与方案", [f"{hdl}决策"])}
    return {
        "hdl": hdl, "turns": [topic_id], "topic_id": topic_id,
        "session_id": sid, "strand_id": strand_id,
        "ooda": ooda, "changes": ooda["决策与方案"],
        "key_facts": [f"{hdl}结论"],
    }


def _mk_theme(tid: int, title: str, overview: str = "", ooda=None) -> dict:
    return {"theme_id": tid, "title": title, "overview": overview,
            "ooda": ooda or {}, "key_facts": [], "open_items": [],
            "timeline": [], "changes": [], "source_strands": {},
            "centroid": _fake_embed(title + overview), "profile": "tester"}


class TestFindThemeCandidates:
    """向量召回：余弦 / 阈值 / top-K。"""

    def test_similar_theme_is_candidate(self):
        theme = _mk_theme(1, "连接池与超时配置优化")
        strand_vec = _fake_embed("连接池与超时配置优化")
        cands = theme_mod.find_theme_candidates(strand_vec, [theme], 0.70)
        assert len(cands) == 1
        assert cands[0]["theme_id"] == 1
        assert cands[0]["sim"] > 0.7

    def test_below_threshold_excluded(self):
        theme = _mk_theme(1, "完全无关的主题甲")
        strand_vec = _fake_embed("连接池优化")
        cands = theme_mod.find_theme_candidates(strand_vec, [theme], 0.70)
        assert cands == []

    def test_top_k_limits_and_sorts(self):
        themes = [_mk_theme(1, "主题甲"), _mk_theme(2, "主题乙"),
                  _mk_theme(3, "主题丙"), _mk_theme(4, "主题丁")]
        strand_vec = _fake_embed("主题甲 主题乙 主题丙")
        cands = theme_mod.find_theme_candidates(strand_vec, themes, 0.0, top_k=2)
        assert len(cands) == 2
        assert cands[0]["sim"] >= cands[1]["sim"]

    def test_missing_centroid_skipped(self):
        theme = {"theme_id": 1, "title": "x", "centroid": None}
        cands = theme_mod.find_theme_candidates([0.1] * 10, [theme], 0.0)
        assert cands == []

    def test_priority_themes_injected_first_even_below_threshold(self):
        """v6.5.3 归并优先级（2026-08-02 用户确认）：切换注入的候选 theme 优先。
        即使 strand 与注入 theme 的 sim 略低于 threshold，也应成为候选（排前），
        因为它是话题切换时语义检索的参考主题（首轮 recall / FAR 注入的 3 个）。
        """
        # 注入 theme：与 strand 字符重合少（sim < 0.70 门槛）
        injected = _mk_theme(1, "监控告警阈值调优")
        # 非注入 theme：与 strand 高度重合（sim 高）
        other = _mk_theme(2, "连接池与超时配置优化")
        strand_vec = _fake_embed("连接池与超时配置优化")
        sim_inj = theme_mod._cosine(strand_vec, injected["centroid"])
        sim_oth = theme_mod._cosine(strand_vec, other["centroid"])
        assert sim_inj < 0.70 and sim_oth >= 0.70, f"前置: inj={sim_inj:.3f} oth={sim_oth:.3f}"
        cands = theme_mod.find_theme_candidates(
            strand_vec, [injected, other], 0.70,
            priority_themes=[{"theme_id": 1, "title": "监控告警阈值调优"}],
        )
        # 注入 theme 即使 sim 不足也应优先出现
        assert cands, "注入 theme 不应被阈值挡掉"
        assert cands[0]["theme_id"] == 1
        assert any(c["theme_id"] == 2 for c in cands)

    def test_priority_themes_respect_top_k(self):
        """注入 theme 优先但不越界 top_k。"""
        injected = _mk_theme(1, "主题甲")
        other = _mk_theme(2, "主题乙")
        strand_vec = _fake_embed("主题甲 主题乙")
        cands = theme_mod.find_theme_candidates(
            strand_vec, [injected, other], 0.70, top_k=1,
            priority_themes=[{"theme_id": 1}],
        )
        assert len(cands) == 1
        assert cands[0]["theme_id"] == 1

    def test_priority_themes_none_keeps_old_behavior(self):
        """无 priority_themes → 行为与旧版一致（threshold 硬门槛）。"""
        theme = _mk_theme(1, "完全无关的主题甲")
        strand_vec = _fake_embed("连接池优化")
        cands = theme_mod.find_theme_candidates(strand_vec, [theme], 0.70)
        assert cands == []


class TestRunThemeMerge:
    """全流程：merge / create / 无候选 new / 4B 拒绝转 create / 兜底。"""

    def test_all_new_strands_created(self, theme_db):
        """无候选（空 themes）→ 全部 create。"""
        strands = [
            _strand("工作线甲", topic_id=1, sid="s1", strand_id=1),
            _strand("工作线乙", topic_id=1, sid="s1", strand_id=2),
        ]
        llm = FakeLLMCall()
        stats = theme_mod.run_theme_merge(
            strands, [], embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 2
        assert stats["merged"] == 0

        from ca.store import load_all_themes
        themes = load_all_themes(db_path=theme_db)
        assert len(themes) == 2
        assert themes[0]["source_strands"] == {"s1": [1]}

    def test_similar_strand_merged_into_existing(self, theme_db):
        """有候选 + 4B merge → 归入已有 theme（timeline 追加）。"""
        # 预置一个 theme
        from ca.store import create_theme
        tid = create_theme(
            profile="tester", title="连接池与超时配置优化", overview="初始",
            ooda={"决策与方案": ["旧决策"]}, key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("连接池与超时配置优化")),
            db_path=theme_db,
        )
        from ca.store import load_all_themes
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("连接池与超时配置优化", topic_id=2, sid="s1", strand_id=1)
        llm = FakeLLMCall(assignments={"strand_1": {"action": "merge", "target": 0}})
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester")

        assert stats["merged"] == 1
        themes = load_all_themes(db_path=theme_db)
        t = themes[0]
        assert t["theme_id"] == tid
        # timeline 追加（seq 1 → 2）
        assert [e["seq"] for e in t["timeline"]] == [1, 2]
        assert t["timeline"][-1]["overview"] == "本块进展"
        # ooda 覆盖为融合结果
        assert t["ooda"]["决策与方案"] == ["融合决策"]
        # source_strands 追加
        assert t["source_strands"]["s1"] == [1]

    def test_llm_rejects_merge_creates_new(self, theme_db):
        """4B 决策 new（拒绝候选）→ 新建 theme。"""
        from ca.store import create_theme, load_all_themes
        create_theme(
            profile="tester", title="旧主题", overview="", ooda={},
            key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("旧主题")),
            db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("连接池优化", topic_id=2, sid="s1", strand_id=1)
        llm = FakeLLMCall(assignments={"strand_1": {"action": "new"}})
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert stats["merged"] == 0
        assert len(load_all_themes(db_path=theme_db)) == 2

    def test_merge_rejected_at_update_creates_new(self, theme_db):
        """4B 决策 merge，但 merge prompt 返回 merge:false → 转 create。"""
        from ca.store import create_theme, load_all_themes
        create_theme(
            profile="tester", title="旧主题", overview="", ooda={},
            key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("旧主题")),
            db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("连接池优化", topic_id=2, sid="s1", strand_id=1)
        llm = FakeLLMCall(
            assignments={"strand_1": {"action": "merge", "target": 0}},
            merge_result={"merge": False},
        )
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(load_all_themes(db_path=theme_db)) == 2

    def test_create_llm_failure_falls_back_to_hdl(self, theme_db):
        """4B 全失败 → create 兜底（title=hdl，ooda=strand ooda）。"""
        strand = _strand("工作线甲", topic_id=1, sid="s1", strand_id=1)
        llm = FakeLLMCall(fail_all=True)
        stats = theme_mod.run_theme_merge(
            [strand], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1

        from ca.store import load_all_themes
        themes = load_all_themes(db_path=theme_db)
        assert themes[0]["title"] == "工作线甲"
        assert themes[0]["ooda"]["决策与方案"] == ["工作线甲决策"]

    def test_merge_llm_failure_appends_timeline_overview(self, theme_db):
        """merge 4B 失败 → 兜底：timeline 追加（归并: hdl），ooda 不变。"""
        from ca.store import create_theme, load_all_themes
        create_theme(
            profile="tester", title="连接池与超时配置优化", overview="初始",
            ooda={"决策与方案": ["旧决策"]}, key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("连接池与超时配置优化")),
            db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("连接池与超时配置优化", topic_id=2, sid="s1", strand_id=1)
        llm = FakeLLMCall(
            assignments={"strand_1": {"action": "merge", "target": 0}},
            merge_result=None,  # merge 调用返回 None（模拟失败）
        )
        # FakeLLMCall 对 merge prompt 返回 merge_result（None → json.dumps(None) 失败？）
        # 需专门 fake：merge 调用返回 None
        class FailMergeCall(FakeLLMCall):
            def __call__(self, prompt):
                self.calls.append(prompt)
                if "assignments" in prompt:
                    return json.dumps({"assignments": {"strand_1": {"action": "merge", "target": 0}}})
                if "已有主题" in prompt:
                    return None  # merge 4B 失败
                return None

        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=FailMergeCall(), db_path=theme_db, profile="tester")
        assert stats["merged"] == 1

        themes = load_all_themes(db_path=theme_db)
        t = themes[0]
        assert [e["seq"] for e in t["timeline"]] == [1, 2]
        assert "归并" in t["timeline"][-1]["overview"]
        assert t["ooda"]["决策与方案"] == ["旧决策"]  # ooda 不变


    def test_merge_fallback_writes_method_fallback(self, theme_db):
        """merge 4B 失败 → 代码兜底归并 → theme_strand_map.method='fallback'。"""
        from ca.store import create_theme, load_all_themes
        create_theme(
            profile="tester", title="连接池与超时配置优化", overview="初始",
            ooda={"决策与方案": ["旧决策"]}, key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("连接池与超时配置优化")),
            db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)
        strand = _strand("连接池与超时配置优化", topic_id=2, sid="s1", strand_id=1)

        class FailMergeCall(FakeLLMCall):
            def __call__(self, prompt):
                self.calls.append(prompt)
                if "assignments" in prompt:
                    return json.dumps({"assignments": {"strand_1": {"action": "merge", "target": 0}}})
                if "已有主题" in prompt:
                    return None  # merge 4B 失败
                return None

        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=FailMergeCall(), db_path=theme_db, profile="tester")
        assert stats["merged"] == 1

        from ca.store import _get_topic_conn
        conn = _get_topic_conn(theme_db)
        method = conn.execute(
            "SELECT method FROM theme_strand_map WHERE session_id='s1' AND strand_id=1"
        ).fetchone()[0]
        assert method == "fallback"

    def test_theme_ref_direct_merge_skips_decide(self, theme_db):
        """v6.5.3: strand 带有效 theme_ref → 直接归并（跳过向量召回+decide）。"""
        from ca.store import create_theme, load_all_themes
        tid = create_theme(
            profile="tester", title="连接池与超时配置优化", overview="初始",
            ooda={"决策与方案": ["旧决策"]}, key_facts=[], open_items=[],
            timeline_entry={"seq": 1, "topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "初始"},
            source_strand={"session_id": "s0", "strand_id": 9},
            centroid_json=json.dumps(_fake_embed("连接池与超时配置优化")),
            db_path=theme_db,
        )
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("连接池与超时配置优化", topic_id=2, sid="s1", strand_id=1)
        strand["theme_ref"] = tid
        llm = FakeLLMCall(fail_all=True)  # decide 若被调用会返回 None → 全 new
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["merged"] == 1
        assert stats["created"] == 0
        # decide 未被调用（无 assignments prompt）
        assert not any("assignments" in c for c in llm.calls)
        # strand 已归入 theme tid
        from ca.store import _get_topic_conn
        conn = _get_topic_conn(theme_db)
        row = conn.execute(
            "SELECT theme_id, method FROM theme_strand_map "
            "WHERE session_id='s1' AND strand_id=1"
        ).fetchone()
        assert row[0] == tid

    def test_theme_ref_invalid_falls_back_to_create(self, theme_db):
        """v6.5.3: theme_ref 指向不存在 theme → 丢弃，走 create。"""
        strand = _strand("工作线甲", topic_id=1, sid="s1", strand_id=1)
        strand["theme_ref"] = 9999  # 不存在
        llm = FakeLLMCall()
        stats = theme_mod.run_theme_merge(
            [strand], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert stats["merged"] == 0


class TestCreateMissingOverview:
    """P2 (v6.5.1): create 4B 返回 ooda 但缺 overview → 重试一次 + 代码拼接兜底。"""

    class _CreateFake:
        """可编程 fake：create 调用按序列返回不同结果。"""

        def __init__(self, responses, calls=None):
            self.responses = list(responses)   # 每次 create 调用弹出一个
            self.calls = calls if calls is not None else []

        def __call__(self, prompt):
            self.calls.append(prompt)
            if not self.responses:
                return None
            return self.responses.pop(0)

    def _strand(self):
        return _strand("工作线甲", topic_id=1, sid="s1", strand_id=1)

    def test_retry_when_overview_missing(self, theme_db):
        """第一次 create 无 overview（有 ooda）→ 重试 → 用第二次结果。"""
        missing_ov = json.dumps({
            "title": "主题甲", "overview": "",
            "ooda": {"决策与方案": ["决策A"]},
            "key_facts": [], "open_items": [],
            "timeline_overview": "进展",
        })
        full_ov = json.dumps({
            "title": "主题甲", "overview": "完整概述",
            "ooda": {"决策与方案": ["决策A"]},
            "key_facts": [], "open_items": [],
            "timeline_overview": "进展",
        })
        llm = self._CreateFake([missing_ov, full_ov])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(llm.calls) == 2   # 重试了一次

        from ca.store import load_all_themes
        themes = load_all_themes(db_path=theme_db)
        assert themes[0]["overview"] == "完整概述"

    def test_retry_fails_pads_overview_from_ooda(self, theme_db):
        """重试仍缺 overview → 代码拼接（决策与方案 + 现象与问题，≤3 条）。"""
        missing_ov = json.dumps({
            "title": "主题甲", "overview": "",
            "ooda": {"决策与方案": ["决策A", "决策B"],
                     "现象与问题": ["问题X"]},
            "key_facts": [], "open_items": [], "timeline_overview": "进展",
        })
        llm = self._CreateFake([missing_ov, missing_ov])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(llm.calls) == 2

        from ca.store import load_all_themes
        themes = load_all_themes(db_path=theme_db)
        ov = themes[0]["overview"]
        assert "决策A" in ov and "决策B" in ov       # 拼接自决策与方案
        assert "问题X" in ov                        # 含现象与问题
        assert themes[0]["ooda"]["决策与方案"] == ["决策A", "决策B"]  # ooda 不受影响

    def test_normal_overview_no_retry(self, theme_db):
        """正常返回 overview → 不重试（仅 1 次调用）。"""
        ok = json.dumps({
            "title": "主题甲", "overview": "正常概述",
            "ooda": {"决策与方案": ["决策A"]},
            "key_facts": [], "open_items": [], "timeline_overview": "进展",
        })
        llm = self._CreateFake([ok])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(llm.calls) == 1   # 不重试

        from ca.store import load_all_themes
        assert load_all_themes(db_path=theme_db)[0]["overview"] == "正常概述"

    def test_llm_failed_no_retry_uses_hdl_fallback(self, theme_db):
        """4B 完全失败（None）→ 不重试，走 hdl fallback（title=hdl）。"""
        llm = self._CreateFake([None, None])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(llm.calls) == 1   # None 不触发重试

        from ca.store import load_all_themes
        assert load_all_themes(db_path=theme_db)[0]["title"] == "工作线甲"

    def test_missing_overview_and_ooda_retries_then_pads_from_strand(self, theme_db):
        """4B 返回无 overview 且无 ooda → 重试一次 → strand ooda 拼接兜底。"""
        hollow = json.dumps({
            "title": "主题甲", "overview": "",
            "key_facts": [], "open_items": [], "timeline_overview": "",
        })
        llm = self._CreateFake([hollow, hollow])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1
        assert len(llm.calls) == 2   # 触发重试

        from ca.store import load_all_themes
        t = load_all_themes(db_path=theme_db)[0]
        assert t["overview"] != ""             # 拼接自 strand ooda（决策与方案）
        assert "工作线甲决策" in t["overview"]

    def test_create_writes_initial_changes(self, theme_db):
        """create 路径把 strand changes 写入 theme.changes_json（全量去重 changes 初始态）。"""
        llm = self._CreateFake([json.dumps({
            "title": "主题甲", "overview": "概述",
            "ooda": {"决策与方案": ["决策A"]},
            "key_facts": [], "open_items": [], "timeline_overview": "进展",
        })])
        stats = theme_mod.run_theme_merge(
            [self._strand()], [], embed_client=None,
            llm_call=llm, db_path=theme_db, profile="tester")
        assert stats["created"] == 1

        from ca.store import load_all_themes
        t = load_all_themes(db_path=theme_db)[0]
        assert t["changes"] == ["工作线甲决策"]   # strand changes 初始化


class TestParseThemeResponse:
    """theme 生成 JSON 宽松解析。"""

    def test_parse_normal(self):
        text = '{"title": "主题", "overview": "概述", "ooda": {}}'
        r = theme_mod.parse_theme_response(text)
        assert r["title"] == "主题"

    def test_parse_invalid_returns_none(self):
        assert theme_mod.parse_theme_response("not json") is None
        assert theme_mod.parse_theme_response("") is None
        assert theme_mod.parse_theme_response(None) is None

    def test_parse_truncated_json_recovered(self):
        """截断 JSON（缺右括号）→ 宽松修复。"""
        text = '{"title": "主题", "overview": "概述"'
        r = theme_mod.parse_theme_response(text)
        assert r is not None
        assert r.get("title") == "主题"


class TestSCandidateMerge:
    """v7 (决策 38): S 匹配分候选主路径（注入集优先）。"""

    def test_priority_theme_forced_candidate(self, theme_db):
        """注入集 theme 强制进候选（S=0，即使无共现边、词面无关）→ 4B merge 归入。"""
        from ca.store import create_theme, load_all_themes

        tid = create_theme(
            profile="tester", title="注入主题甲", overview="注入概述", ooda={},
            key_facts=[], open_items=[],
            timeline_entry={"topic_id": 1, "turns": [1],
                            "session_id": "s1", "overview": "注入概述"},
            source_strand={"session_id": "s0", "strand_id": 1},
            centroid_json=None, db_path=theme_db)
        themes = load_all_themes(db_path=theme_db)
        assert len(themes) == 1

        # strand 词面与注入主题完全不同（无共现边、无余弦近似）
        strand = _strand("完全无关的全新工作线", topic_id=2, sid="s1", strand_id=5)
        llm = FakeLLMCall(assignments={"strand_1": {"action": "merge", "target": 0}})
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester",
            priority_themes=[themes[0]])  # 注入集 I

        assert stats["merged"] == 1
        # 候选来自注入集（S=0 强制），prompt 带 [注入主题] 标注与 S 值
        decide_prompt = llm.calls[0]
        assert "注入主题甲" in decide_prompt
        assert "[注入主题]" in decide_prompt
        assert "S=" in decide_prompt

    def test_s_candidates_respect_new_decision(self, theme_db):
        """注入 theme 在候选（S=0）但 4B 判定不承接 → new（宁分不并）。"""
        from ca.store import create_theme, load_all_themes

        create_theme(
            profile="tester", title="注入主题乙", overview="注入概述", ooda={},
            key_facts=[], open_items=[],
            timeline_entry={"topic_id": 1, "turns": [1],
                            "session_id": "s1", "overview": "注入概述"},
            source_strand={"session_id": "s0", "strand_id": 1},
            centroid_json=None, db_path=theme_db)
        themes = load_all_themes(db_path=theme_db)

        strand = _strand("全新独立工作线", topic_id=2, sid="s1", strand_id=6)
        llm = FakeLLMCall(assignments={"strand_1": {"action": "new"}})
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester",
            priority_themes=[themes[0]])

        assert stats["created"] == 1
        assert stats["merged"] == 0
        assert len(load_all_themes(db_path=theme_db)) == 2

    def test_no_priority_fallback_cosine(self, theme_db):
        """无注入集（全新话题空注入）→ 冷启动退化余弦（v6.5.3 行为保留）。"""
        from ca.store import create_theme, load_all_themes

        create_theme(
            profile="tester", title="历史主题", overview="概述", ooda={},
            key_facts=[], open_items=[],
            timeline_entry={"topic_id": 1, "turns": [1],
                            "session_id": "s0", "overview": "概述"},
            source_strand={"session_id": "s0", "strand_id": 1},
            centroid_json=json.dumps(_fake_embed("历史主题")),
            db_path=theme_db)
        themes = load_all_themes(db_path=theme_db)

        # 无 priority_themes → 走余弦候选（embed 命中历史主题）
        strand = _strand("历史主题", topic_id=2, sid="s1", strand_id=7)
        llm = FakeLLMCall(assignments={"strand_1": {"action": "merge", "target": 0}})
        stats = theme_mod.run_theme_merge(
            [strand], themes, embed_client=FakeEmbedClient(),
            llm_call=llm, db_path=theme_db, profile="tester")

        assert stats["merged"] == 1
        # 余弦路径候选无 [注入主题] 标注（候选行格式 "[0] 历史主题"，无标注后缀）
        assert "[0] 历史主题" in llm.calls[0]
        assert "[0] 历史主题 [注入主题]" not in llm.calls[0]
