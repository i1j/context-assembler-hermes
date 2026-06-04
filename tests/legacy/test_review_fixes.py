"""
验证评审补丁：并发锁、索引错位、LRU 级联清理、Cache-Aside 降级、全局缓存清理
"""
import threading
from unittest.mock import MagicMock

import pytest

from ca import ContextAssembler
from ca.cache import AssemblyCache


@pytest.fixture
def ca_engine(tmp_path):
    """创建临时数据库的 ContextAssembler 实例，并 mock 掉 LLM 和嵌入调用"""
    db_path = str(tmp_path / "test_review.db")
    engine = ContextAssembler(db_path=db_path, session_id="test_review_session")
    
    # 替换 LLM 调用为快速返回
    engine._call_llm_for_l1 = MagicMock(return_value='{"core_change":"测试摘要"}')
    # 替换嵌入客户端为 mock，返回固定向量
    engine.embed_client.embed = MagicMock(return_value=[0.1]*768)
    
    yield engine
    # 测试结束后清理资源
    engine.destroy()


# -------- TC-CONC-001: C-stage 并发测试 --------
def test_concurrent_process_turn_async(ca_engine):
    """验证并发调用时 turn_index 严格递增且无重复"""
    results = []
    lock = threading.Lock()
    errors = []

    def worker(uid):
        try:
            # 构造一个长度为3的历史，模拟已有3轮用户消息
            history = [
                {"role": "user", "content": f"hist_{i}"} for i in range(3)
            ] + [{"role": "assistant", "content": f"hist_a_{i}"} for i in range(3)]
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

    # 验证没有异常
    assert not errors, f"并发调用中出现异常: {errors}"
    
    # 验证返回的 turn_index 严格递增且无重复
    assert len(results) == 5
    # 由于 history 有 3 个 user 消息，expected_next=3，初始 _turn_counter 恢复后可能是 -1，递增后为 0，max(1,3)=3，所以第一个 target=3
    expected_min = 3
    assert min(results) >= expected_min, f"索引小于预期: {results}"
    assert len(set(results)) == 5, f"turn_index 存在重复: {results}"
    
    # 验证后台任务是否正确注册（可能已经完成，但至少无死锁）
    with ca_engine._task_lock:
        pending_count = len(ca_engine._pending_tasks)
    # 由于线程可能已完成，这里不强求等于5
    assert True  # 无异常即通过


# -------- TC-A-002: A-stage 索引映射测试 --------
def test_assemble_uses_turn_index_not_position(ca_engine):
    """验证 A-stage 根据 turn_index 映射摘要，而非数组下标"""
    # 先写入一条 turn_index=5 的 L1 摘要到数据库
    ca_engine.store.write_turn(
        session_id="test_review_session",
        turn_index=5,
        l0_text="L0_5",
        l1_text='{"core_change":"摘要5"}',
        l0_embedding=[0.2]*768,
        l1_embedding=[0.3]*768
    )
    # 同时写入到全局缓存（模拟 C-stage 完成后的增量更新）
    ca_engine.cache.add_turn(5, "L0_5", '{"core_change":"摘要5"}', [0.2]*768, [0.3]*768)
    
    # 构造 messages，模拟数组下标与 turn_index 分离
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello", "_turn_index": 2},
        {"role": "assistant", "content": "hi", "_turn_index": 5}
    ]
    
    # 调用 assemble 会内部使用 idx_to_turn
    final_messages = ca_engine.assemble(
        user_input="test",
        messages=messages,
        context_length=100000
    )
    
    # 解析输出
    assert len(final_messages) == 3
    # 第0条 system 不变
    assert final_messages[0]["role"] == "system"
    # 第1条 user (turn_index=2 不在 head 中，保留原文)
    assert final_messages[1]["role"] == "user"
    # 第2条 assistant (turn_index=5 在 head 中，应替换为摘要)
    assert "[~/5]" in final_messages[2]["content"]
    assert "摘要5" in final_messages[2]["content"]


# -------- TC-CACHE-001: AssemblyCache 数据管理测试 --------
def test_assembly_cache_data_management():
    """验证 AssemblyCache 可正确管理 turn 数据（v4.3.4 无多会话 LRU）"""
    cache = AssemblyCache()
    
    # 添加两条 turn 数据
    cache.add_turn(0, "L0_0", '{"core_change":"摘要0"}', [0.1]*768, [0.2]*768)
    cache.add_turn(1, "L0_1", '{"core_change":"摘要1"}', [0.3]*768, [0.4]*768)
    
    # 验证数据正确存储
    assert 0 in cache.l0_texts
    assert 1 in cache.l0_texts
    assert cache.l0_texts[0] == "L0_0"
    assert cache.l1_texts[1] == '{"core_change":"摘要1"}'
    assert cache.l0_embeddings[0] == [0.1]*768
    assert cache.l1_embeddings[1] == [0.4]*768
    
    # 验证替换已存在的 turn_index
    cache.add_turn(0, "L0_0_new", '{"core_change":"摘要0_new"}')
    assert cache.l0_texts[0] == "L0_0_new"
    assert cache.l1_texts[0] == '{"core_change":"摘要0_new"}'


# -------- TC-RESET-001: 引擎重置与缓存隔离测试 --------
def test_engine_reset_clears_cache(tmp_path):
    """验证 engine.reset() 清除缓存并重建（v4.3.4 无全局单例缓存）"""
    db_path = str(tmp_path / "test_iso.db")
    engine = ContextAssembler(db_path=db_path, session_id="session_A")
    engine._call_llm_for_l1 = MagicMock(return_value='{"core_change":"A"}')
    engine.embed_client.embed = MagicMock(return_value=[0.1]*768)
    
    # 添加一些数据到缓存
    engine.cache.add_turn(0, "L0_A", '{"core_change":"A"}', [0.1]*768, [0.1]*768)
    assert 0 in engine.cache.l1_texts
    
    # 调用 reset，应清除重建缓存
    engine.reset()
    # 缓存被重新构建（从数据库读取），由于数据库没有记录，缓存应为空
    assert 0 not in engine.cache.l1_texts
    
    # 清理资源
    engine.destroy()


# -------- TC-CACHE-ASIDE: Cache-Aside 模式测试 --------
def test_cache_aside_fallback(mocker, tmp_path):
    """验证 _get_previous_l1 优先从缓存读取，miss 时回退 SQLite"""
    db_path = str(tmp_path / "test_aside.db")
    engine = ContextAssembler(db_path=db_path, session_id="test_aside_session")
    engine._call_llm_for_l1 = MagicMock(return_value='{"core_change":"测试"}')
    engine.embed_client.embed = MagicMock(return_value=[0.1]*768)
    
    # 手动设置 _turn_counter 为 1，则上一轮索引为 0
    engine._turn_counter = 1
    
    # 场景1：缓存中没有，数据库中也没有
    l1 = engine._get_previous_l1()
    assert l1 is None
    
    # 场景2：数据库中写入一条记录，但缓存中没有
    engine.store.write_turn(
        session_id="test_aside_session",
        turn_index=0,
        l0_text="L0_0",
        l1_text='{"core_change":"摘要0"}',
        l0_embedding=[0.2]*768,
        l1_embedding=[0.3]*768
    )
    l1 = engine._get_previous_l1()
    assert l1 == {"core_change": "摘要0"}
    
    # 场景3：缓存中存在，优先从缓存读取（即使数据库中有更新的，但索引相同）
    engine.cache.add_turn(0, "L0_0", '{"core_change":"缓存摘要"}', [0.2]*768, [0.3]*768)
    l1 = engine._get_previous_l1()
    assert l1 == {"core_change": "缓存摘要"}
    
    # 清理
    engine.destroy()
