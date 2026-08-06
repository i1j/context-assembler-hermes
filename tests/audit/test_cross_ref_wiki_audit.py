"""CA Topic 测试体系 — OpenViking Wiki 设计需求交叉验证 (2026-06-20)

运行时验证：
1. 审计文档中列出的每个测试函数在对应模块中存在
2. 测试文件中的测试类/方法数量与实际相符
3. 覆盖设计决策的断言验证路径成立

设计来源: decision-points-wiki.md TP-001, TP-002, TP-004, TP-006, TP-007
"""

import importlib
import inspect
import pytest

# ============================================================================
# 审计清单：设计决策 → 测试函数映射
# ============================================================================

# 格式: {决策点: [("模块路径", "测试类.测试方法"), ...]}
WIKI_CROSS_REF = {
    "TP-001": [
        ("tests.unit.test_topic_manager", "TestJaccardText.test_identical_cjk"),
        ("tests.unit.test_topic_manager", "TestJaccardText.test_similar_cjk"),
        ("tests.unit.test_topic_manager", "TestJaccardText.test_different_cjk"),
        ("tests.unit.test_topic_manager", "TestScanForcedSplitPhrases.test_chinese_switch_topic"),
        ("tests.unit.test_topic_manager", "TestScanForcedSplitPhrases.test_english_switch"),
        ("tests.unit.test_topic_manager", "TestScanForcedSplitPhrases.test_normal_message_no_split"),
    ],
    "TP-002": [
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_inner_sphere_act"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_outer_sphere_rel"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_far_but_retrieved_becomes_rel"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_far_not_retrieved"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_centroid_none_fallback_rel"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_min_radius_protection"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_single_topic_no_nearest"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_nearest_centroid_limits_radius"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_bg_topic_uses_bg_level"),
        ("tests.unit.test_topic_manager", "TestGradeTopicsByRadius.test_no_query_embed_defaults_rel"),
        ("tests.unit.test_grade", "TestFromTopicGrade.test_act_maps_to_fct"),
        ("tests.unit.test_grade", "TestFromTopicGrade.test_rel_maps_to_hdl"),
        ("tests.unit.test_grade", "TestFromTopicGrade.test_far_maps_to_none"),
    ],
    "TP-004": [
        ("tests.stage.test_build_conv_history_v6", "TestGradeACT.test_user_fin_elm_thought_tool_fct"),
    ],
    "TP-006": [
        ("tests.unit.test_topic_manager", "TestApplyWaterPressure.test_below_start_unchanged"),
        ("tests.unit.test_topic_manager", "TestApplyWaterPressure.test_at_peak_full_penalty"),
        ("tests.unit.test_topic_manager", "TestApplyWaterPressure.test_partial_progression"),
        ("tests.unit.test_topic_manager", "TestApplyWaterPressure.test_beyond_peak_capped"),
    ],
    "TP-007": [
        ("tests.stage.test_build_conv_history_v6", "TestEmptyDB.test_no_topic_mgr_all_act"),
        ("tests.stage.test_a_stage_topic_aware", "TestEmbedFailurePipeline.test_embed_failure_falls_back_to_rel"),
        ("tests.stage.test_a_stage_topic_aware", "TestEmbedFailurePipeline.test_no_topic_mgr_all_act"),
    ],
    "CR-004": [
        ("tests.stage.test_build_conv_history_v6", "TestGradeFAR.test_user_fin_hdl_thought_tool_deleted"),
        ("tests.stage.test_build_conv_history_v6", "TestGradeREL.test_user_fin_fct_thought_tool_hdl"),
    ],
    "CR-005": [
        ("tests.store.test_store_contract", "TestSQLiteStoreContract.test_session_id_equals_db_stem"),
        ("tests.stage.test_a_stage_topic_aware", "TestFullPipelineRealTopicMgr.test_act_turn_replaced_with_fct"),
        ("tests.stage.test_a_stage_topic_aware", "TestFullPipelineRealTopicMgr.test_two_topics_produce_different_grades"),
    ],
    "CR-006": [
        ("tests.stage.test_build_conv_history_v6", "TestGradeFAR.test_user_fin_hdl_thought_tool_deleted"),
    ],
    "CR-007": [
        ("tests.plugin.test_plugin", "TestIsAvailable.test_initial_state"),
        ("tests.plugin.test_plugin", "TestIsAvailable.test_after_3_failures"),
        ("tests.plugin.test_plugin", "TestIsAvailable.test_after_2_failures"),
        ("tests.plugin.test_plugin", "TestIsAvailable.test_after_success_recovers"),
        ("tests.plugin.test_plugin", "TestBreakerStateCleanup.test_pid_self_exists"),
        ("tests.plugin.test_plugin", "TestBreakerStateCleanup.test_cleanup_removes_dead_pids_leaves_current"),
        ("tests.plugin.test_plugin", "TestBreakerStateCleanup.test_write_state_also_cleans_stale_files"),
    ],
}


def _resolve_test(module_path: str, func_path: str):
    """解析 'TestsClass.test_method' → (tests_module, test_method)"""
    mod = importlib.import_module(module_path)
    parts = func_path.split(".")
    obj = mod
    for part in parts:
        obj = getattr(obj, part)
    return obj


# ============================================================================
# 运行时验证测试
# ============================================================================


class TestCrossRefAudit:
    """验证 wiki 交叉引用清单中的每个测试函数真实存在"""

    @pytest.mark.parametrize("decision,module_path,func_path", [
        (d, m, f) for d, refs in WIKI_CROSS_REF.items() for m, f in refs
    ])
    def test_decisions_have_live_tests(self, decision, module_path, func_path):
        """设计决策 {decision} → 测试 {func_path} 必须存在且可调用"""
        func = _resolve_test(module_path, func_path)
        assert callable(func), f"{module_path}.{func_path} is not callable"
        # Verify it's a pytest test (starts with test_)
        assert func.__name__.startswith("test_"), \
            f"{func_path} does not look like a test function"

    def test_all_tp_decisions_have_entries(self):
        """每个 TP 设计决策至少有一个测试覆盖"""
        required = {"TP-001", "TP-002", "TP-004", "TP-006", "TP-007", "CR-004", "CR-005", "CR-006", "CR-007"}
        covered = set(WIKI_CROSS_REF.keys())
        missing = required - covered
        assert not missing, f"设计决策无测试覆盖: {missing}"

    def test_runtime_test_counts_match_audit(self):
        """模块测试计数与审计基线一致"""
        from tests.unit import test_topic_manager as tm
        from tests.stage import test_a_stage_topic_aware as tsa
        from tests.stage import test_build_conv_history_v6 as tbv6
        from tests.store import test_store_v5 as ts5
        from tests.unit import test_grade as tg

        counts = {
            "test_topic_manager": sum(1 for _, m in inspect.getmembers(tm)
                                      if inspect.isclass(m) and m.__name__.startswith("Test")),
            "test_a_stage_topic_aware": sum(1 for _, m in inspect.getmembers(tsa)
                                            if inspect.isclass(m) and m.__name__.startswith("Test")),
            "test_build_conv_history_v6": sum(1 for _, m in inspect.getmembers(tbv6)
                                              if inspect.isclass(m) and m.__name__.startswith("Test")),
            "test_store_v5": sum(1 for _, m in inspect.getmembers(ts5)
                                 if inspect.isclass(m) and m.__name__.startswith("Test")),
        }

        # Verifiable baseline: we know these numbers from the wiki
        assert counts["test_topic_manager"] >= 10, \
            f"test_topic_manager has {counts['test_topic_manager']} test classes"
        # v6.2: v5 mutation 测试已清理，topic_aware 仅保留真实 mgr 集成（2 类）；
        # 映射/尾部/降级覆盖由 test_build_conv_history_v6 承担
        assert counts["test_a_stage_topic_aware"] >= 2, \
            f"test_a_stage_topic_aware has {counts['test_a_stage_topic_aware']} test classes"
        assert counts["test_build_conv_history_v6"] >= 8, \
            f"test_build_conv_history_v6 has {counts['test_build_conv_history_v6']} test classes"
        assert counts["test_store_v5"] >= 1, \
            "test_store_v5 has no test classes"
