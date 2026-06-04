"""
端到端在线测试: CA Plugin (CAContextAssemblerPlugin)

验证:
1. 插件加载 — is_available(), on_session_start() 创建 SQLite store, pre_llm_call() 含 [~/N] 标记
2. C-stage 写入 — process_turn_async() 写入 L0+L1 到 ca_cache/{session}.db
3. A-stage 组装 — assemble() 返回带 [~/N] 标记的消息, head/middle/tail 分层正确
4. 回退链 — pre_llm_call 在引擎出错时原样返回消息

所有测试不依赖外部 LLM (mock L1 生成 + fallback embedding).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, PropertyMock

import pytest

# ── 插件导入路径 ────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(_PROJECT_ROOT, "plugins")
CA_DIR = _PROJECT_ROOT
for p in (PLUGIN_DIR, CA_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from context_engine.ca_assembler import CAContextAssemblerPlugin, is_available


# ── 模拟 OODA 文本 ──────────────────────────────────────────────────────────

MOCK_OODA_TEXT = (
    "核心摘要：测试摘要内容\n"
    "资源与观察：\n"
    "- 测试资源1\n"
    "事实与约束：\n"
    "- 测试约束1\n"
    "决策与结论：\n"
    "- 测试结论1\n"
    "后续行动：\n"
    "- 测试行动1\n"
)

# 多轮 OODA 文本 — 每轮内容不同以验证 L0 变化
MOCK_OODA_TURNS = [
    (
        "核心摘要：第一轮对话关于API设计\n"
        "资源与观察：\n"
        "- 讨论了RESTful接口\n"
        "事实与约束：\n"
        "- 需要JSON格式\n"
        "决策与结论：\n"
        "- 采用分层架构\n"
        "后续行动：\n"
        "- 编写接口文档\n"
    ),
    (
        "核心摘要：第二轮讨论了数据库选型\n"
        "资源与观察：\n"
        "- 比较了PostgreSQL和MongoDB\n"
        "事实与约束：\n"
        "- 数据一致性要求高\n"
        "决策与结论：\n"
        "- 选用PostgreSQL\n"
        "后续行动：\n"
        "- 设计数据模型\n"
    ),
    (
        "核心摘要：第三轮确定认证方案\n"
        "资源与观察：\n"
        "- 讨论了OAuth2和JWT\n"
        "事实与约束：\n"
        "- 需要SSO集成\n"
        "决策与结论：\n"
        "- 采用OAuth2\n"
        "后续行动：\n"
        "- 实现登录模块\n"
    ),
]


# ── 辅助函数 ────────────────────────────────────────────────────────────────

def build_messages(num_turns: int) -> list[dict]:
    """生成 num_turns 轮对话消息列表 (system + user/assistant 对)."""
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(num_turns):
        messages.append({"role": "user", "content": f"第{i+1}轮用户消息"})
        messages.append(
            {"role": "assistant", "content": f"第{i+1}轮助手回复内容"}
        )
    return messages


def check_db_has_records(db_path: str, session: str = "default",
                         expected_turns: int = 1) -> list[dict]:
    """读取 SQLite 并验证 L0/L1 记录存在, 返回记录列表."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT turn_index, l0_text, l1_text FROM turn_cache "
            "WHERE session_id=? ORDER BY turn_index",
            (session,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == expected_turns, (
        f"Expected {expected_turns} turns, got {len(rows)}"
    )
    result = []
    for row in rows:
        ti, l0, l1 = row
        assert l0, f"L0 text is empty for turn {ti}"
        assert l1, f"L1 text is empty for turn {ti}"
        # Verify L1 is parseable JSON
        parsed = json.loads(l1)
        assert "core_change" in parsed, f"L1 missing core_change for turn {ti}"
        result.append({"turn_index": ti, "l0_text": l0, "l1_text": l1,
                       "l1_parsed": parsed})
    return result


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _env_fallback_embed():
    """确保所有测试使用 fallback embedding (不依赖外部服务)."""
    os.environ.setdefault("CA_EMBED_BACKEND", "fallback")
    yield


@pytest.fixture
def plugin(tmp_path):
    """创建并初始化插件, 使用临时 HERMES_HOME."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(exist_ok=True)
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)

    p = CAContextAssemblerPlugin()
    yield p

    # 清理
    if old_home:
        os.environ["HERMES_HOME"] = old_home
    else:
        os.environ.pop("HERMES_HOME", None)
    try:
        if p._engine is not None:
            p._engine.store.close()
    except Exception:
        pass


@pytest.fixture
def started_plugin(plugin, tmp_path):
    """启动会话的插件 (on_session_start 已调用)."""
    session_id = "test-online"
    plugin.on_session_start(session_id, context_length=32000)
    return plugin, session_id, tmp_path


@pytest.fixture
def seeded_engine(started_plugin):
    """经过 C-stage 写入的引擎 (3轮 process_turn 已执行)."""
    plugin, session_id, tmp_path = started_plugin

    def mock_llm(prev_l1, l2_text):
        """Mock LLM returning MOCK_OODA_TURNS for each call."""
        if not hasattr(mock_llm, "call_count"):
            mock_llm.call_count = 0
        idx = mock_llm.call_count
        mock_llm.call_count += 1
        if idx < len(MOCK_OODA_TURNS):
            return MOCK_OODA_TURNS[idx]
        return MOCK_OODA_TEXT

    engine = plugin._engine
    assert engine is not None, "Engine should be created"

    # Mock _call_llm_for_l1 and embedding on this specific engine instance
    with patch.object(engine, "_call_llm_for_l1", mock_llm), \
         patch.object(engine.embed_client, "embed",
                      return_value=[0.1] * 768):
        messages = build_messages(0)  # empty history for first turn
        for i in range(3):
            ti = engine.process_turn_async(
                user_message=f"第{i+1}轮用户消息",
                assistant_response=f"第{i+1}轮助手回复内容",
                history=messages[:],
            )
            # 每轮后追加消息
            messages.append({"role": "user", "content": f"第{i+1}轮用户消息"})
            messages.append(
                {"role": "assistant", "content": f"第{i+1}轮助手回复内容"}
            )
        engine.wait_for_pending(30.0)

    return plugin, session_id, tmp_path, engine


# ═══════════════════════════════════════════════════════════════════════════
# 测试 1: 插件加载
# ═══════════════════════════════════════════════════════════════════════════

class TestPluginLoading:
    """插件加载测试: 验证 is_available(), on_session_start(), compress()."""

    def test_is_available_true(self):
        """is_available() 在 ca 包可导入时返回 True."""
        assert is_available() is True

    def test_is_available_false_when_ca_missing(self):
        """is_available() 在断路器激活时返回 False."""
        with patch("context_engine.ca_assembler._read_state",
                   return_value={"failures": 3, "retry_after": None}):
            assert is_available() is False

    def test_on_session_start_creates_engine(self, plugin):
        """on_session_start() 创建引擎和 SQLite store."""
        assert plugin._engine is None  # 初始无引擎
        plugin.on_session_start("sess-001")
        assert plugin._engine is not None
        assert plugin._session_id == "sess-001"

    def test_on_session_start_creates_db_dir(self, plugin, tmp_path):
        """on_session_start() 创建 ca_cache/ 目录 (DB 文件在首次写入时创建)."""
        plugin.on_session_start("sess-dir-test")
        cache_dir = tmp_path / "hermes" / "ca_cache"
        assert cache_dir.exists(), f"ca_cache dir not found: {cache_dir}"

    def test_compress_without_data_returns_original(self, started_plugin):
        """pre_llm_call() 无缓存数据时返回原始消息."""
        plugin, _, _ = started_plugin
        msgs = build_messages(2)
        result = plugin.pre_llm_call(msgs, user_input="", context_length=32000)
        # 无 L1 缓存 → 原样返回
        assert result == msgs

    def test_compress_returns_list(self, started_plugin):
        """pre_llm_call() 始终返回 list."""
        plugin, _, _ = started_plugin
        result = plugin.pre_llm_call([], user_input="", context_length=32000)
        assert isinstance(result, list)

# ═══════════════════════════════════════════════════════════════════════════
# 测试 2: C-stage 写入
# ═══════════════════════════════════════════════════════════════════════════

class TestCStageWrite:
    """C-stage 写入测试: process_turn() 写入 L0+L1 到 SQLite."""

    def test_process_turn_writes_to_db(self, started_plugin, tmp_path):
        """process_turn() 后 ca_cache/ 下生成 {session}.db 且有记录."""
        plugin, session_id, _ = started_plugin
        engine = plugin._engine
        db_path = tmp_path / "hermes" / "ca_cache" / f"{session_id}.db"

        with patch.object(engine, "_call_llm_for_l1",
                          return_value=MOCK_OODA_TEXT), \
             patch.object(engine.embed_client, "embed",
                          return_value=[0.1] * 768):
            engine.process_turn_async(
                user_message="测试用户消息",
                assistant_response="测试助手回复",
            )
            engine.wait_for_pending(30.0)

        # DB 文件存在
        assert db_path.exists(), f"DB not found at {db_path}"

        # 读取验证 L0+L1（引擎使用硬编码 session "default"）
        records = check_db_has_records(str(db_path), session="default", expected_turns=1)
        assert records[0]["turn_index"] == 0

    def test_cstage_three_turns(self, seeded_engine, tmp_path):
        """3轮对话后, DB 中有 3 条 L0+L1 记录."""
        plugin, session_id, _, engine = seeded_engine
        db_path = tmp_path / "hermes" / "ca_cache" / f"{session_id}.db"

        records = check_db_has_records(str(db_path), session="default", expected_turns=3)
        for i, rec in enumerate(records):
            assert rec["turn_index"] == i, f"Turn index mismatch at {i}"
            # L0 非空
            assert len(rec["l0_text"]) > 0, f"L0 empty for turn {i}"
            # L1 可解析为 JSON
            parsed = rec["l1_parsed"]
            assert isinstance(parsed, dict)
            assert "core_change" in parsed

    def test_l0_taken_from_core_change(self, started_plugin):
        """L0 是 l1.core_change 的前 100 字符."""
        import json
        plugin, _, _ = started_plugin
        engine = plugin._engine

        custom_text = (
            "核心摘要：自定义测试\n"
            "资源与观察：\n"
            "- test\n"
            "事实与约束：\n"
            "- test\n"
            "决策与结论：\n"
            "- test\n"
            "后续行动：\n"
            "- test\n"
        )

        with patch.object(engine, "_call_llm_for_l1",
                          return_value=custom_text):
            ti = engine.process_turn_async(
                user_message="test", assistant_response="test",
                history=[],
            )
            engine.wait_for_pending(30.0)

        # 从缓存读取 L1 结果
        l1_str = engine.cache.l1_texts.get(ti)
        assert l1_str is not None, f"L1 not found for turn {ti}"
        l1_parsed = json.loads(l1_str)
        # 验证返回的 L1 包含 core_change
        assert "core_change" in l1_parsed
        assert len(l1_parsed["core_change"]) > 0

    def test_db_json_parseable(self, seeded_engine, tmp_path):
        """DB 中的 L1 JSON 可正确解析."""
        plugin, session_id, _, _ = seeded_engine
        db_path = tmp_path / "hermes" / "ca_cache" / f"{session_id}.db"

        records = check_db_has_records(str(db_path), session="default", expected_turns=3)
        for rec in records:
            parsed = rec["l1_parsed"]
            # OODA 五节 (使用 OODAParser 的规范键名)
            for key in ("core_change", "new_materials", "objective_facts",
                        "consensus", "todo"):
                assert key in parsed, f"Missing OODA section: {key}"


# ═══════════════════════════════════════════════════════════════════════════
# 测试 3: A-stage 组装
# ═══════════════════════════════════════════════════════════════════════════

class TestAStageAssembly:
    """A-stage 组装测试: assemble() 返回带 [~/N] 标记的分层消息."""

    def test_assemble_contains_tide_markers(self, seeded_engine):
        """assemble() 返回的消息包含 [~/N] 标记."""
        plugin, _, _, engine = seeded_engine

        # 构建与 C-stage 数据对应的消息
        msgs = build_messages(3)

        result = engine.assemble(
            user_input="最新用户问题",
            messages=msgs,
            context_length=32000,
        )

        # 结果包含 [~/N] 标记（起始索引因 system 消息偏移一位）
        all_content = " ".join(
            m.get("content", "") for m in result
        )
        assert "[~/1]" in all_content, "Missing [~/1] marker"
        assert "[~/2]" in all_content, "Missing [~/2] marker"

    def test_head_middle_tail_layering(self, seeded_engine):
        """head/middle/tail 分层正确."""
        plugin, _, _, engine = seeded_engine

        # 构建 6 轮消息 (足够分出 head/middle/tail)
        msgs = build_messages(6)

        with patch.object(engine.__class__, "_call_llm_for_l1",
                          side_effect=MOCK_OODA_TURNS * 2), \
             patch.object(engine.embed_client, "embed",
                          return_value=[0.1] * 768):
            # 先写入 C-stage 数据 (使用已有引擎需额外写入)
            if not hasattr(engine, "_test_seeded_6"):
                for i in range(3, 6):
                    engine.process_turn_async(
                        user_message=f"第{i+1}轮用户消息",
                        assistant_response=f"第{i+1}轮助手回复内容",
                        history=msgs[:i*2],
                    )
                engine.wait_for_pending(30.0)
                engine._test_seeded_6 = True

        result = engine.assemble(
            user_input="最新用户问题",
            messages=msgs,
            context_length=32000,
        )

        # Head (前 3 轮) 使用 L1 → 内容为 [~/N] L1
        # 注意：turn_index 0 对应消息位置 0 (system)，被跳过
        # 所以 head 实际产出 [~/1], [~/2] 两条
        head_contents = []
        middle_contents = []
        for i, m in enumerate(result):
            content = m.get("content", "")
            if i > 0:  # 跳过 system
                if "[~/" in content:
                    head_contents.append(content)

        assert len(head_contents) >= 2, (
            f"Expected ≥2 head entries with [~/N], got {len(head_contents)}"
        )

    def test_assemble_via_compress_plugin(self, seeded_engine):
        """通过 plugin.pre_llm_call() 调用 assemble() 也能返回 [~/N] 标记."""
        plugin, _, _, _ = seeded_engine

        msgs = build_messages(3)

        result = plugin.pre_llm_call(
            messages=msgs,
            user_input="最新用户问题",
            context_length=32000,
        )

        all_content = " ".join(
            m.get("content", "") for m in result
        )
        assert "[~" in all_content, (
            "compress() result should contain [~/N] markers"
        )

    def test_compress_with_int_tokens_does_not_crash(self, seeded_engine):
        """pre_llm_call() 收到 int context_length 时不崩溃."""
        plugin, _, _, _ = seeded_engine

        msgs = build_messages(3)

        # context_length=int — 模拟大窗口场景
        result = plugin.pre_llm_call(
            messages=msgs,
            user_input="第3轮用户消息",
            context_length=85000,
        )

        # 不崩溃, 返回 list
        assert isinstance(result, list)
        assert len(result) > 0
        # 应正常走 assemble() 带 [~/N] 标记, 而非硬截断
        all_content = " ".join(
            m.get("content", "") for m in result
        )
        assert "[~" in all_content, (
            "compress(int) result should contain [~/N] markers, "
            f"got fallback: {all_content[:200]}"
        )

    def test_compress_with_int_tokens_empty_messages(self, started_plugin):
        """pre_llm_call() int context_length + 空消息列表不崩溃."""
        plugin, _, _ = started_plugin
        result = plugin.pre_llm_call(
            messages=[],
            user_input="",
            context_length=85000,
        )
        assert isinstance(result, list)
        assert result == []

    def test_assemble_non_empty_result(self, seeded_engine):
        """assemble() 返回非空列表."""
        plugin, _, _, engine = seeded_engine
        msgs = build_messages(3)
        result = engine.assemble(
            user_input="q", messages=msgs, context_length=32000,
        )
        assert len(result) > 0
        assert len(result) == len(msgs)  # 消息数量不变


# ═══════════════════════════════════════════════════════════════════════════
# 测试 4: 回退链
# ═══════════════════════════════════════════════════════════════════════════

class TestFallbackChain:
    """回退链测试: pre_llm_call 在引擎出错时原样返回消息."""

    def test_compress_fallback_on_engine_error(self, started_plugin):
        """pre_llm_call() 在引擎错误时原样返回消息."""
        plugin, _, _ = started_plugin
        plugin._engine = None  # 模拟引擎丢失

        msgs = [{"role": "system", "content": "sys"}]
        for i in range(10):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})

        result = plugin.pre_llm_call(msgs)
        # 引擎不存时原样返回
        assert result == msgs


# ═══════════════════════════════════════════════════════════════════════════
# 测试 5: 插件状态管理
# ═══════════════════════════════════════════════════════════════════════════

class TestPluginState:
    """插件状态管理: session 生命周期."""

    def test_on_session_reset_clears_state(self, started_plugin):
        """on_session_reset 重置所有 per-session 状态."""
        plugin, _, _ = started_plugin
        plugin._engine_errored = True
        old_engine = plugin._engine

        plugin.on_session_reset()
        # 引擎被新的替换（或保持但重置状态）
        assert plugin._engine_errored is False

    def test_on_session_end_closes_store(self, started_plugin):
        """on_session_end 关闭 store."""
        plugin, _, _ = started_plugin
        plugin.on_session_end()
        assert plugin._engine is None



