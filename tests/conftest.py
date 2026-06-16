import pytest, platform, os, json, sys
try:
    import psutil
except ImportError:
    psutil = None
from pathlib import Path
from unittest.mock import patch

# 确保 ca/ 可被 import（在 fixture 之前，供 test_c.py / test_store.py 等模块级 import 使用）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def pytest_configure(config):
    config.addinivalue_line("markers", "critical")
    config.addinivalue_line("markers", "high")
    config.addinivalue_line("markers", "medium")
    config.addinivalue_line("markers", "low")
    config.addinivalue_line("markers", "l1")
    config.addinivalue_line("markers", "l2")
    config.addinivalue_line("markers", "linux_only")

def _get_cpu_brand():
    try:
        import cpuinfo
        info = cpuinfo.get_cpu_info()
        brand = info.get("brand_raw") or info.get("brand")
        if brand: return brand
    except: pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":")[1].strip()
    except: pass
    return platform.processor() or "unknown"

def get_hardware_info():
    memory_gb = 0
    if psutil:
        try:
            memory_gb = psutil.virtual_memory().total / (1024**3)
        except Exception:
            pass
    return {
        "platform": platform.platform(),
        "cpu_brand": _get_cpu_brand(),
        "cpu_count": os.cpu_count() or 1,
        "memory_gb": memory_gb
    }

@pytest.fixture(scope="session")
def hardware_info():
    return get_hardware_info()

def get_fd_count():
    if psutil:
        try: return psutil.Process().num_fds()
        except: pass
    try: return len(os.listdir('/proc/self/fd'))
    except: return -1

@pytest.fixture
def fd_checker():
    initial = get_fd_count()
    if initial < 0:
        pytest.skip("FD count unavailable")
    class FdWatcher:
        def __init__(self, start): self.start = start
        def assert_no_leak(self, max_delta=10):
            final = get_fd_count()
            delta = final - self.start
            assert delta <= max_delta, f"FD leak: {self.start} → {final} (Δ{delta})"
    return FdWatcher(initial)

@pytest.fixture
def ca_engine(tmp_path):
    from ca import ContextAssembler
    db = tmp_path / "test.db"
    engine = ContextAssembler(db_path=str(db), session_id="test")
    yield engine
    engine.destroy()

@pytest.fixture
def engine(ca_engine):
    """Alias for ca_engine, for backward compat."""
    return ca_engine

# ---------- v4.4.0 适配 ----------
@pytest.fixture(autouse=True)
def _mock_embed(ca_engine):
    """防止测试挂死在 Ollama 连接"""
    with patch('ca.embedding.EmbeddingClient.embed', side_effect=lambda *a, **kw: [0.1] * 768):
        yield

@pytest.fixture(autouse=True)
def _mock_llm():
    """防止 C-stage 测试阻塞在真实 LLM 调用（qwen3.5:hermes-32k 生成耗时远超测试超时）

    新签名返回 Tuple[str, str]：(response_text, finish_reason)
    """
    with patch('ca.ContextAssembler._call_llm_for_fct',
               return_value=(
                   "### 现象与问题\n- 无\n"
                   "### 背景与约束\n- 无\n"
                   "### 决策与方案\n- 无\n"
                   "### 后续行动\n- 无\n"
                   "<core_change>mock_response</core_change>",
                   "stop"
               )):
        yield

def seed_dialogue(engine, turn_index, messages):
    """将消息列表写入 store，供 assemble 读取历史"""
    engine.store.write_turn(
        session_id="test",
        turn_index=turn_index,
        turn_type="dialogue",
        tool_sub_index=0,
        hdl_text="",
        fct_text="{}",
        elm_text=json.dumps(messages, ensure_ascii=False),
        _assemble_status=0
    )
