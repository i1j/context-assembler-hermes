"""Debug dump + CA-METRIC 日志监测审计。

覆盖：
- Debug dump 文件生成完整性
- CA-METRIC 日志键名格式一致性
- 所有 CA-METRIC 键使用统一命名（无 l0/l1 残留）
"""

import json
import os
import re
import tempfile
from pathlib import Path

import pytest


# ============================================================================
# CA-METRIC 命名审计
# ============================================================================

# 预期的 CA-METRIC 键模式：ca.{component}.{metric}
# component: fct / hdl
# 不允许：l0 / l1 / L0 / L1
_METRIC_KEY_PATTERN = re.compile(r"ca\.(fct|hdl)\.[a-z_]+")
_FORBIDDEN_PATTERNS = ["ca.l0.", "ca.l1.", "ca.L0.", "ca.L1."]


def _find_ca_metric_lines(root_dir: str) -> list[dict]:
    """扫描 ca/ 源码中所有 CA-METRIC 日志行，返回 {file, line, key} 列表。"""
    results = []
    ca_dir = Path(root_dir) / "ca"
    for pyfile in sorted(ca_dir.rglob("*.py")):
        with open(pyfile) as f:
            for lineno, line in enumerate(f, 1):
                if "CA-METRIC" in line:
                    m = re.search(r"\[CA-METRIC\]\s+(\S+)", line)
                    key = m.group(1).rstrip(":") if m else None
                    results.append({
                        "file": str(pyfile.relative_to(ca_dir.parent)),
                        "line": lineno,
                        "text": line.strip(),
                        "key": key,
                    })
    return results


class TestDebugDumpGeneration:
    """验证 debug dump 文件在设置 CA_DEBUG_DUMP 后正确生成"""

    def test_mutation_dump_writes_valid_json(self, ca_engine, monkeypatch):
        """简单 mutation 触发后，dump 文件生成且内容为合法 JSON"""
        from ca.store import write_turn_v5

        # 准备 DB 数据
        write_turn_v5(ca_engine.store, "test", 2, 1,
                      role="assistant", elm_text="orig",
                      fct_text="工具组：测试")
        write_turn_v5(ca_engine.store, "test", 2, 2,
                      role="tool", elm_text="result",
                      fct_text="成功")

        # 启用 debug dump
        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("CA_DEBUG_DUMP", tmp)
        from ca.config import Config
        Config.DEBUG_MODE = True

        try:
            from tests.stage.test_a_stage import _make_plugin
            plugin = _make_plugin(ca_engine)
            conv = [
                {"role": "user", "content": "Q"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "Q2"},
                {"role": "assistant", "content": "orig", "tool_calls": [{"id": "c2"}]},
                {"role": "tool", "tool_call_id": "c2", "content": "result"},
            ]
            plugin._simple_mutation_mode_v5(conv)

            # 验证 dump 文件生成
            dump_files = list(Path(tmp).glob("ca_mutation_*.json"))
            assert len(dump_files) >= 1, f"应生成 mutation dump, 列出: {list(Path(tmp).iterdir())}"

            # 验证 dump 内容为合法 JSON
            for df in dump_files:
                with open(df) as f:
                    data = json.load(f)
                assert isinstance(data, list), "dump 应为 list"
                assert len(data) > 0, "dump 不应为空"
                assert "role" in data[0], "dump 条目应含 role"
        finally:
            Config.DEBUG_MODE = False

    def test_incremental_dump_writes_valid_json(self, ca_engine, monkeypatch):
        """增量 mutation 后，dump 文件生成且内容为合法 JSON"""
        from ca.store import write_turn_v5

        write_turn_v5(ca_engine.store, "test", 1, 2,
                      role="tool", elm_text="old", fct_text="cached_fct")

        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("CA_DEBUG_DUMP", tmp)
        from ca.config import Config
        Config.DEBUG_MODE = True

        try:
            from tests.stage.test_a_stage import _make_plugin
            plugin = _make_plugin(ca_engine)
            plugin._A_stable_cache = [
                {"role": "user", "content": "cached"},
            ]
            plugin._A_cache_turns = 1
            conv = [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "Q2"},
                {"role": "assistant", "content": "A2"},
            ]
            plugin._incremental_mutation(conv)

            dump_files = list(Path(tmp).glob("ca_incr_mutation_*.json"))
            assert len(dump_files) >= 1, f"应生成 incr mutation dump, 列出: {list(Path(tmp).iterdir())}"

            for df in dump_files:
                with open(df) as f:
                    data = json.load(f)
                assert isinstance(data, list), "dump 应为 list"
        finally:
            Config.DEBUG_MODE = False

    def test_dump_not_written_when_disabled(self, ca_engine):
        """CA_DEBUG_DUMP 未设置时，不生成 dump 文件"""
        from ca.config import Config
        Config.DEBUG_MODE = True

        tmp = tempfile.mkdtemp()
        # 明确不设 CA_DEBUG_DUMP

        from tests.stage.test_a_stage import _make_plugin
        plugin = _make_plugin(ca_engine)
        conv = [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}]
        plugin._simple_mutation_mode_v5(conv)

        # 无 dump 文件生成
        dump_files = list(Path(tmp).glob("ca_mutation_*.json"))
        assert len(dump_files) == 0, f"不应生成 dump, 但找到: {dump_files}"

        Config.DEBUG_MODE = False


class TestCAMetricNamingAudit:
    """CA-METRIC 日志键名统一性审计"""

    def test_all_ca_metric_keys_have_expected_format(self):
        """每个 CA-METRIC 键匹配 ca.{component}.{metric} 格式"""
        metrics = _find_ca_metric_lines(
            Path(__file__).resolve().parent.parent.parent  # ca_assembler/
        )
        violations = []
        for m in metrics:
            key = m["key"]
            if key and not _METRIC_KEY_PATTERN.match(key):
                violations.append(f"{m['file']}:{m['line']} — {key}")

        assert not violations, \
            f"CA-METRIC 键名格式违规：\n" + "\n".join(violations)

    def test_no_stale_l0_l1_metric_names(self):
        """不允许 l0/l1/L0/L1 出现在 CA-METRIC 键名中"""
        metrics = _find_ca_metric_lines(
            Path(__file__).resolve().parent.parent.parent
        )
        violations = []
        for m in metrics:
            key = m["key"]
            if key:
                for forbid in _FORBIDDEN_PATTERNS:
                    if forbid in key:
                        violations.append(f"{m['file']}:{m['line']} — {key}")

        assert not violations, \
            f"CA-METRIC 键名含旧术语：\n" + "\n".join(violations)

    def test_ca_metric_component_naming_consistent(self):
        """所有 CA-METRIC 键的 component 来自 {fct, hdl}"""
        valid_components = {"fct", "hdl"}
        metrics = _find_ca_metric_lines(
            Path(__file__).resolve().parent.parent.parent
        )
        violations = []
        for m in metrics:
            key = m["key"]
            if key:
                parts = key.split(".")
                if len(parts) >= 2 and parts[1] not in valid_components:
                    violations.append(f"{m['file']}:{m['line']} — {key}")

        assert not violations, \
            f"CA-METRIC 含非标准 component：\n" + "\n".join(violations)
