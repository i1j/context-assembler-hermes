"""reality 防膨胀守卫 — ca/reality.py（对齐 OV WM 三层防线）。

设计决策对照（2026-08-03 实验 + OV ov_wm_v2_update / session.py）:
  → OV 三层防线：prompt 软约束（默认 KEEP/UPDATE/APPEND 语义）
    + 服务端监控（_build_wm_section_reminders 注入 size warnings）
    + 代码守卫（_wm_enforce_key_facts_consolidation：Layer1 条数比 /
      Layer2 锚点覆盖率 / 超限 APPEND 节流 / salvage）
  → 实验实测（winker 46 strand）：goals 无上限膨胀到 12 条、
    key_facts 8 条超"最多 5"、context 10 条、current_state 7 条超 2-5；
    create 时 4 个 reality current_status 全空（@27/@28/@41/@45）

覆盖:
  - merge prompt 四字段语义（state=删已解决 / facts=按主题合并 /
    goals=默认 KEEP+显式完成 / context=过滤去重）
  - build_size_warnings：超限注入（state>5 / facts>5 / goals>8 / ctx>8）
  - 锚点提取（中文适配：日期/数字+单位/路径/ASCII 专名）
  - 代码守卫：trivial 缩小拒绝（<50% 条数比）/ 锚点覆盖 <70% 拒绝 /
    拒绝后 salvage 新条目
  - create 兜底：current_status 全空 → 从 strand ooda 填充
"""

import sys
from pathlib import Path

_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.reality as reality_mod


def _strand(**overrides) -> dict:
    strand = {
        "hdl": "压缩与持久化职责边界厘清",
        "turns": [7, 8, 9],
        "ooda": {
            "现象与问题": ["压缩结果被误写进 state.db"],
            "背景与约束": ["双管道解耦实现中"],
            "决策与方案": ["压缩仅用于 LLM 输入", "采用原子替换保证一致性"],
            "后续行动": ["验证 in_place 模式一致性", "消除双写"],
        },
        "changes": ["确认压缩不写 state.db"],
    }
    strand.update(overrides)
    return strand


def _reality(**overrides) -> dict:
    reality = {
        "reality_id": 1,
        "name": "压缩与持久化职责边界厘清",
        "hdl": "已明确压缩不写state.db，双管道解耦实现中",
        "current_status": {
            "current_state": ["压缩结果已明确不写state.db", "双管道解耦实现中"],
            "key_facts": ["2026-07-30: 确认压缩仅用于LLM输入"],
            "goals": ["验证in_place模式一致性", "消除双写"],
            "context": ["plugins/ca_assembler/ca/theme.py"],
        },
        "timeline": ["决策做A功能"],
    }
    reality.update(overrides)
    return reality


def _oversized_reality(**overrides) -> dict:
    """goals 9 条 / key_facts 7 条 / context 9 条 / state 6 条（全超限）。"""
    r = _reality()
    r["current_status"] = {
        "current_state": [f"state 状态 {i}" for i in range(6)],
        "key_facts": [f"2026-07-2{i}: 事实 {i}" for i in range(7)],
        "goals": [f"目标 {i}" for i in range(9)],
        "context": [f"/path/file{i}.py" for i in range(9)],
    }
    r.update(overrides)
    return r


class TestMergePromptFieldSemantics:
    """merge prompt 四字段语义（对齐 OV 默认 op 语义）。"""

    def test_state_must_drop_resolved(self):
        """current_state：UPDATE 语义——已解决条目必须删除，只留未解决+新进展。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "已解决" in prompt
        assert "删除" in prompt or "移除" in prompt

    def test_state_snapshot_limit(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "2-5" in prompt

    def test_facts_consolidate_by_topic(self):
        """key_facts：APPEND+按主题合并（保留日期/数值锚点）。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "合并" in prompt or "归类" in prompt
        assert "最多 5 条" in prompt

    def test_goals_default_keep(self):
        """goals：默认 KEEP——原样保留，变化才增删，必须显式标记完成。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "KEEP" in prompt or "保留" in prompt
        assert "完成" in prompt
        assert "新增" in prompt

    def test_context_filter_and_dedup(self):
        """context：相关性过滤 + 去重——只保留路径/URL，重复合并。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "只保留" in prompt or "相关" in prompt
        assert "重复" in prompt or "去重" in prompt


class TestBuildSizeWarnings:
    """服务端监控：超限注入 size warnings（对齐 _build_wm_section_reminders）。"""

    def test_empty_when_within_limits(self):
        r = _reality()  # state2/facts1/goals2/ctx1 全在限内
        warnings = reality_mod.build_size_warnings(r)
        assert warnings == ""

    def test_goals_oversized_warning(self):
        r = _oversized_reality()
        w = reality_mod.build_size_warnings(r)
        assert "goals" in w or "目标" in w
        assert "9" in w

    def test_all_sections_oversized(self):
        r = _oversized_reality()
        w = reality_mod.build_size_warnings(r)
        for key in ("current_state", "key_facts", "goals", "context"):
            assert key in w

    def test_injected_into_merge_prompt(self):
        r = _oversized_reality()
        prompt = reality_mod.build_merge_reality_prompt(r, [_strand()], max_chars=4000)
        assert "超限" in prompt or "合并" in prompt
        assert "goals" in prompt

    def test_no_warning_when_within_limits_in_prompt(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "<section_size_warnings>" not in prompt


class TestExtractAnchors:
    """锚点提取（中文适配：日期/数字+单位/路径/ASCII 专名）。"""

    def test_dates(self):
        anchors = reality_mod.extract_lexical_anchors("2026-07-30: 确认压缩仅用于LLM输入")
        assert "2026-07-30" in anchors

    def test_numbers_with_units(self):
        anchors = reality_mod.extract_lexical_anchors("清理释放 2.9GB 空间，耗时 20 分钟")
        assert "2.9gb" in anchors or "2.9gb" in {a.lower() for a in anchors}

    def test_paths(self):
        anchors = reality_mod.extract_lexical_anchors(
            "修改 plugins/ca_assembler/ca/theme.py 与 E:\\AI\\comfyUI\\config.json")
        assert "theme.py" in anchors or "plugins/ca_assembler/ca/theme.py" in anchors

    def test_ascii_proper_nouns(self):
        anchors = reality_mod.extract_lexical_anchors(
            "krea2MuseByStable_v15TurboFp8 模型与 Bambushu 工作流")
        assert "krea2musebystable_v15turbofp8" in anchors

    def test_chinese_content_ignored_as_anchor(self):
        """中文内容不作为锚点（锚点 = 可验证的事实载体）。"""
        anchors = reality_mod.extract_lexical_anchors("已确认压缩仅用于LLM输入")
        assert "已确认压缩仅用于llm输入" not in anchors


class TestMergeGuard:
    """代码守卫（对齐 _wm_enforce_key_facts_consolidation，中文适配）。"""

    def test_trivial_shrink_rejected(self):
        """Layer 1：合并后条数 < 旧 50% → 拒绝（trivial 缩小）。"""
        old = {"goals": [f"目标 {i}" for i in range(8)]}
        new = {"goals": [f"目标 {i}" for i in range(3)]}  # 3/8 = 37.5% < 50%
        verdict = reality_mod.guard_merge_consolidation(old, new)
        assert verdict["approved"] is False
        assert verdict["reason"] == "trivial_shrink"

    def test_anchor_coverage_low_rejected(self):
        """Layer 2：锚点覆盖 < 70% → 拒绝。"""
        old = {"key_facts": [
            "2026-07-30: 确认压缩仅用于LLM输入",
            "2026-07-31: 采用原子替换保证一致性",
        ]}
        new = {"key_facts": [
            "2026-07-30: 确认压缩仅用于LLM输入",
            "新增了完全不同的另一条事实",
        ]}  # 1/2 锚点 → 50% < 70%
        verdict = reality_mod.guard_merge_consolidation(old, new)
        assert verdict["approved"] is False
        assert verdict["reason"] == "low_anchor_coverage"

    def test_good_consolidation_accepted(self):
        """合并合理：条数比 ≥50% 且锚点覆盖 ≥70% → 接受。"""
        old = {"goals": [
            "验证in_place模式一致性",
            "消除双写",
            "增加监控告警",
            "优化缓存策略",
        ]}
        new = {"goals": [
            "验证in_place模式一致性",
            "消除双写并增加监控告警",
            "优化缓存策略",
        ]}  # 3/4 = 75% 条数；锚点（数字/ASCII）应覆盖大部分
        verdict = reality_mod.guard_merge_consolidation(old, new)
        assert verdict["approved"] is True

    def test_salvage_new_items(self):
        """拒绝后 salvage：从被拒内容提取真新增条目（去重后 APPEND）。"""
        old = {"goals": ["目标 A", "目标 B"]}
        new = {"goals": ["目标 A", "目标 B", "全新目标 C", "目标 D"]}
        # 纯中文无锚点 → 守卫保守放行（approved=True）；salvage 仍是真新增
        salvaged = reality_mod.salvage_new_items(old, new, "goals")
        assert salvaged["op"] == "APPEND"
        assert "全新目标 C" in salvaged["items"]
        assert "目标 D" in salvaged["items"]
        assert "目标 A" not in salvaged["items"]  # 旧条目不进 salvage

    def test_salvage_none_when_no_fresh(self):
        """无真新增 → KEEP。"""
        old = {"goals": ["目标 A"]}
        new = {"goals": ["目标 A"]}
        salvaged = reality_mod.salvage_new_items(old, new, "goals")
        assert salvaged == {"op": "KEEP"}

    def test_oversized_append_throttled(self):
        """超限 APPEND 节流：只收真新增，上限 5 条。"""
        old = {"goals": [f"目标 {i}" for i in range(9)]}  # 超 8 限
        new_items = [f"新目标 {i}" for i in range(7)]
        accepted = reality_mod.throttle_append(old["goals"], new_items)
        assert len(accepted) <= 5


class TestCreateFallback:
    """create 兜底：current_status 全空 → 从 strand ooda 填充（@27/@28/@41/@45 修复）。"""

    def test_fill_empty_current_status_from_strand(self):
        st = _strand()
        parsed = {"name": "压缩职责厘清", "hdl": "压缩不写state.db"}
        filled = reality_mod.fill_current_status_fallback(st, parsed)
        cs = filled["current_status"]
        # current_state ← 决策与方案；goals ← 后续行动；key_facts ← changes 派生
        assert cs["current_state"]  # 非空
        assert "压缩仅用于 LLM 输入" in cs["current_state"]
        assert "验证 in_place 模式一致性" in cs["goals"]
        assert cs["key_facts"]

    def test_fill_empty_dict_structure(self):
        """current_status 键存在但四段全空（空结构 dict，truthy）→ 同样触发兜底。"""
        st = _strand()
        parsed = {"name": "压缩职责厘清", "hdl": "压缩不写state.db",
                  "current_status": {"current_state": [], "key_facts": [],
                                     "goals": [], "context": []}}
        filled = reality_mod.fill_current_status_fallback(st, parsed)
        cs = filled["current_status"]
        assert cs["current_state"]  # 已从 ooda 填充
        assert cs["goals"]

    def test_keep_partial_current_status(self):
        """部分字段有值 → 不覆盖已有字段，只填空缺。"""
        st = _strand()
        parsed = {
            "name": "压缩职责厘清", "hdl": "压缩不写state.db",
            "current_status": {"goals": ["已有目标"], "context": []},
        }
        filled = reality_mod.fill_current_status_fallback(st, parsed)
        cs = filled["current_status"]
        assert cs["goals"] == ["已有目标"]       # 不覆盖
        assert cs["current_state"]               # 只填空缺

    def test_no_fallback_when_complete(self):
        st = _strand()
        parsed = {
            "name": "压缩职责厘清", "hdl": "压缩不写state.db",
            "current_status": {
                "current_state": ["a"], "key_facts": ["b"],
                "goals": ["c"], "context": ["d"],
            },
        }
        filled = reality_mod.fill_current_status_fallback(st, parsed)
        assert filled["current_status"]["current_state"] == ["a"]  # 原样保留
