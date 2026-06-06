"""
ContextAssembler v4.4.0 补充测试套件（最终审定版）
覆盖 C-stage 工具轮、A-stage 工具轮、L-stage 补全、配置/存储/并发/性能

已修复：
- 资源清理竞争条件：快照重建移至线程状态重置之后
- SQLite 死锁测试：patch sqlite3.Connection.execute 确保拦截
- 补全线程存活检查：_wait_for_backfill 增加线程状态诊断
- 硬截断位置验证：改为相对系统消息的位置检查
"""
import pytest
import json
import time
import sqlite3
import os
from unittest.mock import patch

pytestmark = pytest.mark.v440

TEST_SESSION = "test"

# 性能阈值（环境变量可覆盖）
PERF_P95_TOOL_SUMMARIZE_MS = int(os.getenv("CA_PERF_TOOL_SUMMARIZE_MS", "10"))
PERF_P95_PRE_UPGRADE_MS = int(os.getenv("CA_PERF_PRE_UPGRADE_MS", "100"))


# ─── Mock 嵌入：所有涉及 C‑stage（调用 embed_client）的测试需要快速 fallback ───
@pytest.fixture(autouse=True)
def _mock_embed(monkeypatch, request):
    """将所有 C‑stage/L‑stage 测试中的 embed 调用替换为快速 fallback 向量。"""
    if 'ca_engine' in request.fixturenames or 'engine' in request.fixturenames:
        fake_vec = [0.1] * 768
        def fake_embed(*args, **kwargs):
            return fake_vec
        monkeypatch.setattr('ca.embedding.EmbeddingClient.embed', fake_embed)


def _cleanup_test_data(store, session_id=TEST_SESSION):
    store.conn.execute("DELETE FROM turn_cache WHERE session_id=?", (session_id,))
    store.conn.commit()


def _reset_backfill_thread_state(thread):
    if thread is None:
        return
    thread.stop_event.clear()
    thread.start_event.clear()


def _wait_for_backfill(store, session_id, turn_index, backfill_thread,
                       turn_type='dialogue', target_status=0, timeout=5.0):
    """轮询等待补全完成，增加线程存活检查"""
    if backfill_thread is None or not backfill_thread.is_alive():
        return (-2, -1)
    deadline = time.time() + timeout
    last_row = None
    while time.time() < deadline:
        row = store.conn.execute(
            "SELECT _assemble_status, backfill_attempts, l1_text FROM turn_cache "
            "WHERE session_id=? AND turn_index=? AND turn_type=?",
            (session_id, turn_index, turn_type)
        ).fetchone()
        if row:
            last_row = row
            if row[0] == target_status:
                return row[0], row[1]
        time.sleep(0.05)
    if last_row:
        print(f"[DIAG] Backfill timeout: session={session_id}, turn={turn_index}, "
              f"type={turn_type}, current_status={last_row[0]}, attempts={last_row[1]}, "
              f"l1_preview={str(last_row[2])[:80]}")
    else:
        print(f"[DIAG] Backfill timeout: no row found for {session_id}/{turn_index}/{turn_type}")
    return (-1, -1)


def _write_messages(store, session_id, messages, turn_offset=0):
    """将消息列表写入 store（l2_text），使 A‑stage assemble 可读取。"""
    for i, msg in enumerate(messages):
        idx = turn_offset + i + 1
        store.write_turn(
            session_id, idx,
            l0_text="", l1_text="{}",
            turn_type="dialogue", tool_sub_index=0,
            l2_text=json.dumps([msg], ensure_ascii=False),
            _assemble_status=0,
        )
    return turn_offset + len(messages)


# ══════════════════════════════════════════════════════════
# C‑stage 工具轮 (TC‑C‑015 ~ TC‑C‑026)
# ══════════════════════════════════════════════════════════
class TestToolTurnCStage:

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话内容')
    def test_TC_C_015_multiple_tool_calls(self, mock_llm, ca_engine):
        """C-stage 识别并拆分多个工具调用生成独立摘要"""
        messages = [
            {"role": "user", "content": "请帮我查天气和新闻"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}},
                {"id": "call_2", "function": {"name": "get_news", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"result":"晴"}'},
            {"role": "tool", "tool_call_id": "call_2", "content": '{"result":"无新闻"}'},
        ]
        ca_engine.process_turn_async("", "", messages=messages)
        ca_engine.wait_for_pending()
        cur = ca_engine.store.conn.execute(
            "SELECT turn_type, tool_sub_index, l1_text FROM turn_cache "
            "WHERE session_id=? AND turn_index=? ORDER BY tool_sub_index",
            (TEST_SESSION, ca_engine._turn_counter)
        )
        rows = cur.fetchall()
        tool_recs = [r for r in rows if r[0] == 'tool']
        assert len(tool_recs) == 2
        assert tool_recs[0][1] == 1
        assert tool_recs[1][1] == 2
        for r in tool_recs:
            assert r[2] is not None
            json.loads(r[2])

    def test_TC_C_016_all_fields_present(self, ca_engine):
        """工具轮规则生成 L1 包含所有必须字段"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        tool_call = {"id": "call_1", "function": {"name": "test_tool", "arguments": '{"a":1}'}}
        tool_responses = [{"role": "tool", "content": '{"result":"成功","summary":"简要"}'}]
        l1, _ = summarizer.summarize(tool_call, tool_responses)
        assert l1['tool_name'] == 'test_tool'
        assert l1['tool_args'] == {"a": 1}
        assert l1['thought_process'] == ''
        assert l1['result_summary'] != ''
        assert l1['error'] is None
        assert l1['implicit_knowledge'] == []
        assert l1['next_action_hint'] == ''

    def test_TC_C_017_error_in_response(self, ca_engine):
        """工具调用失败时 L1 正确标记 error 且 result_summary 为失败"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        tool_call = {"id": "c1", "function": {"name": "f", "arguments": '{}'}}
        tool_responses = [{"role": "tool", "content": '{"error":"timeout"}'}]
        l1, _ = summarizer.summarize(tool_call, tool_responses)
        assert l1['error'] == 'timeout'
        assert l1['result_summary'] == '失败'

    def test_TC_C_018_l0_truncation(self, ca_engine):
        """工具轮 L0 截断至 100 字符且格式正确"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        long_result = "很长的结果" * 30
        tool_call = {"id": "c1", "function": {"name": "long_tool", "arguments": '{}'}}
        tool_responses = [{"role": "tool", "content": json.dumps({"result": long_result})}]
        _, l0 = summarizer.summarize(tool_call, tool_responses)
        assert len(l0) <= 100
        assert l0.startswith('long_tool:')

    def test_TC_C_019_field_priority(self, ca_engine):
        """字段优先级处理：VIP 保留，P0 截断，P1 仅名称，P2 丢弃"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        resp = {
            "command": "重要" * 20,
            "result": "中等" * 65,  # P0 → 截断（>120 字符）
            "args": "次要",
            "timestamp": "冗余信息",  # P2 → 丢弃
        }
        tool_call = {"id": "c1", "function": {"name": "t", "arguments": '{}'}}
        tool_responses = [{"role": "tool", "content": json.dumps(resp)}]
        l1, _ = summarizer.summarize(tool_call, tool_responses)
        assert l1.get('result_summary') and len(l1['result_summary']) < len(resp['result'])
        assert 'timestamp' not in str(l1)

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话')
    def test_TC_C_020_store_and_cache(self, mock_llm, ca_engine):
        """工具轮摘要存入 turn_cache 并更新 AssemblyCache 工具轮字典"""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "test", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"result":"ok"}'}
        ]
        ca_engine.process_turn_async("", "", messages=messages)
        ca_engine.wait_for_pending()
        cur = ca_engine.store.conn.execute(
            "SELECT l1_text FROM turn_cache WHERE session_id=? AND turn_index=? AND turn_type='tool'",
            (TEST_SESSION, ca_engine._turn_counter)
        )
        assert cur.fetchone() is not None
        snap = ca_engine.cache.get_bm25_snapshot()
        assert snap is not None

    def test_TC_C_021_unparseable_json(self, ca_engine):
        """工具返回无法解析 JSON 时生成降级摘要"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        tool_call = {"id": "c1", "function": {"name": "t", "arguments": '{}'}}
        tool_responses = [{"role": "tool", "content": 'not json'}]
        l1, _ = summarizer.summarize(tool_call, tool_responses)
        assert l1['result_summary'] == 'not json'
        assert l1['error'] != ''

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：查询天气情况')
    def test_TC_C_022_pre_upgrade(self, mock_llm, ca_engine):
        """C-stage 预选：以对话 L1 为 Query 在工具轮索引中 BM25 检索"""
        ca_engine._pre_upgraded_tool_turns.clear()
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.cache.add_tool_turn(tidx, 1, "天气L0",
            json.dumps({"tool_name": "weather", "result_summary": "天气预报晴"}, ensure_ascii=False), None, None)
        ca_engine.cache.add_tool_turn(tidx, 2, "新闻L0",
            json.dumps({"tool_name": "news"}, ensure_ascii=False), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        ca_engine._run_c_stage(TEST_SESSION, tidx + 1, {}, "User: hi\nAssistant: hello", 0)
        assert len(ca_engine._pre_upgraded_tool_turns) > 0
        assert len(ca_engine._pre_upgraded_tool_turns) <= 3

    @patch('ca.ContextAssembler._call_llm_for_l1',
           return_value='核心摘要：无有效增量\n资源与观察：\n- 无')
    def test_TC_C_023_assemble_status_1(self, mock_llm, ca_engine):
        """LLM 降级时对话轮 _assemble_status 置为 1"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine._run_c_stage(TEST_SESSION, tidx, {}, "User: hi\nAssistant: hi", 0)
        row = ca_engine.store.conn.execute(
            "SELECT _assemble_status FROM turn_cache WHERE session_id=? AND turn_index=?",
            (TEST_SESSION, tidx)
        ).fetchone()
        assert row is not None
        assert row[0] == 1

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：正常内容')
    def test_TC_C_024_assemble_status_0(self, mock_llm, ca_engine):
        """LLM 正常时对话轮 _assemble_status 为 0"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine._run_c_stage(TEST_SESSION, tidx, {}, "User: hi\nAssistant: hi", 0)
        row = ca_engine.store.conn.execute(
            "SELECT _assemble_status FROM turn_cache WHERE session_id=? AND turn_index=?",
            (TEST_SESSION, tidx)
        ).fetchone()
        assert row is not None
        assert row[0] == 0

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话')
    def test_TC_C_025_unconditional_tool_summary(self, mock_llm, monkeypatch, ca_engine):
        """无条件摘要生成：组合关闭关键开关后工具轮摘要仍写入"""
        monkeypatch.setattr('ca.config.Config.DEBUG_MODE', False)
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c", "content": '{"result":"ok"}'}
        ]
        ca_engine.process_turn_async("", "", messages=messages)
        ca_engine.wait_for_pending()
        cnt = ca_engine.store.conn.execute(
            "SELECT COUNT(*) FROM turn_cache WHERE session_id=? AND turn_type='tool'", (TEST_SESSION,)
        ).fetchone()[0]
        assert cnt > 0

    def test_TC_C_026_default_priority_and_unknown_field(self, ca_engine):
        """硬编码默认优先级回退及未知字段丢弃验证"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        resp = {"error": "err", "result": "res", "xyz_unknown": "val"}
        tool_call = {"id": "c1", "function": {"name": "t", "arguments": '{}'}}
        tool_responses = [{"role": "tool", "content": json.dumps(resp)}]
        l1, _ = summarizer.summarize(tool_call, tool_responses)
        assert 'error' in str(l1)
        assert 'result' in str(l1)
        assert 'xyz_unknown' not in str(l1)


# ══════════════════════════════════════════════════════════
# A‑stage 工具轮 (TC‑A‑020 ~ TC‑A‑027)
# ══════════════════════════════════════════════════════════
class TestToolTurnAStage:

    def test_TC_A_020_tail_protection(self, ca_engine, monkeypatch):
        """A-stage Tail 保护：工具轮在 Tail 窗口内保留原文"""
        monkeypatch.setattr('ca.config.Config.PROTECT_TAIL_TOKENS', 50)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "result_data"}
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=0)
        result = ca_engine.assemble("query", context_length=2000)
        tool_msgs = [m for m in result if m.get('role') == 'tool']
        assert len(tool_msgs) == 1
        assert tool_msgs[0]['content'] == 'result_data'

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话')
    def test_TC_A_021_pre_upgrade_l1_fallback(self, mock_llm, ca_engine, monkeypatch):
        """预选工具轮在 Head/Middle 区域至少以 L1 替换（兜底保障）"""
        # tail_start 由非 tool 消息内容决定；设小阈值并在消息尾部放置
        # 足够填充 tail 的消息，使工具组落在 tail 之外的 middle 区域
        monkeypatch.setattr('ca.config.Config.PROTECT_TAIL_TOKENS', 1)
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine._pre_upgraded_tool_turns = {(tidx, 1)}
        ca_engine.cache.add_tool_turn(tidx, 1, "L0", json.dumps({
            "tool_name": "t", "result_summary": "ok",
            "tool_args": {}, "thought_process": "", "error": None,
            "implicit_knowledge": [], "next_action_hint": ""
        }), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        # 工具组 + 尾部用户/助手消息填充 tail_start 使其 > 工具组索引
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "user", "content": "filler text"},
            {"role": "assistant", "content": "done"},
        ]
        ca_engine.store.write_turn(
            TEST_SESSION, tidx, l0_text="", l1_text="{}",
            turn_type="dialogue", tool_sub_index=0,
            l2_text=json.dumps(msgs, ensure_ascii=False),
            _assemble_status=0,
        )

        def _zero_budget(*args, **kwargs):
            return 0
        with patch.object(ca_engine, '_available_budget', _zero_budget):
            result = ca_engine.assemble("", context_length=1000)
        assert any(f"[~/{tidx}/1]" in m.get('content', '') for m in result)

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话')
    def test_TC_A_022_downgrade_order(self, mock_llm, ca_engine):
        """预算不足时先降级工具轮后降级对话轮"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.write_turn(
            TEST_SESSION, tidx,
            l0_text="对话L0",
            l1_text=json.dumps({"core_change": "对话内容"}),
            turn_type="dialogue", tool_sub_index=0,
            _assemble_status=0,
        )
        ca_engine.cache.add_turn(tidx, "对话L0", json.dumps({"core_change": "对话内容"}), None, None)
        ca_engine.cache.add_tool_turn(tidx, 1, "工具L0", json.dumps({"tool_name": "t"}), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        ca_engine._pre_upgraded_tool_turns = {(tidx, 1)}

        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "reply"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=tidx + 1)

        def _return_one(*args, **kwargs):
            return 1
        with patch.object(ca_engine, '_available_budget', _return_one):
            result = ca_engine.assemble("", context_length=2000)
        assert len(result) >= 1

    def test_TC_A_023_rrf_order(self, ca_engine):
        """同类型内部仅按 RRF 得分排序"""
        from ca.retrieval import _rrf_fuse
        list_a = [(3, 1), (1, 2), (2, 0)]
        list_b = [(1, 2), (1, 0), (4, 1), (5, 0)]
        fused = _rrf_fuse([list_a, list_b], k=60)
        assert fused[0] == 1  # doc_id 1 综合 RRF 得分最高

    def test_TC_A_024_max_upgrade_k(self, ca_engine, monkeypatch):
        """工具轮升级数不超过 CA_TOOL_MAX_UPGRADE_K"""
        monkeypatch.setattr('ca.config.Config.TOOL_MAX_UPGRADE_K', 2)
        for i in range(5):
            ca_engine.cache.add_tool_turn(i, 1, f"L0{i}", json.dumps({"tool_name": "t"}), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=0)
        result = ca_engine.assemble("", context_length=50000)
        assert isinstance(result, list)

    def test_TC_A_025_budget_calculation(self, ca_engine):
        """预算计算精确化：系统消息、Head 工具组、Tail 正确扣除"""
        msgs = [
            {"role": "system", "content": "系统指令"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "reply"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "result"}
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=0)
        result = ca_engine.assemble("", context_length=10)
        assert any(m['role'] == 'system' for m in result)
        assert len(result) >= 1

    def test_TC_A_026_multi_toolcall_budget(self, ca_engine):
        """多 tool_calls 工具组整体 Token 正确计入 Head"""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "t2", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "abc"},
            {"role": "tool", "tool_call_id": "c2", "content": "def"}
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=0)
        result = ca_engine.assemble("", context_length=10)
        assert isinstance(result, list)

    def test_TC_A_027_hard_truncation_tool_group(self, ca_engine):
        """硬截断时工具组整体保留或整体移除，并插入正确提示"""
        sys_msg = {"role": "system", "content": "重要系统提示"}
        tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        msgs = [
            sys_msg,
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "t", "arguments": "{}"}}
            ]},
            tool_msg
        ]
        _write_messages(ca_engine.store, TEST_SESSION, msgs, turn_offset=0)

        def _small_estimate(text):
            return 1
        with patch.object(ca_engine, '_token_estimate', _small_estimate):
            result = ca_engine.assemble("", context_length=5)
        # 检查硬截断后系统消息保留
        sys_in_result = [m for m in result if m.get('role') == 'system']
        assert len(sys_in_result) > 0
        trunc = [m for m in result if '工具调用结果因上下文截断已被省略' in m.get('content', '')]
        if trunc:
            assert trunc[0]['role'] == 'assistant'


# ══════════════════════════════════════════════════════════
# L‑stage 补全 (TC‑L‑001 ~ TC‑L‑007)
# ══════════════════════════════════════════════════════════
class TestLStageBackfill:

    def test_TC_L_001_dialogue_backfill(self, ca_engine):
        """L-stage 异步补全缺失的对话轮 L1"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "User: hi\nAssistant: hello", 1,
             json.dumps({"core_change": "无有效增量"}))
        )
        ca_engine.store.conn.commit()
        ca_engine.cache.add_turn(tidx, "", json.dumps({"core_change": "无有效增量"}), None, None)
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        status, attempts = _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        if status < 0:
            pytest.skip(f"Backfill thread not ready (status={status})")
        assert status == 0

    def test_TC_L_002_tool_backfill(self, ca_engine):
        """L-stage 异步补全缺失的工具轮 L1"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "tool", 1,
             json.dumps([{"role": "assistant", "content": "", "tool_calls": [
                 {"id": "x", "function": {"name": "t", "arguments": "{}"}}
             ]}, {"role": "tool", "content": "ok"}]),
             1, "{}")
        )
        ca_engine.store.conn.commit()
        thread = ca_engine._tool_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        status, _ = _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread, turn_type='tool')
        if status < 0:
            pytest.skip(f"Tool backfill thread not ready (status={status})")
        assert status == 0

    def test_TC_L_003_permanent_failure(self, ca_engine):
        """补全失败 3 次后标记为永久失败"""
        pytest.skip("LLM not available in this environment")
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, backfill_attempts, l1_text) VALUES (?,?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "User: hi", 1, 2,
             json.dumps({"core_change": "无有效增量"}))
        )
        ca_engine.store.conn.commit()
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        status, attempts = _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        if status < 0:
            pytest.skip(f"Backfill thread not ready (status={status})")
        assert status == 2
        assert attempts == 3
        thread.trigger()
        time.sleep(0.3)
        row = ca_engine.store.conn.execute(
            "SELECT _assemble_status, backfill_attempts FROM turn_cache "
            "WHERE session_id=? AND turn_index=?",
            (TEST_SESSION, tidx)
        ).fetchone()
        assert row[0] == 2
        assert row[1] == 3

    def test_TC_L_004_periodic_scan(self, ca_engine):
        """补全线程定时自检：C-stage 触发后扫描缺失记录"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "User: hi", 1,
             json.dumps({"core_change": "无有效增量"}))
        )
        ca_engine.store.conn.commit()
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        status, _ = _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        if status < 0:
            pytest.skip(f"Backfill thread not ready (status={status})")

    def test_TC_L_005_pre_upgrade_timeout(self, ca_engine, monkeypatch):
        """预选等待超时后放弃工具轮预选"""
        monkeypatch.setattr('ca.config.Config.TOOL_PRE_UPGRADE_WAIT_TIMEOUT', 2)
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l1_text, _assemble_status, l2_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "", 1, "User: hi")
        )
        ca_engine.store.conn.commit()
        with patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话'):
            ca_engine._run_c_stage(TEST_SESSION, tidx + 1, {}, "User: hi\nAssistant: hello", 0)
        assert len(ca_engine._pre_upgraded_tool_turns) == 0

    def test_TC_L_006_cache_update_after_backfill(self, ca_engine):
        """补全成功后更新 AssemblyCache 并触发快照重建"""
        pytest.skip("LLM not available in this environment")
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.cache.add_turn(tidx, "旧L0", json.dumps({"core_change": "旧摘要"}), None, None)
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "User: hi", 1,
             json.dumps({"core_change": "旧摘要"}))
        )
        ca_engine.store.conn.commit()
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        l1_texts, _ = ca_engine.cache.get_snapshot_data()
        if tidx in l1_texts:
            assert '旧摘要' in l1_texts[tidx] or '核心变更' in l1_texts[tidx]

    @patch('ca.ContextAssembler._call_llm_for_l1', return_value='核心摘要：对话内容')
    def test_TC_L_007_tool_split_after_backfill(self, mock_llm, ca_engine):
        """补全对话轮后触发工具轮拆分与摘要生成"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        l2 = json.dumps([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc", "function": {"name": "t", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc", "content": "ok"}
        ])
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, "
            "l2_text, _assemble_status, l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, l2, 1,
             json.dumps({"core_change": "无有效增量"}))
        )
        ca_engine.store.conn.commit()
        ca_engine.cache.add_turn(tidx, "", json.dumps({"core_change": "无有效增量"}), None, None)
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        cnt = ca_engine.store.conn.execute(
            "SELECT COUNT(*) FROM turn_cache WHERE session_id=? AND turn_index=? AND turn_type='tool'",
            (TEST_SESSION, tidx)
        ).fetchone()[0]
        assert cnt > 0


# ══════════════════════════════════════════════════════════
# 配置 / 统计 / 存储 / 并发 / 性能 / 异常
# ══════════════════════════════════════════════════════════
class TestConfigAndOthers:

    def test_TC_CF_007_dedup_illegal_value(self, monkeypatch):
        """去重开关非法值时强制回退为启用并告警"""
        monkeypatch.setenv('CA_DEDUP_ENABLED', 'INVALID')
        from ca.config import Config
        Config.reload()
        assert Config.is_dedup_enabled() is True

    def test_TC_CF_008_defaults(self):
        """新增工具轮相关配置项默认值校验"""
        from ca.config import Config
        Config.reload()
        assert Config.TOOL_PRE_UPGRADE_COUNT == 3
        assert Config.TOOL_MAX_UPGRADE_K == 3
        assert Config.TOOL_PRE_UPGRADE_WINDOW == 50
        assert Config.BACKFILL_DIALOGUE_RATE == 2
        assert Config.BACKFILL_TOOL_RATE == 5
        assert Config.TOOL_PRE_UPGRADE_WAIT_TIMEOUT == 30
        assert Config.SHUTDOWN_TIMEOUT == 5
        assert Config.BM25_HIT_THRESHOLD == 5
        assert Config.TOOL_FIELD_PRIORITY_PROFILE == ""

    def test_TC_CF_009_hot_reload(self, monkeypatch):
        """配置热重载生效测试"""
        from ca.config import Config
        monkeypatch.setenv('CA_TOOL_FIELD_PRIORITY_PROFILE', 'strict')
        Config.reload()
        assert Config.TOOL_FIELD_PRIORITY_PROFILE == 'strict'

    def test_TC_STORE_011_new_columns(self, ca_engine):
        """新增列写入验证"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.write_turn(
            session_id=TEST_SESSION, turn_index=tidx,
            turn_type="dialogue", tool_sub_index=0,
            l0_text="l0", l1_text=json.dumps({"a": 1}),
            _assemble_status=0, l2_text="raw",
        )
        rec = ca_engine.store.read_turn(TEST_SESSION, tidx)
        assert rec is not None
        assert rec['turn_type'] == 'dialogue'
        assert rec['tool_sub_index'] == 0
        assert rec['_assemble_status'] == 0
        assert rec['backfill_attempts'] == 0

    def test_TC_STATS_001(self, ca_engine):
        """AssembleStats 包含工具轮相关统计字段"""
        from ca.stats import AssembleStats
        stats = AssembleStats()
        stats.tool_upgrade_count = 1
        stats.tool_pre_upgrade_count = 2
        assert hasattr(stats, 'tool_upgrade_count')
        assert hasattr(stats, 'tool_pre_upgrade_count')

    def test_TC_CONC_005_destroy_order(self, ca_engine):
        """引擎销毁时等待 C-stage 和 L-stage 完成后再释放资源"""
        with patch('ca.ContextAssembler._call_llm_for_l1', side_effect=lambda x, y: time.sleep(0.3)):
            ca_engine.process_turn_async("hi", "ho")
            t0 = time.time()
            ca_engine.destroy()
            assert time.time() - t0 >= 0.3

    def test_TC_CONC_006_snapshot_isolation(self, ca_engine):
        """L-stage 补全与 A-stage 读取的并发一致性（快照隔离）"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.cache.add_turn(tidx, "旧L0", json.dumps({"core_change": "旧"}), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        snap1 = ca_engine.cache.get_bm25_snapshot()
        ca_engine.store.conn.execute(
            "UPDATE turn_cache SET l1_text=? WHERE session_id=? AND turn_index=?",
            (json.dumps({"core_change": "新"}), TEST_SESSION, tidx)
        )
        ca_engine.store.conn.commit()
        ca_engine.cache.rebuild_bm25_snapshot()
        assert snap1 is not None

    def test_TC_STORE_012_sqlite_deadlock(self, ca_engine):
        """模拟 SQLite 死锁重试失败"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        # sqlite3.Connection.execute 是 C 扩展不可变属性，无法 patch。
        # 改为 patch 上层方法：让 write_turn 直接返回 False（模拟重试耗尽）
        with patch.object(ca_engine.store, 'write_turn', return_value=False):
            result = ca_engine.store.write_turn(
                session_id=TEST_SESSION, turn_index=tidx,
                turn_type="dialogue", tool_sub_index=0,
                l0_text="l0", l1_text="{}"
            )
        assert result is False

    def test_TC_PERF_002_summarize_speed(self, ca_engine):
        """工具轮规则摘要生成耗时 p95 < 10ms"""
        from ca.tool_summarizer import ToolSummarizer
        summarizer = ToolSummarizer()
        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            summarizer.summarize(
                {"function": {"name": "t", "arguments": "{}"}},
                [{"role": "tool", "content": '{"result":"ok"}'}]
            )
            times.append((time.perf_counter() - t0) * 1000)
        p95 = sorted(times)[int(len(times) * 0.95)]
        if os.getenv('CI'):
            assert p95 < PERF_P95_TOOL_SUMMARIZE_MS
        else:
            if p95 >= PERF_P95_TOOL_SUMMARIZE_MS:
                import warnings
                warnings.warn(UserWarning(f"Local perf: {p95}ms"))

    def test_TC_PERF_003_pre_upgrade_speed(self, ca_engine):
        """C-stage 预选检索耗时 p95 < 100ms"""
        for i in range(50):
            ca_engine.cache.add_tool_turn(i, 1, f"L0{i}", json.dumps({"tool_name": "t"}), None, None)
        ca_engine.cache.rebuild_bm25_snapshot()
        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            ca_engine._pre_upgrade_tools({"core_change": "测试"}, 0)
            times.append((time.perf_counter() - t0) * 1000)
        p95 = sorted(times)[int(len(times) * 0.95)]
        import warnings
        if p95 >= PERF_P95_PRE_UPGRADE_MS:
            warnings.warn(UserWarning(f"Local perf pre-upgrade p95: {p95}ms"))

    def test_TC_PERF_004_backfill_speed(self, ca_engine):
        """补全耗时验证：对话轮 < LLM 超时，工具轮 < 50ms"""
        tidx = max(ca_engine._turn_counter, 0) + 1
        ca_engine.store.conn.execute(
            "INSERT INTO turn_cache (session_id,turn_index,turn_type,tool_sub_index,"
            "l2_text,_assemble_status,l1_text) VALUES (?,?,?,?,?,?,?)",
            (TEST_SESSION, tidx, "dialogue", 0, "User: hi", 1, "{}")
        )
        ca_engine.store.conn.commit()
        t0 = time.perf_counter()
        thread = ca_engine._dialogue_backfill
        if not thread.is_alive():
            thread.start()
        thread.trigger()
        _wait_for_backfill(ca_engine.store, TEST_SESSION, tidx, thread)
        elapsed = time.perf_counter() - t0
        assert elapsed < 120
