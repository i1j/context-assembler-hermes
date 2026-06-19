# 📋 CA Topic 测试体系 — OpenViking Wiki 设计需求交叉验证 (2026-06-19)
#
# 验证新增测试是否全面覆盖了 wiki 中所有 topic 相关设计决策。
#
# ============================================================================
# TP-002: 三级话题定级 (topic 半径)
# ============================================================================
# 来源: decision-points-wiki.md TP-002 + design/decision-points/TP-002.md
#
# 设计决策: _grade_topics_by_radius():
#   r = min(max_intra, nearest / WEIGHT)
#   d <= r/2 -> TopicGrade.ACT (内球)
#   d <= r -> TopicGrade.REL (外球)
#   检索命中 -> TopicGrade.REL
#   其余 -> TopicGrade.FAR
#   single-topic max_intra=0.0; min radius=0.05; WEIGHT=2.0
#
# 覆盖情况:
#   - test_inner_sphere_act              -> d <= r/2 -> ACT     ✅
#   - test_outer_sphere_rel              -> d <= r -> REL       ✅
#   - test_far_but_retrieved_becomes_rel -> 检索命中 -> REL     ✅
#   - test_far_not_retrieved             -> 其余 -> FAR         ✅
#   - test_centroid_none_fallback_rel    -> centroid=None->REL  ✅
#   - test_min_radius_protection         -> r=0 -> 0.05保护     ✅
#   - test_single_topic_no_nearest       -> 单话题 max_intra     ✅
#   - test_nearest_centroid_limits_radius -> WEIGHT=2.0         ✅
#   - test_bg_topic_uses_bg_level        -> is_bg -> Far        ✅
#   - test_no_query_embed_defaults_rel   -> q_emb=None->REL     ✅
#
# 结论: TP-002 全部 4 条路径 + 3 个边界条件全部覆盖 ✅
#
# ============================================================================
# TP-001: 话题分割 (Jaccard 链合并)
# ============================================================================
# 来源: decision-points-wiki.md TP-001
# 设计决策: 强制分割短语触发新话题
#   Jaccard: >=ENTRY(0.02) 弱匹配延续, >=CHAIN(0.04) 强匹配延续, <ENTRY 新话题
#
# 覆盖情况:
#   - test_jaccard_chain_continues         -> ENTRY 延续 ✅
#   - test_jaccard_entry_continues         -> CHAIN 延续 ✅
#   - test_jaccard_miss_new_topic          -> <ENTRY 新话题 ✅
#   - test_forced_split_new_topic          -> 强制短语新话题 ✅
#   - test_forced_split_returns_switch     -> detect True ✅
#   - _scan_forced_split_phrases: 14 个测试覆盖中英文短语 ✅
#   - _jaccard_text: 10 个测试覆盖 CJK/英文/空/混合 ✅
#
# 结论: TP-001 全部 Jaccard 逻辑 + 强制分割全部覆盖 ✅
#
# 注意: TP-001 旧文档阈值 0.03/0.04, v5 代码改为 0.02/0.04
#       (ENTRY=0.02, CHAIN=0.04) — 未在 wiki 文档同步
#
# ============================================================================
# TP-003: TopicRetriever (per-topic 检索)
# ============================================================================
# 不在 topic_manager 测试范围（独立模块）
# 结论: 无需覆盖 ✅
#
# ============================================================================
# TP-004: topic_boost — 父 topic ACT -> 工具轮自动 FCT
# ============================================================================
# 来源: decision-points-wiki.md TP-004
# 设计: 父对话轮 topic ACT -> 该 topic 工具轮升 FCT
# 当前代码: thought_tool_map {TopicGrade.ACT: Grade.FCT}
#
# 覆盖:
#   - test_thought_replaced_with_full_fct      -> ACT thought -> Fct ✅
#   - 集成测试验证 ACT->FCT 替换管道           -> ✅
#
# 结论: TP-004 全部覆盖 ✅
#
# ============================================================================
# Grade.from_topic_grade() — 话题等级->行等级映射
# ============================================================================
# Wiki: ACT->FCT, REL->HDL, FAR->ELM(fallback)
# 已存在 test_grade.py 中:
#   - from_topic_grade(ACT) -> FCT  ✅
#   - from_topic_grade(REL) -> HDL  ✅
#   - from_topic_grade(FAR) -> ELM  ✅
#   - from_topic_grade(None)-> ELM  ✅
#
# ============================================================================
# A-stage 话题感知三级替换 (v5.10 基线 B)
# ============================================================================
# Wiki: "A-stage: 从 turn_stream 读取, 按 TopicGrade 替换"
#  thought/tool (降一级): ACT->FCT, REL->Hdl[:150], FAR->清空
#  user/fin (不降级):     ACT->Elm(原文), REL->Fct, FAR->Hdl[:150]
#
# 覆盖:
#   ACT thought -> Fct        ✅ test_thought_replaced_with_full_fct
#   REL thought/tool -> Hdl   ✅ test_thought_tool_hdl_fin_fct
#   FAR thought/tool -> 清空  ✅ test_thought_tool_cleared_fin_hdl
#   ACT fin -> Elm 原文       ✅ (同上)
#   REL fin -> Fct            ✅ test_rel_fin_replaces_original
#   FAR fin -> Hdl[:150]      ✅ (同上)
#   ACT user -> Elm           ✅ test_user_never_replaced_act
#   REL user -> Fct           ✅ test_user_replaced_with_fct_in_rel
#   FAR user -> Hdl[:150]     ✅ test_user_replaced_with_hdl_in_far
#
# 结论: 全部 9 条替换路径覆盖 ✅
#
# ============================================================================
# TopicGradeManager 全模块
# ============================================================================
# detect() — 增量话题分割 + 切换检测
#   - 6 个测试覆盖: 首轮/跳过已处理/强制分割/高Jaccard延续/低Jaccard新话题 ✅
# grade_on_switch() — 切换时定级
#   - 5 个测试覆盖: topic_data初始化/强制ACT/记录switch_turn/半径定级/冻结 ✅
# get_turn_grade() — 查询
#   - 5 个测试覆盖: turn<=0/未知turn/未知topic/缓存/切换后 ✅
# _assign_topic() — 分配
#   - 8 个测试覆盖: turn<=0/空msg/强制分割/首轮/Jaccard链/弱匹配/不匹配/Fct回退 ✅
# _compute_centroids() — 形心
#   - 7 个测试覆盖: 无store/跳过bg/正常计算/embed异常/embed None/DB异常/nearest ✅
# 其他:
#   - _extract_turn_fct 5个测试 ✅
#   - _apply_water_pressure 4个测试 ✅
#   - _init_topic_data 3个测试 ✅
#   - get_topic_grades/reset 4个测试 ✅
#   - 完整管线 3个测试 ✅
#
# ============================================================================
# 已知文档缺口 (代码 vs wiki 不一致)
# ============================================================================
# 1. 水位压力 (water pressure) — 在代码中活跃但 wiki 无记录
# 2. v5 Jaccard 阈值 (ENTRY=0.02) vs TP-001 旧值 (0.03)
# 3. user/fin 不降级映射 — wiki 只写了 thought/tool 降一级
# 4. _topic_mgr=None 降级防护 — 无 wiki 记录（bug 修复后新增）
#
# 这些是文档同步缺口，非测试缺口。测试按实际代码行为验证。
# ============================================================================
# 汇总
# ============================================================================
# 99 个单元测试 (test_topic_manager.py)
# + 13 个集成测试 (test_a_stage_topic_aware.py)
# + 17 个已有 grade.py 测试 (test_grade.py)
# = 129 个测试，覆盖 TP-001, TP-002, TP-004 的全部设计决策
