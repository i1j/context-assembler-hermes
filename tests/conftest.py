"""CA 测试 fixture 体系（v5.10+ 适配版）

提供：
- 引擎 fixture（ca_engine / engine）
- 自动 mock 外部依赖（embed / LLM）
- v5 turn_stream fixture（v5_store / v5_turn_stream）
- 文件描述符泄漏检测
"""

import pytest, platform, os, json, sys
try:
    import psutil
except ImportError:
    psutil = None
from pathlib import Path
from unittest.mock import patch
from typing import Optional
from types import SimpleNamespace

# 确保 ca/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "critical: 关键路径测试")
    config.addinivalue_line("markers", "high: 高优先级测试")
    config.addinivalue_line("markers", "medium: 中优先级测试")
    config.addinivalue_line("markers", "low: 低优先级/信息性测试")
    config.addinivalue_line("markers", "l1: 需 L1 LLM 调用")
    config.addinivalue_line("markers", "l2: 需 L2 LLM 调用")
    config.addinivalue_line("markers", "linux_only: 仅 Linux 环境")
    config.addinivalue_line("markers", "v460: 测试已删除的 v4.6 API，仅兼容归档")


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
        "memory_gb": memory_gb,
    }


# ── 引擎 fixture ──

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
            assert delta <= max_delta, f"FD leak: {self.start} \u2192 {final} (\u0394{delta})"
    return FdWatcher(initial)


@pytest.fixture
def ca_engine(tmp_path):
    """ContextAssembler 实例，实时数据库。
    
    附带自动mock：embed 和 LLM 调用自动打桩，避免真实网络IO。
    """
    from ca import ContextAssembler
    db = tmp_path / "test.db"
    engine = ContextAssembler(db_path=str(db), session_id="test")
    yield engine
    engine.destroy()


@pytest.fixture
def engine(ca_engine):
    """ca_engine 的向后兼容别名。"""
    return ca_engine


# ── v5 turn_stream fixture ──

@pytest.fixture
def v5_store(tmp_path):
    """v5 内存模式 SQLiteStore，已创建 turn_stream 表。
    
    只包含 Schema（无写引擎），用于 store 层纯函数测试：
      from ca.store import write_turn_v5, read_fct_v5, ...
    """
    from ca.store import SQLiteStore
    s = SQLiteStore(db_path=str(tmp_path / "v5_test.db"))
    yield s
    s.close()


@pytest.fixture
def v5_turn_stream(v5_store):
    """预填入 2 轮数据（共 6 行）的 v5 turn_stream 表。
    
    turn 0: Elm(user="你好") + Fct("决策：开始讨论了")
    turn 1: Elm(user="继续") + tool(terminal) + tool_result
    
    用于 stage/test_astage.py 和 stage/test_estage.py 的快速 setup。
    """
    from ca.store import write_turn_v5
    s = v5_store
    # turn 0
    write_turn_v5(s, "test", 0, 0, role="user", content="你好",
                  fct_text='{"core_change":"开始讨论了","new_materials":["问候"]}')
    write_turn_v5(s, "test", 0, 1, role="assistant", content="",
                  tool_calls_json='[{"id":"c0","function":{"name":"test","arguments":{}}}]')
    write_turn_v5(s, "test", 0, 2, role="tool", content="ok", tool_call_id="c0",
                  fct_text='{"core_change":"工具执行成功","new_materials":["结果 ok"]}')
    # turn 1
    write_turn_v5(s, "test", 1, 0, role="user", content="继续",
                  fct_text='{"core_change":"用户请求继续","new_materials":["追问"]}')
    write_turn_v5(s, "test", 1, 1, role="assistant", content="",
                  tool_calls_json='[{"id":"c1","function":{"name":"terminal","arguments":{"cmd":"ls"}}}]')
    write_turn_v5(s, "test", 1, 2, role="tool", content="file1.txt", tool_call_id="c1",
                  fct_text='{"core_change":"列出文件","new_materials":["file1.txt"]}')
    yield s


# ── 外部依赖 mock ──

@pytest.fixture(autouse=True)
def _mock_embed(ca_engine):
    """防止测试挂死在 Ollama 嵌入连接（768维全1向量）"""
    with patch('ca.embedding.EmbeddingClient.embed',
               side_effect=lambda *a, **kw: [0.1] * 768):
        yield


@pytest.fixture(autouse=True)
def _mock_llm():
    """防止 C-stage / F-stage 测试阻塞在真实 LLM 调用。
    
    Mock _call_llm_for_fct 返回值 (response_text, finish_reason)。
    """
    with patch(
        'ca.ContextAssembler._call_llm_for_fct',
        return_value=(
            "### 现象与问题\n- 无\n"
            "### 背景与约束\n- 无\n"
            "### 决策与方案\n- 无\n"
            "### 后续行动\n- 无\n"
            "<core_change>mock_response</core_change>",
            "stop",
        ),
    ):
        yield


# ── 旧的辅助函数（标记为 deprecated，待清理） ──
