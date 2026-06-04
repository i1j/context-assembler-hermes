import time


def test_c_stage_completion_time(ca_engine):
    t0 = time.time()
    ca_engine.process_turn_async("hi", "resp")
    ca_engine.wait_for_pending(10)
    t1 = time.time()
    assert t1 - t0 < 5


def test_assemble_performance_baseline(ca_engine):
    msgs = [{"role": "user", "content": f"msg_{i}"} for i in range(50)]
    t0 = time.time()
    ca_engine.assemble("query", msgs)
    t1 = time.time()
    assert t1 - t0 < 1.0
