"""测试体系自防御（Self-Defense）— 运行时验证测试收集完整性

DOA（Dead-on-Arrival）检测：测试文件不应因模块级 import 失败而静默不被收集。

覆盖：
- DOA-001: 发现含 import pytest 但未匹配 test_*.py 的文件（命名 DOA）
- DOA-002: 发现含 test_*.py 匹配但模块级 import 崩溃的文件（import DOA — 通过 pytest 自身行为检测）
- DOA-003: 验证空测试目录（有 __init__.py 但无测试用例）

设计决策:
   → CR-007: 断路器缺失 → plugin 测试 DOA（2026-06-20）
   → CR-007b: 审计测试命名 DOA（2026-06-20）
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent   # ca_assembler/tests/audit/
_PROJECT_DIR = TESTS_DIR.parent.parent         # ca_assembler/
_TESTS_PACKAGE_DIR = TESTS_DIR.parent           # ca_assembler/tests/

# 确保项目根在 sys.path 中，使 importlib.import_module('tests.audit.test_*') 可工作
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))


def _find_py_files(glob_pattern: str = "**/*.py") -> list[Path]:
    """递归查找 tests/ 下的所有 .py 文件。"""
    return sorted(_TESTS_PACKAGE_DIR.glob(glob_pattern))


# ===========================================================================
# DOA-001: 命名 DOA — 文件含 import pytest 但未匹配 test_*.py
# ===========================================================================


def _get_doa_naming_candidates() -> list[Path]:
    """查找 tests/ 下含 import pytest 但文件名不为 test_*.py 的文件。"""
    candidates = []
    for pyfile in _find_py_files():
        if pyfile.name.startswith("test_"):
            continue
        if pyfile.name in ("__init__.py", "conftest.py"):
            continue
        # tests/fixtures/ 下的文件是 fixture 支持模块，非测试文件
        if pyfile.parent.name == "fixtures":
            continue
        try:
            content = pyfile.read_text(encoding="utf-8")
            if "import pytest" in content or "from pytest" in content:
                candidates.append(pyfile)
        except (OSError, UnicodeDecodeError):
            continue
    return candidates


def test_doa_naming_no_missed_tests():
    """DOA-001: 不应有含 pytest import 但未匹配 test_*.py 的文件。

    这类文件不会被 pytest 收集，其测试逻辑永远不执行。
    """
    missed = _get_doa_naming_candidates()
    assert not missed, (
        f"发现 {len(missed)} 个含 pytest import 的非 test_* 文件：\n"
        + "\n".join(f"  {f.relative_to(TESTS_DIR)}" for f in missed)
    )


# ===========================================================================
# DOA-002: import DOA — 每个 test_*.py 模块应能被收集
# ===========================================================================
# 注意：import DOA 通常由 pytest 自身在收集阶段报错。
# 本测试验证每个 test_*.py 文件至少能被 Python import 成功
# （import 级别错误，非 pytest 收集级别）。


def _get_all_test_files() -> list[Path]:
    """获取所有 test_*.py 文件。"""
    return _find_py_files("**/test_*.py")


def test_doa_import_every_test_file_loadable():
    """DOA-002: 每个 test_*.py 应能被 Python import 不抛异常。

    模块级 import 错误（AttributeError、NameError 等）意味着
    该文件在 pytest 收集阶段就会崩溃，所有测试函数永远不执行。
    """
    failed = []
    for testfile in _get_all_test_files():
        # 构造模块路径：ca_assembler/tests/unit/test_foo.py → tests.unit.test_foo
        rel = testfile.relative_to(_PROJECT_DIR)  # tests/unit/test_foo.py
        mod_name = str(rel.with_suffix("")).replace(os.sep, ".")
        try:
            importlib.import_module(mod_name)
        except Exception as exc:
            failed.append((testfile, exc))
    assert not failed, (
        f"发现 {len(failed)} 个 import 失败的测试文件：\n"
        + "\n".join(f"  {f.relative_to(_TESTS_PACKAGE_DIR)} ↴ {exc}" for f, exc in failed)
    )


# ===========================================================================
# DOA-003: 死目录检测 — 有 __init__.py 但零 test_*.py 的测试子目录
# ===========================================================================


def _get_dead_test_dirs() -> list[Path]:
    """检查 tests/ 下的一级子目录：有 __init__.py 但无 test_*.py 文件。"""
    dead = []
    for entry in sorted(TESTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in (".pytest_cache", "__pycache__"):
            continue
        # 跳过审计目录（单独检查）
        init_file = entry / "__init__.py"
        if not init_file.exists():
            continue
        has_test = bool(list(entry.glob("test_*.py")))
        if not has_test:
            dead.append(entry)
    return dead


def test_doa_no_dead_test_dirs():
    """DOA-003: 不应有含 __init__.py 但零 test_*.py 的测试子目录。

    这类目录会误导阅读者认为其中包含有效测试，但 pytest 不会收集任何测试。
    """
    dead = _get_dead_test_dirs()
    assert not dead, (
        f"发现 {len(dead)} 个空测试子目录（有 __init__.py 但无 test_*.py）：\n"
        + "\n".join(f"  {d.relative_to(TESTS_DIR)}/" for d in dead)
    )
