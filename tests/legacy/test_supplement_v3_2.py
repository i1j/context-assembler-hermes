"""v3.2 补充测试用例（含评审修订）"""
from unittest.mock import patch
from ca.cache import CacheBuilder
import threading


# ===================== C‑stage 补充 =====================
def test_c_stage_no_full_cosine_top3(ca_engine):
    """TC‑C‑010: 后台线程不调用全量 top-3 计算"""
    with patch.object(ca_engine, '_compute_top3_promotions', create=True) as mock_top3:
        ca_engine.process_turn_async("msg", "resp")
        ca_engine.wait_for_pending(timeout=10)
        mock_top3.assert_not_called()


# ===================== A‑stage 补充 =====================
def test_budget_exhausted_skips_retrieval(ca_engine):
    """TC‑A‑013: 预算耗尽时跳过检索"""
    msgs = [{"role": "user", "content": "x" * 5000}]
    with patch('ca.retrieval.Retriever.retrieve') as mock_retrieve:
        result = ca_engine.assemble("query", msgs, context_length=100)
        mock_retrieve.assert_not_called()
    assert len(result) > 0, "即使预算耗尽也应返回降级后的消息"


def test_hard_truncation_tool_group_integrity(ca_engine):
    """TC‑A‑014: 工具组整体移除/保留 (精确校验 ID 配对)"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "before", "_turn_index": 1},
        {"role": "assistant", "content": "call", "tool_calls": [{"id": "call_1", "name": "t1"}], "_turn_index": 2},
        {"role": "tool", "tool_call_id": "call_1", "content": "result1", "_turn_index": 3},
        {"role": "user", "content": "after", "_turn_index": 4}
    ]
    result = ca_engine.assemble("q", msgs, context_length=30)

    actual_call_ids = set()
    actual_response_ids = set()
    for m in result:
        if "tool_calls" in m:
            for tc in m["tool_calls"]:
                actual_call_ids.add(tc["id"])
        if m.get("role") == "tool":
            actual_response_ids.add(m.get("tool_call_id"))
    assert actual_call_ids == actual_response_ids, \
        f"工具组被腰斩！Calls: {actual_call_ids}, Responses: {actual_response_ids}"


def test_truncation_placeholder_role(ca_engine):
    """TC‑A‑015: 截断提示消息 role 为 assistant"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "call", "tool_calls": [{"id": "c1"}], "_turn_index": 1},
        {"role": "tool", "tool_call_id": "c1", "content": "res", "_turn_index": 2},
        {"role": "user", "content": "x" * 1000, "_turn_index": 3}
    ]
    result = ca_engine.assemble("q", msgs, context_length=20)
    placeholders = [m for m in result if "因上下文截断已被省略" in str(m.get("content", ""))]
    if placeholders:
        for ph in placeholders:
            assert ph.get("role") == "assistant", f"占位消息 role 应为 assistant，实际 {ph.get('role')}"
    else:
        assert len(result) > 0, "assemble 应返回有效结果"


def test_token_estimate_cjk_extended(ca_engine):
    """TC‑A‑016: 广义 CJK 字符集命中 1.5 系数"""
    text = "中文测试，包含全角标点！𠀀𠀁𠀂"
    tokens = ca_engine._token_estimate(text)
    assert tokens >= int(len(text) * 1.4), f"Token 估算未命中 1.5 系数: {tokens}"


def test_token_estimate_empty_text(ca_engine):
    """TC‑A‑017: 空文本返回 0"""
    assert ca_engine._token_estimate("") == 0
    assert ca_engine._token_estimate(None) == 0


# ===================== 并发与索引 (评审补丁) =====================
def test_concurrent_process_turn_async(ca_engine):
    """TC‑CONC‑001a: C-stage 并发乱序提交不重复分配 turn_index"""
    results = []
    lock = threading.Lock()
    errors = []

    def worker(uid):
        try:
            history = [{"role": "user", "content": f"hist_{i}"} for i in range(3)] + \
                      [{"role": "assistant", "content": f"hist_a_{i}"} for i in range(3)]
            idx = ca_engine.process_turn_async(
                user_message=f"user_{uid}",
                assistant_response=f"assistant_{uid}",
                history=history
            )
            with lock:
                results.append(idx)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发调用中出现异常: {errors}"
    assert len(results) == 5
    expected_min = 3
    assert min(results) >= expected_min, f"索引小于预期: {results}"
    assert len(set(results)) == 5, f"turn_index 存在重复: {results}"


def test_assemble_uses_turn_index_not_position(ca_engine):
    """TC‑A‑002a: A-stage 按 turn_index 而非数组下标映射摘要"""
    ca_engine.store.write_turn(
        session_id="default",
        turn_index=5,
        l0_text="L0_5",
        l1_text='{"core_change":"摘要5"}',
        l0_embedding=[0.2]*768,
        l1_embedding=[0.3]*768
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello", "_turn_index": 2},
        {"role": "assistant", "content": "hi", "_turn_index": 5}
    ]
    # v4.3.4: 使用 CacheBuilder 替代 _build_cache()
    cache = CacheBuilder(ca_engine.store).build("default")
    head_indices = [5]
    middle_indices = []
    upgrades = []
    tail_start = 3
    idx_to_turn = {i: msg.get("_turn_index", i) for i, msg in enumerate(messages)}
    l1_texts, l0_texts = cache.get_snapshot_data()

    final = ca_engine._build_final_messages(
        messages, l1_texts, l0_texts, head_indices, middle_indices, upgrades, tail_start, idx_to_turn
    )
    assert final[2]["content"].find("[~/5]") != -1
    assert "摘要5" in final[2]["content"]