"""Engine fixtures — ca_engine, engine, hardware_info, fd_checker."""

import os
import platform
import pytest

try:
    import psutil
except ImportError:
    psutil = None


def _get_cpu_brand():
    try:
        import cpuinfo
        info = cpuinfo.get_cpu_info()
        brand = info.get("brand_raw") or info.get("brand")
        if brand:
            return brand
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":")[1].strip()
    except Exception:
        pass
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


@pytest.fixture(scope="session")
def hardware_info():
    return get_hardware_info()


def get_fd_count():
    if psutil:
        try:
            return psutil.Process().num_fds()
        except Exception:
            pass
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return -1


@pytest.fixture
def fd_checker():
    initial = get_fd_count()
    if initial < 0:
        pytest.skip("FD count unavailable")

    class FdWatcher:
        def __init__(self, start):
            self.start = start

        def assert_no_leak(self, max_delta=10):
            final = get_fd_count()
            delta = final - self.start
            assert delta <= max_delta, f"FD leak: {self.start} → {final} (Δ{delta})"

    return FdWatcher(initial)


@pytest.fixture
def ca_engine(tmp_path):
    """ContextAssembler 实例，实时数据库。"""
    from ca import ContextAssembler

    db = tmp_path / "test.db"
    engine = ContextAssembler(db_path=str(db), session_id="test")
    yield engine
    engine.destroy()


@pytest.fixture
def engine(ca_engine):
    """ca_engine 的向后兼容别名。"""
    return ca_engine
