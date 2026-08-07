"""reality 生成 prompt 构建 — ca/reality.py（决策 37 reality 重构）。

设计决策对照（37-reality-restructure.md）:
  → §4.1 reality 数据模型：name(固定) / hdl(状态锚点,可改) /
    current_status{current_state, key_facts, goals, context} / timeline
  → §4.3 状态结构对齐 OV L1：Current State→current_state,
    Task & Goals→goals, Key Facts & Decisions→key_facts,
    Files & Context→context, Abstract(L0)→hdl
  → §5.1 strand→reality 衔接判定：strand「现象与问题」是否承接
    reality.current_status.goals？→ merge/new（宁分不并显式）
  → §9-9 待定项落实：不确定 → 新 reality（宁分不并显式规则入 prompt）
  → OV update 语义借鉴（ov_wm_v2_update）：current_state=UPDATE 快照重写、
    key_facts=APPEND、goals=KEEP 除非变化、context=APPEND 相关性过滤

覆盖:
  - create prompt: OV L1 五段映射字段 / 中文 / 禁代码符号 / 预算
  - merge prompt: 已有 reality + 新 strands / UPDATE/APPEND/KEEP 语义 / hdl 重写
  - decide prompt: goals 承接判定锚 / 宁分不并显式 / merge+new 选项
  - 解析: 正常 JSON / 宽松解析（截断修复）
"""

import sys
from pathlib import Path

# ca/ 可导入（conftest 已加 sys.path；此处独立兜底）
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
            "决策与方案": ["压缩仅用于 LLM 输入"],
            "后续行动": ["验证 in_place 模式一致性"],
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


class TestBuildCreateRealityPrompt:
    """create 式 prompt：新 strand → reality 初始状态（OV L1 五段映射）。"""

    def test_contains_strand_hdl(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "压缩与持久化职责边界厘清" in prompt

    def test_contains_strand_ooda_content(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "压缩仅用于 LLM 输入" in prompt

    def test_multiple_strands_all_present(self):
        strands = [_strand(hdl="strand甲"), _strand(hdl="strand乙")]
        prompt = reality_mod.build_create_reality_prompt(strands, max_chars=4000)
        assert "strand甲" in prompt and "strand乙" in prompt

    def test_chinese_requirement(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "中文" in prompt

    def test_no_code_symbol_rule(self):
        """hdl/name 命名规范：禁止代码符号名/英文标识符。"""
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "禁止" in prompt
        assert "符号" in prompt or "标识符" in prompt

    def test_budget_injected(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4321)
        assert "4321" in prompt

    def test_json_output_instruction(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "JSON" in prompt

    def test_ov_l1_field_mapping_in_create(self):
        """§4.3：OV L1 五段映射字段齐全（name/hdl/current_state/key_facts/goals/context）。"""
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "name" in prompt
        assert "hdl" in prompt
        assert "current_status" in prompt
        assert "current_state" in prompt
        assert "key_facts" in prompt
        assert "goals" in prompt
        assert "context" in prompt

    def test_current_state_snapshot_rule(self):
        """OV Current State 语义：快照（2-5 条），非事实堆砌。"""
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "快照" in prompt or "2-5" in prompt

    def test_key_facts_with_date_rule(self):
        """OV Key Facts 语义：持久事实带日期。"""
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "日期" in prompt or "date" in prompt.lower()


class TestBuildMergeRealityPrompt:
    """merge 式 prompt：已有 reality + 新 strand → 融合更新（OV update 语义）。"""

    def test_contains_existing_reality(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "压缩与持久化职责边界厘清" in prompt    # name
        assert "双管道解耦实现中" in prompt            # hdl
        assert "验证in_place模式一致性" in prompt      # goals

    def test_contains_new_strands(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand(hdl="新增工作线")], max_chars=4000)
        assert "新增工作线" in prompt

    def test_merge_decision_field(self):
        """归并决策字段：4B 可否决（merge:false → 转 create）。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "merge" in prompt

    def test_hdl_rewrite_rule(self):
        """hdl 每次归并重写（当前状态锚点），旧 hdl 由系统入 timeline。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "hdl" in prompt
        assert "重写" in prompt or "锚点" in prompt

    def test_current_state_update_semantics(self):
        """OV update：current_state 默认 UPDATE（快照重写），未解决事项必须保留。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "UPDATE" in prompt or "更新" in prompt
        assert "未解决" in prompt or "保留" in prompt

    def test_key_facts_append_semantics(self):
        """OV update：key_facts 默认 APPEND（新增持久事实，已有不重复）。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "APPEND" in prompt or "追加" in prompt
        assert "重复" in prompt

    def test_goals_keep_semantics(self):
        """OV update：goals 默认 KEEP（原样保留），变化才增删，显式标记完成。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "KEEP" in prompt or "保留" in prompt
        assert "原样保留" in prompt
        assert "完成" in prompt

    def test_context_append_semantics(self):
        """OV update：context 默认 APPEND（新资源加入，相关性过滤）。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "APPEND" in prompt or "追加" in prompt
        assert "相关" in prompt

    def test_chinese_and_budget_in_merge(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=3999)
        assert "中文" in prompt
        assert "3999" in prompt

    def test_json_output_instruction_in_merge(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "JSON" in prompt
        assert "current_status" in prompt

    def test_context_path_only_rule(self):
        """B2（2026-08-03 审计）：context 必须是路径/URL，禁止函数名/描述文字。"""
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000)
        assert "路径" in prompt or "URL" in prompt
        assert "函数" in prompt or "描述" in prompt


class TestBuildDecidePromptMembers:
    """A1（2026-08-03 审计）：decide 候选展示成员 hdl——家族一致性判定。"""

    def _candidate(self, name="候选甲", goals=None, members=None) -> dict:
        c = {
            "reality_id": 1,
            "name": name,
            "hdl": "候选当前状态",
            "current_status": {"goals": goals if goals is not None else ["目标甲"]},
        }
        if members:
            c["member_hdls"] = members
        return c

    def test_candidate_members_shown(self):
        """候选带 member_hdls 时展示成员工作线（家族一致性判定锚）。"""
        prompt = reality_mod.build_reality_decide_prompt(
            [{"strand": _strand(hdl="GGUF模型加载"),
              "candidates": [self._candidate(
                  members=["GGUF模型路径扫描机制优化",
                           "文件结构与模型路径优化"])]}])
        assert "GGUF模型路径扫描机制优化" in prompt

    def test_candidate_without_members_ok(self):
        """无 member_hdls 的候选不报错（兼容原形态）。"""
        prompt = reality_mod.build_reality_decide_prompt(
            [{"strand": _strand(hdl="GGUF模型加载"),
              "candidates": [self._candidate()]}])
        assert "候选甲" in prompt

    def test_family_consistency_rule(self):
        """decide prompt 含家族一致性规则：成员与 strand 同族优先。"""
        prompt = reality_mod.build_reality_decide_prompt(
            [{"strand": _strand(hdl="GGUF模型加载"),
              "candidates": [self._candidate(
                  members=["GGUF模型路径扫描机制优化"])]}])
        assert "成员" in prompt
        assert "同族" in prompt or "一致" in prompt or "家族" in prompt


class TestTimelineAppend:
    """A2（2026-08-03 审计）：timeline 追加防重（fallback 路径 hdl 未重写导致重复）。"""

    def test_append_dedupe(self):
        """重复 hdl 不追加（fallback 路径 old_hdl 未变）。"""
        tl = ["已识别5.3 GB孤儿数据"]
        result = reality_mod.append_timeline_hdl(tl, "已识别5.3 GB孤儿数据")
        assert result == ["已识别5.3 GB孤儿数据"]  # 未追加

    def test_append_new(self):
        """R-4（决策 42）：追加结构化为 {"hdl", "ts"}；存量 str 条目保留（2026-08-08 设计变更跟随）。"""
        tl = ["旧hdl"]
        result = reality_mod.append_timeline_hdl(tl, "新hdl")
        assert result[0] == "旧hdl"  # 存量 str 条目保留
        assert len(result) == 2
        assert result[1]["hdl"] == "新hdl"
        assert isinstance(result[1]["ts"], float)

    def test_append_empty(self):
        """空 hdl 不追加。"""
        result = reality_mod.append_timeline_hdl([], "")
        assert result == []


class TestCreatePromptNameRule:
    """B1（2026-08-03 审计）：create 时 name 必须是固定事物名，≠ hdl 状态。"""

    def test_name_not_equal_hdl_rule(self):
        prompt = reality_mod.build_create_reality_prompt(
            [_strand()], max_chars=4000)
        assert "name" in prompt
        assert "hdl" in prompt
        # name=固定事物名（不变）；hdl=当前状态锚点（可改）——语义区分显式化
        assert "固定" in prompt
        assert "不变" in prompt or "可改" in prompt


class TestForceConsolidation:
    """N2 方向 A（2026-08-03）：超限强制压缩重试——拒绝→强化重试→代码截断兜底。"""

    def test_find_oversized_sections(self):
        """超限检测：返回超限段 {section: count}。"""
        cs = {
            "current_state": ["a"] * 6,      # >5 超限
            "key_facts": ["b"] * 4,          # ≤5 正常
            "goals": ["c"] * 3,              # ≤8 正常
            "context": ["d"] * 10,           # >8 超限
        }
        over = reality_mod.find_oversized_sections(cs)
        assert over == {"current_state": 6, "context": 10}

    def test_find_oversized_empty_when_ok(self):
        cs = {"current_state": ["a"] * 5, "key_facts": ["b"] * 5,
              "goals": ["c"] * 8, "context": ["d"] * 8}
        assert reality_mod.find_oversized_sections(cs) == {}

    def test_build_force_note_mentions_section_and_limit(self):
        note = reality_mod.build_force_note({"key_facts": 7})
        assert "key_facts" in note
        assert "7" in note
        assert "5" in note
        assert "合并" in note

    def test_build_force_note_multiple_sections(self):
        note = reality_mod.build_force_note({"key_facts": 7, "context": 10})
        assert "key_facts" in note and "context" in note

    def test_force_note_injected_into_merge_prompt(self):
        """强化指令注入 merge prompt（重试时带）。"""
        r = _reality()
        r["current_status"] = {"key_facts": [f"2026-07-0{i}: 事实{i}" for i in range(7)]}
        prompt = reality_mod.build_merge_reality_prompt(
            r, [_strand()], max_chars=4000,
            force_note=reality_mod.build_force_note({"key_facts": 7}))
        assert "上次输出" in prompt or "超限" in prompt

    def test_no_force_note_when_empty(self):
        prompt = reality_mod.build_merge_reality_prompt(
            _reality(), [_strand()], max_chars=4000, force_note="")
        assert "上次输出" not in prompt

    def test_truncate_by_anchor_priority(self):
        """代码兜底：保留锚点最丰富的（带日期/路径/数值），丢弃纯描述。"""
        items = [
            "2026-07-30: 确认压缩仅用于LLM输入",     # 锚点: 日期+LLM
            "做了一些优化工作",                       # 锚点: 0（纯中文）
            "修改 plugins/ca_assembler/ca/theme.py",  # 锚点: 路径（最丰富）
            "2026-08-01: 完成双管道解耦",             # 锚点: 日期
        ]
        kept = reality_mod.truncate_by_anchor_priority(items, limit=2)
        assert len(kept) == 2
        # 锚点最多的优先：路径条目(4锚点) > 日期条目(1锚点)
        assert "theme.py" in kept[0]
        # 纯中文描述被丢弃
        assert "做了一些优化工作" not in kept

    def test_truncate_keeps_all_when_within_limit(self):
        items = ["2026-07-30: a", "2026-07-31: b"]
        assert reality_mod.truncate_by_anchor_priority(items, limit=5) == items

    def test_truncate_empty(self):
        assert reality_mod.truncate_by_anchor_priority([], limit=5) == []

    def test_enforce_section_limits_truncates_all_oversized(self):
        """N2 兜底：所有超限段强制截断到限内。"""
        cs = {
            "current_state": ["a"] * 6,
            "key_facts": ["2026-07-30: 事实1", "b"] * 4,   # 8 条 → 截 5
            "goals": ["c"] * 3,
            "context": ["d"] * 10,
        }
        reality_mod.enforce_section_limits(cs)
        assert len(cs["current_state"]) <= 5
        assert len(cs["key_facts"]) <= 5
        assert len(cs["context"]) <= 8
        # 锚点优先：带日期的 key_facts 保留
        assert any("2026-07-30" in str(x) for x in cs["key_facts"])

    def test_enforce_section_limits_noop_when_within(self):
        cs = {"current_state": ["a"], "key_facts": ["b"], "goals": ["c"], "context": ["d"]}
        before = {k: list(v) for k, v in cs.items()}
        reality_mod.enforce_section_limits(cs)
        assert cs == before


class TestBuildRealityDecidePrompt:
    """衔接判定 prompt：strand「现象与问题」承接 goals → merge/new。"""

    def _candidate(self, name: str = "压缩与持久化职责边界厘清",
                   goals: list | None = None) -> dict:
        return {
            "reality_id": 1,
            "name": name,
            "hdl": "已明确压缩不写state.db",
            "current_status": {
                "goals": goals if goals is not None else ["验证in_place模式一致性"],
            },
        }

    def _strand_with_cand(self, hdl: str = "压缩职责厘清", candidates: list | None = None) -> dict:
        st = _strand(hdl=hdl)
        if candidates is not None:
            st["candidates"] = candidates
        return st

    def test_contains_strand_hdl(self):
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", [self._candidate()])])
        assert "压缩职责厘清" in prompt

    def test_contains_candidate_name_and_goals(self):
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", [self._candidate()])])
        assert "压缩与持久化职责边界厘清" in prompt    # candidate name
        assert "验证in_place模式一致性" in prompt      # candidate goals

    def test_goals_anchor_rule(self):
        """§5.1 判定锚：strand「现象与问题」是否承接候选 reality 的 goals。"""
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", [self._candidate()])])
        assert "现象与问题" in prompt
        assert "goals" in prompt or "目标" in prompt
        assert "承接" in prompt or "推进" in prompt

    def test_merge_and_new_options(self):
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", [self._candidate()])])
        assert "merge" in prompt
        assert "new" in prompt

    def test_dont_merge_explicit_rule(self):
        """§9-9：宁分不并显式——不确定 → new（保守优于错误归并）。"""
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", [self._candidate()])])
        assert "不确定" in prompt
        assert "new" in prompt

    def test_multiple_candidates_indexed(self):
        cands = [self._candidate(name="候选甲"), self._candidate(name="候选乙")]
        prompt = reality_mod.build_reality_decide_prompt(
            [self._strand_with_cand("压缩职责厘清", cands)])
        assert "候选甲" in prompt and "候选乙" in prompt

    def test_filter_no_candidate_strands(self):
        """无候选 strand 不进入决策（单向否决，直接 new）。"""
        with_cand = [self._strand_with_cand("有候选", [self._candidate()])]
        no_cand = [self._strand_with_cand("无候选strand甲", None)]
        prompt = reality_mod.build_reality_decide_prompt(with_cand + no_cand)
        assert "有候选" in prompt
        assert "无候选strand甲" not in prompt

    def test_dont_merge_rule(self):
        """宁分不并：merge 但 target 缺失 → 降级 new（解析层兜底）。"""
        assigns = reality_mod.parse_reality_assignments(
            '{"assignments": {"strand_1": {"action": "merge"}}}')
        assert assigns.get("strand_1") == {"action": "new"}


class TestParseRealityResponse:
    """宽松解析 reality 生成/融合 JSON。"""

    def test_parse_normal_json(self):
        raw = '{"name": "甲", "hdl": "乙", "current_status": {"goals": ["丙"]}}'
        result = reality_mod.parse_reality_response(raw)
        assert result is not None
        assert result["name"] == "甲"
        assert result["current_status"]["goals"] == ["丙"]

    def test_parse_malformed_json_lenient(self):
        """截断（缺右括号）→ 宽松解析修复（复用 topic_summary._lenient_json_parse）。"""
        raw = '{"name": "甲", "hdl": "乙"'
        result = reality_mod.parse_reality_response(raw)
        assert result is not None
        assert result["name"] == "甲"

    def test_parse_empty_returns_none(self):
        assert reality_mod.parse_reality_response("") is None
        assert reality_mod.parse_reality_response(None) is None

    def test_parse_non_dict_returns_none(self):
        assert reality_mod.parse_reality_response("[1, 2, 3]") is None

    def test_parse_strips_hdl_field_prefix(self):
        """Q1（2026-08-03 实验）：4B 把 prompt 字段说明抄进 hdl 值 → 剥离。"""
        raw = ('{"name": "甲", "hdl": "重写后的当前状态锚点：双管道解耦实现中",'
               ' "current_status": {"goals": ["目标甲"]}}')
        result = reality_mod.parse_reality_response(raw)
        assert result is not None
        assert result["hdl"] == "双管道解耦实现中"

    def test_parse_strips_hdl_prefix_variants(self):
        """前缀变体：全角/半角冒号、无"重写"版本。"""
        for raw_hdl in ["重写后的当前状态锚点：A", "重写后的当前状态锚点:B",
                        "当前状态锚点：C", "当前状态锚点:D"]:
            raw = '{"hdl": "%s"}' % raw_hdl
            result = reality_mod.parse_reality_response(raw)
            assert result["hdl"] == raw_hdl.split("：")[-1].split(":")[-1].strip()

    def test_parse_does_not_strip_body_text(self):
        """正文含"锚点"字样不误删（清洗保守，仅剥已知前缀）。"""
        raw = '{"hdl": "锚点迁移方案已确认"}'
        result = reality_mod.parse_reality_response(raw)
        assert result["hdl"] == "锚点迁移方案已确认"

    def test_parse_strips_prefix_in_current_status_items(self):
        """current_status 条目带前缀同样剥离。"""
        raw = ('{"name": "甲", "hdl": "乙",'
               ' "current_status": {"goals": ["重写后的当前状态锚点：目标丙"]}}')
        result = reality_mod.parse_reality_response(raw)
        assert result["current_status"]["goals"] == ["目标丙"]

    def test_parse_strips_prefix_after_lenient_fix(self):
        """宽松解析路径（缺右括号修复后）同样走清洗。"""
        raw = '{"hdl": "重写后的当前状态锚点：双管道解耦实现中"'
        result = reality_mod.parse_reality_response(raw)
        assert result is not None
        assert result["hdl"] == "双管道解耦实现中"
