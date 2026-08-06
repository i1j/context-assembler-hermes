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

from ca.grade import TopicGrade


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
    """验证 debug dump 文件在 DEBUG_MODE 下由 _build_conv_history_v6 生成（v6.2）"""

    def _write_turns(self, store, sid="test"):
        """写 3 个 turn（2 尾部保护区）触发 _build_conv_history_v6。"""
        from ca.store import write_turn_v5
        for t in range(1, 4):
            write_turn_v5(store, sid, t, 0, role="user", elm_text=f"Q{t}")
            write_turn_v5(store, sid, t, 1, role="assistant", elm_text="",
                          finish_reason="stop",
                          fct_text='{"core_change":"F%d"}' % t)

    def test_build_dump_writes_valid_json(self, ca_engine, monkeypatch):
        """_build_conv_history_v6 + DEBUG_MODE → dump 文件生成且内容为合法 JSON。"""
        import time as _time
        from unittest.mock import MagicMock
        from ca.config import Config

        sid = f"dump_on_{int(_time.time() * 1000)}"
        self._write_turns(ca_engine.store, sid=sid)
        ca_engine._session_id = sid
        Config.DEBUG_MODE = True
        try:
            mgr = MagicMock()
            mgr.get_turn_grade.return_value = TopicGrade.ACT
            ca_engine._build_conv_history_v6(mgr)

            dump_files = list(Path("/tmp").glob(f"ca_conv_hist_{sid}_*.json"))
            assert len(dump_files) >= 1, \
                f"应生成 build dump, 列出: {[p.name for p in dump_files]}"

            # 验证 dump 内容为合法 JSON（v6 dump 是 dict）
            latest = max(dump_files, key=lambda p: p.stat().st_mtime)
            with open(latest) as f:
                data = json.load(f)
            assert isinstance(data, dict), "v6 dump 应为 dict"
            assert data.get("session_id") == sid
            assert isinstance(data.get("messages"), list)
            assert len(data["messages"]) > 0, "dump 不应为空"
        finally:
            Config.DEBUG_MODE = False

    def test_dump_not_written_when_disabled(self, ca_engine):
        """DEBUG_MODE=False（默认）→ 不生成 dump 文件。"""
        import time as _time
        from unittest.mock import MagicMock
        from ca.config import Config

        sid = f"dump_off_{int(_time.time() * 1000)}"
        self._write_turns(ca_engine.store, sid=sid)
        ca_engine._session_id = sid
        Config.DEBUG_MODE = False
        try:
            mgr = MagicMock()
            mgr.get_turn_grade.return_value = TopicGrade.ACT
            ca_engine._build_conv_history_v6(mgr)

            dump_files = list(Path("/tmp").glob(f"ca_conv_hist_{sid}_*.json"))
            assert len(dump_files) == 0, \
                f"不应生成 dump, 但找到: {[p.name for p in dump_files]}"
        finally:
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
