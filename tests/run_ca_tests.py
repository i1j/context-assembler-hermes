#!/usr/bin/env python3
"""run_ca_tests.py — CA 测试运行包装器

用法:
  python tests/run_ca_tests.py          # 完整套件（含预检）
  python tests/run_ca_tests.py --quick  # 跳过预检，直接跑测试

与直接 pytest 的区别：
  - 先运行预检（scripts/test-preflight.sh）防止死胎测试
  - 设置 hermetic 环境（清空 API key、临时 HOME、UTC 时区）
  - 自动传递 -n 4 参数（CPU 核心适配）
  - 统一报告格式

退出码：
  0 — 全部通过
  1 — 预检失败
  2 — 测试失败
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = PROJECT_DIR / "scripts"
TEST_DIR = PROJECT_DIR / "tests"

PREFLIGHT_SCRIPT = SCRIPT_DIR / "test-preflight.sh"


def run_preflight() -> bool:
    """运行预检脚本。返回 True 表示通过。"""
    if not PREFLIGHT_SCRIPT.exists():
        print("[CA run_tests] 预检脚本不存在，跳过")
        return True
    result = subprocess.run(
        ["bash", str(PREFLIGHT_SCRIPT)],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        return False
    return True


def run_tests() -> bool:
    """运行完整测试套件。返回 True 表示全部通过。"""
    # 清空敏感环境变量
    env = os.environ.copy()
    for key in list(env.keys()):
        if key.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            env[key] = ""

    env.setdefault("TZ", "UTC")
    env.setdefault("LANG", "C.UTF-8")

    cmd = [
        sys.executable, "-m", "pytest",
        str(TEST_DIR),
        "-n", "4",
        "-q",
        "--tb=short",
    ]

    print(f"[CA run_tests] {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR), env=env)
    return result.returncode == 0


def main():
    quick_mode = "--quick" in sys.argv

    print("╔══════════════════════════════════════════╗")
    print("║  CA Context Assembler — 测试套件         ║")
    print("╚══════════════════════════════════════════╝")
    print()

    if not quick_mode:
        print("── 阶段 1: 预检 ──")
        if not run_preflight():
            print("[FAIL] 预检未通过，终止")
            sys.exit(1)
        print("[PASS] 预检通过")
        print()

    print("── 阶段 2: 测试执行 ──")
    if not run_tests():
        print("[FAIL] 测试未全部通过")
        sys.exit(2)

    print()
    print("╔══════════════════════════════════════════╗")
    print("║  🎉 全部通过                             ║")
    print("╚══════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
