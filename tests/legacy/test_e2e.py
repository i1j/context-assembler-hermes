"""
端到端集成测试 (TC-ST-001~003).

需要真实 Ollama 嵌入服务。
LLM 调用被 mock（Qwen3 思考分离与旧版 Ollama 兼容性问题）。
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

# Ollama 实际运行端口（由环境变量或自动检测决定）
_OLLAMA_PORT = os.environ.get("CA_OLLAMA_PORT", "11436")
_OLLAMA_ENDPOINT = f"http://127.0.0.1:{_OLLAMA_PORT}"
_EMBED_MODEL = "dengcao/Qwen3-Embedding-0.6B:Q8_0"

# 多轮 Mock OODA 文本
MOCK_OODA_TURNS = [
    (
        "核心摘要：第一轮对话关于API设计\n"
        "资源与观察：\n- 讨论了RESTful API规范\n"
        "事实与约束：\n- 需要认证机制\n"
        "决策与结论：\n- 采用JWT方案\n"
        "后续行动：\n- 下周评审设计"
    ),
    (
        "核心摘要：第二轮数据库选型\n"
        "资源与观察：\n- 比较了PostgreSQL和MongoDB\n"
        "事实与约束：\n- 数据一致性要求高\n"
        "决策与结论：\n- 选择PostgreSQL\n"
        "后续行动：\n- 准备测试数据"
    ),
]


def ollama_available() -> bool:
    """检测 Ollama 服务是否可用。"""
    import urllib.request
    import json as _json

    try:
        req = urllib.request.Request(
            f"{_OLLAMA_ENDPOINT}/api/tags",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
            return len(data.get("models", [])) > 0
    except Exception:
        return False


@pytest.fixture
def e2e_config(monkeypatch):
    """配置端到端测试使用的 Ollama 端点。"""
    monkeypatch.setenv("CA_EMBED_ENDPOINT", _OLLAMA_ENDPOINT)
    monkeypatch.setenv("CA_EMBED_MODEL", _EMBED_MODEL)
    monkeypatch.setenv("CA_EMBED_BACKEND", "ollama")
    monkeypatch.setenv("CA_LLM_ENDPOINT", _OLLAMA_ENDPOINT)
    monkeypatch.setenv("CA_LLM_MODEL", "qwen3.5:hermes-32k")
    from ca.config import Config
    Config.reload()


def _call_llm_mock(messages, *args, **kwargs):
    """模拟 LLM 调用，返回预定义的 OODA 文本。

    根据消息内容轮转不同的 mock 输出，模拟多轮对话。
    """
    idx = hash(str(messages)) % len(MOCK_OODA_TURNS)
    return MOCK_OODA_TURNS[idx]


# ═══════════════════════════════════════════════════════════════════════
# TC-ST-001: 正常多轮对话端到端
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not ollama_available(), reason="Ollama 服务未运行")
def test_normal_dialogue_e2e(e2e_config, tmp_path):
    """TC-ST-001: 正常多轮对话端到端。

    验证：
      1. C-stage: process_turn_async 写入 L0+L1 到 SQLite
      2. 嵌入: 使用真实 Ollama 嵌入计算 cosine top-3
      3. A-stage: assemble() 含 [~/N] 标记
    """
    from ca import ContextAssembler

    db_path = str(tmp_path / "e2e_normal.db")
    engine = ContextAssembler(db_path=db_path)

    try:
        with patch.object(engine, "_call_llm_for_l1", side_effect=_call_llm_mock):
            # ── 第1轮对话 ──
            t1 = engine.process_turn_async(
                user_message="如何设计API？",
                assistant_response="建议使用RESTful API + JWT认证",
            )
            engine.wait_for_pending(timeout=30)
            assert t1 == 0, f"首轮 turn_index 应为 0, 实际 {t1}"

            # ── 第2轮对话 ──
            t2 = engine.process_turn_async(
                user_message="数据库选什么？",
                assistant_response="用PostgreSQL，数据一致性要求高",
            )
            engine.wait_for_pending(timeout=30)
            assert t2 == 1, f"第二轮 turn_index 应为 1, 实际 {t2}"

        # ── 验证 C-stage 写入 ──
        records = engine.store.read_session("default")
        assert len(records) == 2, f"应写入 2 条记录, 实际 {len(records)}"

        # 验证 L1 结构
        l1_0 = json.loads(records[0]["l1_text"])
        assert "core_change" in l1_0, "L1 应含 core_change"
        assert "new_materials" in l1_0, "L1 应含 new_materials"

        # 验证 L0 已提取
        assert len(records[0]["l0_text"]) > 0, "L0 不应为空"

        # ── A-stage 组装 ──
        msgs = [
            {"role": "user", "content": "如何设计API？"},
            {"role": "assistant", "content": "建议使用RESTful API + JWT认证"},
            {"role": "user", "content": "数据库选什么？"},
            {"role": "assistant", "content": "用PostgreSQL"},
            {"role": "user", "content": "好的"},
        ]
        result = engine.assemble(
            user_input="好的",
            messages=msgs,
            context_length=32000,
        )

        # 验证 A-stage 输出含 [~/N] 标记
        all_content = " ".join(m.get("content", "") for m in result)
        assert "[~/" in all_content, "assemble 输出应含 [~/N] 标记"

        # 验证首条消息的格式
        first = result[0]
        assert first.get("role") in ("system", "assistant"), f"首条消息 role 异常: {first.get('role')}"

    finally:
        engine.store.close()


# ═══════════════════════════════════════════════════════════════════════
# TC-ST-002: LLM 不可用降级
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not ollama_available(), reason="Ollama 服务未运行")
def test_llm_unavailable_degradation(e2e_config, tmp_path):
    """TC-ST-002: LLM 不可用时降级输出。

    LLM 调用抛异常时，引擎应写入降级摘要（"无有效增量"）。
    嵌入服务仍正常使用真实 Ollama。
    """
    from ca import ContextAssembler

    db_path = str(tmp_path / "e2e_degrade.db")
    engine = ContextAssembler(db_path=db_path)

    try:
        with patch.object(engine, "_call_llm_for_l1", side_effect=TimeoutError("LLM down")):
            engine.process_turn_async(
                user_message="测试消息",
                assistant_response="测试回复",
            )
            engine.wait_for_pending(timeout=30)

        records = engine.store.read_session("default")
        assert len(records) == 1, "降级后仍应写入记录"

        l1 = json.loads(records[0]["l1_text"])
        assert l1["core_change"] == "无有效增量", \
            f"降级 L1 core_change 应为'无有效增量', 实际: {l1['core_change']}"

    finally:
        engine.store.close()


# ═══════════════════════════════════════════════════════════════════════
# TC-ST-003: 断路器触发与恢复
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not ollama_available(), reason="Ollama 服务未运行")
def test_circuit_breaker_e2e(tmp_path):
    """TC-ST-003: 断路器触发与恢复。

    CA 插件层使用文件断路器：
      - 连续 3 次失败 → is_available() 返回 False
      - cooldown 后恢复 → is_available() 返回 True
      - 成功后重置失败计数
    """
    # 插件路径
    import sys
    PLUGIN_DIR = os.path.expanduser(
        "~/.hermes/hermes-agent/plugins"
    )
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)

    from plugins.context_engine.ca_assembler import (
        CAContextAssemblerPlugin,
        _record_failure,
        _record_success,
        _read_state,
        _write_state,
        is_available,
    )

    old_home = os.environ.get("HERMES_HOME")

    try:
        os.environ["HERMES_HOME"] = str(tmp_path)

        # ── 初始状态 ──
        plugin = CAContextAssemblerPlugin()
        assert is_available(), "初始断路器应可用"

        # ── 连续 3 次失败 → 断路器打开 ──
        _write_state({"failures": 0})
        for _ in range(3):
            _record_failure()

        plugin2 = CAContextAssemblerPlugin()
        assert not is_available(), "连续 3 次失败后应不可用"

        # ── cooldown 后恢复 ──
        import time
        _write_state({"failures": 3, "retry_after": time.monotonic() - 1})

        plugin3 = CAContextAssemblerPlugin()
        assert is_available(), "cooldown 后应恢复可用"

        # ── 成功后重置 ──
        _write_state({"failures": 2})
        _record_success()
        state = _read_state()
        assert state.get("failures", 0) == 0, "成功应重置失败计数"

    finally:
        if old_home is not None:
            os.environ["HERMES_HOME"] = old_home
        else:
            os.environ.pop("HERMES_HOME", None)
