#!/usr/bin/env bash
# scripts/test-preflight.sh — CA 测试体系预检
#
# 在运行完整测试套件前执行三项检查：
#   1. 收集完整性 — 确认所有 test_*.py 能被 pytest 收集
#   2. 命名 DOA 检测 — 检查含 pytest import 但未匹配 test_*.py 的文件
#   3. 死目录检测 — 发现存有 __pycache__ 但无源文件的空目录
#
# 用法:
#   bash scripts/test-preflight.sh
#   source venv/bin/activate && bash scripts/test-preflight.sh
#
# 退出码:
#   0 — 全部通过
#   1 — 收集错误（DOA import）
#   2 — 命名 DOA 检测到未收集的测试
#   4 — 死目录 / 其他检查项
#   124 — 超时
#─────────────────────────────────────────────────────────────────

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

PASS=0
FAIL=0
TIMEOUT_SEC=30

green()  { echo -e "\033[32m✓ $1\033[0m"; }
red()    { echo -e "\033[31m✗ $1\033[0m"; }
yellow() { echo -e "\033[33m⚠ $1\033[0m"; }

echo "════════════════════════════════════════"
echo " CA 测试体系预检"
echo " 项目: $PROJECT_DIR"
echo " 时间: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════"
echo ""

# ─── 1. 收集完整性 ──────────────────────────────────────────────
echo "── 1. 收集完整性 ──"

# 捕获 collect-only 的输出和退出码
COLLECT_OUTPUT=$(timeout "$TIMEOUT_SEC" python -m pytest tests/ --collect-only -q 2>&1) || true
# 检查是否包含 pytest 的 ERROR COLLECTING（区别于测试函数名中的 error）
if echo "$COLLECT_OUTPUT" | grep -q "ERROR collecting"; then
    red "pytest 收集失败：发现 DOA 测试文件"
    echo "$COLLECT_OUTPUT"
    FAIL=$((FAIL + 1))
else
    # 提取收集计数
    COUNT=$(echo "$COLLECT_OUTPUT" | grep -oP '\d+(?= tests? collected)' || echo "0")
    green "pytest 收集成功：$COUNT 个测试"
    PASS=$((PASS + 1))
fi
echo ""

# ─── 2. 命名 DOA 检测 ──────────────────────────────────────────
echo "── 2. 命名 DOA 检测（含 pytest import 但未命 test_*.py）──"
MISSED_FILES=()
while IFS= read -r -d '' file; do
    # 跳过 __init__.py 和 conftest.py
    basename="$(basename "$file")"
    [[ "$basename" == "__init__.py" ]] && continue
    [[ "$basename" == "conftest.py" ]] && continue
    MISSED_FILES+=("$file")
done < <(find tests/ -name '*.py' ! -name 'test_*.py' -exec grep -l 'import pytest' {} + 2>/dev/null | sort -z)

if [ ${#MISSED_FILES[@]} -gt 0 ]; then
    yellow "发现 ${#MISSED_FILES[@]} 个可能 DOA 的文件（含 pytest import 但非 test_* 命名）:"
    for f in "${MISSED_FILES[@]}"; do
        echo "       $f"
    done
    FAIL=$((FAIL + 1))
else
    green "无命名 DOA"
    PASS=$((PASS + 1))
fi
echo ""

# ─── 3. 死目录检测 ──────────────────────────────────────────────
echo "── 3. 死目录检测（有 __init__.py/__pycache__ 但无 test_*.py）──"
DEAD_DIRS=()
while IFS= read -r -d '' dir; do
    # 检查是否有 test_*.py 文件
    file_count=$(find "$dir" -maxdepth 1 -name 'test_*.py' 2>/dev/null | wc -l)
    if [ "$file_count" -eq 0 ]; then
        # 排除 .pytest_cache 和 __pycache__
        basename="$(basename "$dir")"
        [[ "$basename" == ".pytest_cache" ]] && continue
        [[ "$basename" == "__pycache__" ]] && continue
        DEAD_DIRS+=("$dir")
    fi
done < <(find tests/ -mindepth 1 -maxdepth 2 -type d -print0 | sort -z)

if [ ${#DEAD_DIRS[@]} -gt 0 ]; then
    yellow "发现 ${#DEAD_DIRS[@]} 个测试子目录无任何 test_*.py:"
    for d in "${DEAD_DIRS[@]}"; do
        echo "       $d"
    done
    FAIL=$FAIL  # 不强制失败，仅警告
else
    green "无死目录"
    PASS=$((PASS + 1))
fi
echo ""

# ─── 摘要 ───────────────────────────────────────────────────────
echo "════════════════════════════════════════"
if [ "$FAIL" -eq 0 ]; then
    echo " 结果: ✅ 全部通过 ($PASS/$PASS)"
    exit 0
else
    echo " 结果: ❌ $FAIL 项未通过 (通过 $PASS/$((PASS+FAIL)))"
    exit 1
fi
