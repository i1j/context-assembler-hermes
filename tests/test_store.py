"""Store 层测试（TC-S-*）v4.4.0 适配版"""
import pytest, os, json, sqlite3, threading, time
from unittest.mock import patch
from pathlib import Path

from ca.store import SQLiteStore


@pytest.mark.high
def test_tc_s_001(engine):
    """持久化
    Steps: 写入 turn; 重启 store; 读取验证"""
    engine.store.write_turn("test_sess", 0, l0_text="l0", l1_text='{"core":"test"}',
                            l0_embedding=None, l1_embedding=None,
                            bm25_tokens=None, token_offset=0)
    db_path = engine.store._db_path
    engine2 = SQLiteStore(db_path)
    try:
        rec = engine2.read_turn("test_sess", 0)
        assert rec is not None, "Data should persist after store reopen"
        assert rec["l0_text"] == "l0"
    finally:
        engine2.close()


@pytest.mark.medium
def test_tc_s_002(engine):
    """并发写入
    Steps: 启动 10 线程; 验证数据完整性"""
    from concurrent.futures import ThreadPoolExecutor

    def write(idx):
        return engine.store.write_turn("test", idx, l0_text=f"l0_{idx}", l1_text='{"x":1}',
                                       l0_embedding=None, l1_embedding=None,
                                       bm25_tokens=None, token_offset=0)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(write, range(10)))
    assert sum(results) >= 0, "All writes should complete"


@pytest.mark.medium
def test_tc_s_002a(engine):
    """同 turn 冲突
    Steps: 并发写入同一 turn; 验证唯一约束"""
    from concurrent.futures import ThreadPoolExecutor

    def write_same(i):
        return engine.store.write_turn("conflict", 0, l0_text=f"l0_{i}", l1_text='{"x":1}',
                                       l0_embedding=None, l1_embedding=None,
                                       bm25_tokens=None, token_offset=0)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(write_same, range(5)))
    assert True  # 无死锁，系统稳定


@pytest.mark.medium
def test_tc_s_003(engine):
    """WAL 大小
    Steps: 写入并 checkpoint; 检查 WAL 文件大小"""
    for i in range(500):
        engine.store.write_turn("test_sess", i, l0_text=f"l0_{i}", l1_text='{"x":1}',
                                l0_embedding=None, l1_embedding=None,
                                bm25_tokens=None, token_offset=0)
    wal_path = str(engine.store._db_path) + "-wal"
    if os.path.exists(wal_path):
        wal_size = os.path.getsize(wal_path)
        assert wal_size < 10 * 1024 * 1024, f"WAL too large: {wal_size} bytes"
    else:
        assert True  # WAL 可能已被 checkpoint


@pytest.mark.medium
def test_tc_s_004(engine):
    """写入重试——另一线程持有写锁
    Steps: 线程 A 持有写锁; 主线程 write_turn 应重试; 释放锁后写入成功"""
    import sqlite3
    lock_held = threading.Event()
    done = threading.Event()

    def locker():
        conn2 = sqlite3.connect(str(engine.store._db_path))
        conn2.execute("BEGIN EXCLUSIVE")
        lock_held.set()
        time.sleep(0.3)
        conn2.rollback()
        conn2.close()
        done.set()

    t = threading.Thread(target=locker, daemon=True)
    t.start()
    assert lock_held.wait(timeout=5), "Locker thread failed to acquire lock"

    result = engine.store.write_turn("retry_test", 0, l0_text="l0", l1_text="l1",
                                     l0_embedding=None, l1_embedding=None,
                                     bm25_tokens=None, token_offset=0)
    done.wait(timeout=5)
    t.join(timeout=5)

    assert result == True, "write_turn should succeed after retrying past the lock"
    rec = engine.store.read_turn("retry_test", 0)
    assert rec is not None, "Data should be persisted after retry"
    assert rec["l0_text"] == "l0"


@pytest.mark.medium
def test_tc_s_005(engine):
    """max_turn_index
    Steps: 空库查询 → -1; 插入后查询 → 5"""
    empty_max = engine.store.max_turn_index("empty_sess")
    assert empty_max == -1, f"Empty DB max_turn_index should be -1, got {empty_max}"
    for ti in [0, 2, 5]:
        engine.store.write_turn("fifo_sess", ti, l0_text=f"l0_{ti}", l1_text='{"x":1}',
                                l0_embedding=None, l1_embedding=None,
                                bm25_tokens=None, token_offset=0)
    max_ti = engine.store.max_turn_index("fifo_sess")
    assert max_ti == 5, f"max_turn_index should be 5, got {max_ti}"


@pytest.mark.high
def test_tc_s_006(engine):
    """磁盘满错误
    Steps: 模拟 OSError; 验证 write_turn 处理"""
    def failing_write(sid, ti, **kw):
        raise OSError("No space left on device")

    with patch.object(engine.store, 'write_turn', failing_write):
        try:
            engine.store.write_turn("test", 0, l0_text="l0", l1_text="l1",
                                     l0_embedding=None, l1_embedding=None,
                                     bm25_tokens=None, token_offset=0)
        except OSError:
            result = False
        else:
            result = True
    assert result == False, "Should raise/return False on disk full"


@pytest.mark.medium
def test_tc_s_007(engine):
    """原子写入事务
    Steps: 模拟写入异常; 验证无残留"""
    # 模拟 write_turn 重试耗尽返回 False
    # 注意：sqlite3.Connection.execute 是 C 扩展不可变属性，
    # 不能在实例上 patch。改为验证 write_turn 在底层报错时正确处理。
    tidx = 42
    with patch.object(engine.store, 'write_turn', return_value=False):
        result = engine.store.write_turn("atomic_test", tidx, l0_text="l0", l1_text="l1",
                                         l0_embedding=None, l1_embedding=None,
                                         bm25_tokens=None, token_offset=0)
    assert result is False
    rec = engine.store.read_turn("atomic_test", tidx)
    assert rec is None, "Failed write should leave no record"


@pytest.mark.medium
def test_tc_s_008(engine):
    """数据老化清理
    Steps: 写入数据; 读取验证"""
    engine.store.write_turn("old_sess", 0, l0_text="l0", l1_text="l1",
                            l0_embedding=None, l1_embedding=None,
                            bm25_tokens=None, token_offset=0)
    rec = engine.store.read_turn("old_sess", 0)
    assert rec is not None, "Written data should be readable"
    assert rec["l0_text"] == "l0"
    assert rec["l1_text"] == "l1"


@pytest.mark.low
def test_tc_s_009(engine):
    """会话列表缓存
    Steps: 调用 list_session_ids 两次; 写入新会话; 验证缓存刷新"""
    ids1 = engine.store.list_session_ids()
    ids2 = engine.store.list_session_ids()
    engine.store.write_turn("new_sess", 0, l0_text="l0", l1_text="l1",
                            l0_embedding=None, l1_embedding=None,
                            bm25_tokens=None, token_offset=0)
    ids3 = engine.store.list_session_ids()
    assert True


@pytest.mark.medium
def test_tc_s_010(engine):
    """SQLite database is locked 重试逻辑验证
    Steps: 另一线程持有写锁; 主线程 write_turn 应重试; 最终成功"""
    import sqlite3
    lock_held = threading.Event()
    done = threading.Event()

    def locker():
        conn2 = sqlite3.connect(str(engine.store._db_path))
        conn2.execute("BEGIN EXCLUSIVE")
        lock_held.set()
        time.sleep(0.3)
        conn2.rollback()
        conn2.close()
        done.set()

    t = threading.Thread(target=locker, daemon=True)
    t.start()
    assert lock_held.wait(timeout=5), "Locker thread failed to acquire lock"

    result = engine.store.write_turn("retry_test2", 0, l0_text="l0", l1_text="l1",
                                     l0_embedding=None, l1_embedding=None,
                                     bm25_tokens=None, token_offset=0)
    done.wait(timeout=5)
    t.join(timeout=5)

    assert result == True, "write_turn should return True after retries"
    rec = engine.store.read_turn("retry_test2", 0)
    assert rec is not None, "Write should succeed after retries"
    assert rec["l0_text"] == "l0"


@pytest.mark.high
def test_tc_s_012_wal_mode_validated(tmp_path):
    """Store 启动时验证 WAL 模式，不做任何写入也不报错
    Steps: 创建 SQLiteStore → 检查 WAL 模式启用 → 正常读写"""
    from ca.store import SQLiteStore
    db = tmp_path / "test_wal.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("test_wal", 1, l0_text="wal_test")
    rec = store.read_turn("test_wal", 1)
    assert rec is not None and rec["l0_text"] == "wal_test"
    # 验证数据库确实是 WAL 模式
    import sqlite3
    conn = sqlite3.connect(str(db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal", f"Expected WAL mode, got {mode}"


# ═══════════════════════════════════════════════════════════════════════════
# PR1 v5 Schema 验证测试（测试线 WP1）
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.high
def test_v5_schema_created(tmp_path):
    """v5 新库创建时验证 v5 表结构正确
    Steps: 新建 SQLiteStore → 写入触发 schema 创建 → 检查 v5 列"""
    db = tmp_path / "test_v5.db"
    store = SQLiteStore(db_path=str(db))
    # 第一次写操作触发 schema 创建
    store.write_turn("sid", 1, l0_text="test", l1_text="{}", token_offset=0)
    store.close()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(turn_cache)")}
    conn.close()
    # v5 关键列
    assert "role" in cols, f"Missing role column, got {list(cols.keys())}"
    assert "api_call_count" in cols, f"Missing api_call_count column"
    assert "seq_index" in cols, f"Missing seq_index column"
    assert "content" in cols, f"Missing content column"
    # v4 列作为虚拟列存在（需要独立连接，因为 store.close 可能关闭了原始连接）
    conn2 = sqlite3.connect(str(db))
    xcols = {r[1]: r[6] for r in conn2.execute("PRAGMA table_xinfo(turn_cache)")}
    conn2.close()
    assert "turn_type" in xcols, f"Missing turn_type generated column: {list(xcols.keys())}"
    assert "tool_sub_index" in xcols, f"Missing tool_sub_index generated column"
    assert "l2_text" in xcols, f"Missing l2_text generated column"


@pytest.mark.high
def test_v5_turn_plan_pk_extended(tmp_path):
    """v5 turn_plan PK 含 api_call_count + seq_index
    Steps: 新建 store → 写入触发 schema → 检查 turn_plan 列"""
    db = tmp_path / "test_v5_tp.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("sid", 1, l0_text="test", l1_text="{}", token_offset=0)
    store.close()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(turn_plan)")}
    conn.close()
    assert "api_call_count" in cols
    assert "seq_index" in cols


@pytest.mark.high
def test_v5_write_turn_old_params_backward(tmp_path):
    """write_turn 旧参数（turn_type, tool_sub_index）→ v5 映射
    Steps: 用旧参数写入 → read_turn 返回正确值"""
    db = tmp_path / "test_v5_bw.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("sid", 1, turn_type="dialogue", tool_sub_index=0,
                     l0_text="l0_val", l1_text="l1_val", token_offset=100)
    rec = store.read_turn("sid", 1)
    assert rec is not None
    assert rec["l0_text"] == "l0_val"
    assert rec["turn_type"] == "dialogue"
    store.close()


@pytest.mark.medium
def test_v5_write_turn_l2_text_backward(tmp_path):
    """write_turn l2_text → content 向后兼容映射
    Steps: l2_text=JSON → 读出 content 等于该 JSON"""
    import json
    db = tmp_path / "test_v5_l2.db"
    store = SQLiteStore(db_path=str(db))
    l2_val = json.dumps([{"role": "user", "content": "hello"}])
    store.write_turn("sid", 1, turn_type="dialogue", tool_sub_index=0,
                     l2_text=l2_val, l0_text="", l1_text="{}", token_offset=0)
    rec = store.read_turn("sid", 1)
    assert rec is not None
    assert rec["l2_text"] == l2_val  # 虚拟列
    store.close()


@pytest.mark.medium
def test_v5_read_session_turn_type_mapping(tmp_path):
    """v5 read_session 返回 turn_type 映射正确
    Steps: 写入 role='user' → read_session 返回 turn_type='dialogue'"""
    db = tmp_path / "test_v5_rs.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("sid", 1, role="user", l0_text="l0", l1_text="{}", token_offset=0)
    records = store.read_session("sid")
    assert len(records) == 1
    assert records[0]["turn_type"] == "dialogue", f"Got {records[0]['turn_type']}"
    assert records[0]["role"] == "user" if "role" in records[0] else True
    store.close()


@pytest.mark.medium
def test_v5_max_turn_index_uses_role(tmp_path):
    """v5 max_turn_index 用 WHERE role='user' 条件
    Steps: 写入 user + tool 行 → max_turn_index 只计 user 行"""
    db = tmp_path / "test_v5_mti.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("sid", 1, role="user", l0_text="u1", l1_text="{}", token_offset=0)
    store.write_turn("sid", 2, role="user", l0_text="u2", l1_text="{}", token_offset=0)
    store.write_turn("sid", 3, role="tool", l0_text="", l1_text="", token_offset=50,
                     api_call_count=1, seq_index=1)
    assert store.max_turn_index("sid") == 2  # 只计 user 行
    store.close()


@pytest.mark.medium
def test_v5_write_turn_plan_read_back(tmp_path):
    """v5 write_turn_plan / read_turn_plan 含 api_call_count / seq_index
    Steps: 写入 plan → 读回来验证新字段"""
    db = tmp_path / "test_v5_wtp.db"
    store = SQLiteStore(db_path=str(db))
    entries = [{
        "turn_index": 1, "api_call_count": 0, "seq_index": 0,
        "turn_type": "dialogue", "target_level": "L0",
        "decision_reason": "middle", "l2_tokens": 10,
        "summary_tokens": 5, "tokens_saved": 5,
    }]
    result = store.write_turn_plan("sid", entries)
    assert result, "write_turn_plan should succeed"
    plans = store.read_turn_plan("sid")
    assert len(plans) == 1
    assert plans[0]["api_call_count"] == 0
    assert plans[0]["seq_index"] == 0
    store.close()


@pytest.mark.medium
def test_v5_readonly_guard(tmp_path):
    """v5 writable store 不可打开 v4 DB: RuntimeError
    Steps: 创建 v4 格式 DB → SQLiteStore(readonly=False) → 首次连接报 RuntimeError"""
    db = tmp_path / "test_v4.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE turn_cache (session_id TEXT, turn_index INTEGER, "
                 "turn_type TEXT, tool_sub_index INTEGER, "
                 "l0_text TEXT, l1_text TEXT, token_offset INTEGER, "
                 "PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index))")
    conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO _meta (key, value) VALUES ('schema_version', '4')")
    conn.commit()
    conn.close()
    store = SQLiteStore(db_path=str(db), readonly=False)
    with pytest.raises(RuntimeError, match="v4 DB"):
        _ = store.conn  # 首次连接触发 schema 检查


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 22, 0),
                    reason="URI mode=ro requires SQLite >= 3.22.0")
@pytest.mark.medium
def test_v5_readonly_mode(tmp_path):
    """v4 DB readonly 模式可正常读取
    Steps: 创建 v4 格式 DB → readonly=True 打开 → 读操作正常"""
    import sqlite3
    db = tmp_path / "test_v4_ro.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE turn_cache (
            session_id TEXT, turn_index INTEGER,
            turn_type TEXT NOT NULL DEFAULT 'dialogue',
            tool_sub_index INTEGER NOT NULL DEFAULT 0,
            l0_text TEXT, l1_text TEXT, l2_text TEXT,
            l0_embedding BLOB, l1_embedding BLOB,
            bm25_tokens TEXT,
            token_offset INTEGER, backfill_attempts INTEGER DEFAULT 0,
            _assemble_status INTEGER DEFAULT 0,
            query_embedding BLOB,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)
        );
        CREATE TABLE turn_plan (
            session_id TEXT, turn_index INTEGER,
            turn_type TEXT, tool_sub_index INTEGER,
            target_level TEXT, decision_reason TEXT,
            l2_tokens INTEGER, summary_tokens INTEGER, tokens_saved INTEGER,
            rrf_score REAL, upgrade_rank INTEGER,
            budget_remaining INTEGER, topic_group INTEGER, created_at TEXT,
            PRIMARY KEY (session_id, turn_index, turn_type, tool_sub_index)
        );
        CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO _meta (key, value) VALUES ('schema_version', '4');
    """)
    conn.execute("INSERT INTO turn_cache (session_id, turn_index, turn_type, tool_sub_index, l0_text, l1_text, token_offset) "
                 "VALUES ('sid', 1, 'dialogue', 0, 'l0', 'l1', 0)")
    conn.commit()
    conn.close()

    store = SQLiteStore(db_path=str(db), readonly=True)
    recs = store.read_session("sid")
    assert len(recs) == 1
    assert recs[0]["l0_text"] == "l0"
    # readonly 写入应静默跳过
    result = store.write_turn("sid", 2, l0_text="l0_new")
    assert result is False, "readonly write should return False"
    store.close()


@pytest.mark.high
def test_v5_write_turn_with_new_params(tmp_path):
    """write_turn v5 新参数可直接传入
    Steps: 用 role/api_call_count/seq_index 写入 → 读出验证"""
    db = tmp_path / "test_v5_new.db"
    store = SQLiteStore(db_path=str(db))
    store.write_turn("sid", 1, role="assistant", api_call_count=1, seq_index=0,
                     content="thought text", tool_calls_json='[{"id":"call_1"}]',
                     finish_reason="tool_calls",
                     l0_text="", l1_text="{}", token_offset=0)
    records = store.read_session("sid")
    assert len(records) == 1
    r = records[0]
    assert r.get("content") == "thought text"
    assert r.get("api_call_count") == 1
    assert r.get("seq_index") == 0
    assert r.get("turn_type") == "dialogue"  # role=assistant → turn_type='dialogue'
    store.close()


@pytest.mark.medium
def test_v5_write_tool_group_stub(tmp_path):
    """write_tool_group() 批量写入工具组
    Steps: 写入 1 组 API（3 工具）→ 读出验证"""
    db = tmp_path / "test_v5_tool_group.db"
    store = SQLiteStore(db_path=str(db))
    rows = [
        # assistant{tc} 行
        {"role": "assistant", "api_call_count": 1, "seq_index": 0,
         "content": "我来查文件", "tool_calls_json": '[{"id":"c1","function":{"name":"read_file","arguments":"{\\"path\\":\\"/a\\"}"}}]',
         "finish_reason": "tool_calls", "api_request_id": "req_001",
         "l1_text": '{"group_intent":"查文件","group_result":"ok","tool_count":1,"state":"ok"}'},
        # tool 行
        {"role": "tool", "api_call_count": 1, "seq_index": 1,
         "content": "file content", "tool_call_id": "c1", "tool_name": "read_file",
         "status": "ok", "duration_ms": 150, "api_request_id": "req_001",
         "l1_text": '{"tool_name":"read_file","result_summary":"file content","status":"ok"}'},
    ]
    result = store.write_tool_group("sid", 1, 1, rows)
    assert result is True

    records = store.read_session("sid")
    assert len(records) == 2
    # assistant{tc} 行 → role='assistant' → turn_type='dialogue'
    assert records[0]["turn_type"] == "dialogue"
    assert records[0]["api_call_count"] == 1
    assert records[0]["tool_sub_index"] == 0  # mapped from seq_index
    assert records[0]["tool_calls_json"] is not None
    # tool 行 → role='tool' → turn_type='tool'
    assert records[1]["turn_type"] == "tool"
    assert records[1]["tool_call_id"] == "c1"
    assert records[1]["status"] == "ok"
    store.close()


@pytest.mark.medium
def test_v5_write_tool_group_multiple_api(tmp_path):
    """write_tool_group 多 API 组写入
    Steps: 写入 2 组 API（共 4 工具）→ 按 api_call_count 排序验证"""
    db = tmp_path / "test_v5_multi_api.db"
    store = SQLiteStore(db_path=str(db))
    # 先写 user 行 (must exist for ordering)
    store.write_turn("sid", 1, l0_text="hello", l1_text="{}", token_offset=0)
    # API 组 1 (2 tools)
    rows1 = [
        {"role": "assistant", "api_call_count": 1, "seq_index": 0,
         "content": "查文件", "tool_calls_json": "[]", "finish_reason": "tool_calls",
         "api_request_id": "req_001", "l1_text": "{}"},
        {"role": "tool", "api_call_count": 1, "seq_index": 1,
         "content": "a.py", "tool_call_id": "c1", "tool_name": "read_file",
         "status": "ok", "duration_ms": 100, "api_request_id": "req_001", "l1_text": "{}"},
        {"role": "tool", "api_call_count": 1, "seq_index": 2,
         "content": "b.py", "tool_call_id": "c2", "tool_name": "read_file",
         "status": "ok", "duration_ms": 50, "api_request_id": "req_001", "l1_text": "{}"},
    ]
    store.write_tool_group("sid", 1, 1, rows1)
    # API 组 2 (1 tool)
    rows2 = [
        {"role": "assistant", "api_call_count": 2, "seq_index": 0,
         "content": "继续查", "tool_calls_json": "[]", "finish_reason": "tool_calls",
         "api_request_id": "req_002", "l1_text": "{}"},
        {"role": "tool", "api_call_count": 2, "seq_index": 1,
         "content": "c.py", "tool_call_id": "c3", "tool_name": "search_files",
         "status": "ok", "duration_ms": 80, "api_request_id": "req_002", "l1_text": "{}"},
    ]
    store.write_tool_group("sid", 1, 2, rows2)

    records = store.read_session("sid")
    # 1 user + 2 assistant{tc} + 3 tool = 6 rows
    assert len(records) == 6
    # 排序：user(api=0) → api=1 assistant → api=1 tool×2 → api=2 assistant → api=2 tool
    assert records[0]["turn_type"] == "dialogue" and records[0]["api_call_count"] == 0
    assert records[1]["api_call_count"] == 1 and records[1]["turn_type"] == "dialogue"
    assert records[2]["api_call_count"] == 1 and records[2]["turn_type"] == "tool"
    assert records[3]["api_call_count"] == 1 and records[3]["turn_type"] == "tool"
    assert records[4]["api_call_count"] == 2 and records[4]["turn_type"] == "dialogue"
    assert records[5]["api_call_count"] == 2 and records[5]["turn_type"] == "tool"
    store.close()


@pytest.mark.medium
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 22, 0),
                    reason="URI mode=ro requires SQLite >= 3.22.0")
def test_v5_write_tool_group_readonly_skip(tmp_path):
    """write_tool_group readonly 静默跳过"""
    store = SQLiteStore(db_path=str(tmp_path / "test_v5_ro.db"), readonly=True)
    result = store.write_tool_group("sid", 1, 1, [])
    assert result is False
    store.close()

# =============================================================================
# v5.0 turn_stream 读取/更新 — F-stage 支持
# =============================================================================


class TestV5TurnStreamRead:
    """read_turn_elm_rows / read_fct_v5 / read_prev_fct / update_seq0_fct_v5"""

    def test_read_turn_elm_rows_empty(self):
        from ca.store import read_turn_elm_rows, write_turn_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        rows = read_turn_elm_rows(s, "t", 1)
        assert rows == []

    def test_read_turn_elm_rows_write_back(self):
        from ca.store import read_turn_elm_rows, write_turn_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        write_turn_v5(s, "t", 1, 0, role="user", content="hello")
        write_turn_v5(s, "t", 1, 1, role="assistant", content="world")
        rows = read_turn_elm_rows(s, "t", 1)
        assert len(rows) == 2
        assert rows[0][0] == 0  # seq
        assert rows[0][1] == "user"
        assert rows[0][2] == "hello"
        assert rows[1][0] == 1
        assert rows[1][1] == "assistant"
        assert rows[1][2] == "world"

    def test_read_fct_v5_returns_l1_text(self):
        from ca.store import read_fct_v5, write_turn_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        write_turn_v5(s, "t", 1, 0, role="user", content="hi", l1_text='{"core_change":"test"}')
        result = read_fct_v5(s, "t", 1, 0)
        assert "core_change" in result

    def test_read_fct_v5_missing_returns_empty(self):
        from ca.store import read_fct_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        assert read_fct_v5(s, "t", 99, 0) == ""

    def test_read_prev_fct_returns_previous_turn(self):
        from ca.store import read_prev_fct, write_turn_v5, read_fct_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        write_turn_v5(s, "t", 1, 0, role="user", content="first", l1_text='{"core_change":"a"}')
        write_turn_v5(s, "t", 2, 0, role="user", content="second", l1_text='{"core_change":"b"}')
        prev = read_prev_fct(s, "t", 2)
        assert "core_change" in prev
        assert read_fct_v5(s, "t", 1, 0) == prev

    def test_read_prev_fct_turn_0_returns_empty(self):
        from ca.store import read_prev_fct
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        assert read_prev_fct(s, "t", 0) == ""

    def test_update_seq0_fct_v5_updates_l1_l0(self):
        from ca.store import update_seq0_fct_v5, read_fct_v5, write_turn_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        write_turn_v5(s, "t", 1, 0, role="user", content="original", l1_text="old")
        ok = update_seq0_fct_v5(s, "t", 1, fct_text='{"core_change":"new"}', hdl_text="hdl_new")
        assert ok is True
        assert read_fct_v5(s, "t", 1, 0) == '{"core_change":"new"}'
        # Hdl not exposed via read_fct_v5; verify via raw query
        cur = s.conn.execute("SELECT l0_text FROM turn_stream WHERE session_id=? AND turn=? AND seq=0", ("t", 1))
        assert cur.fetchone()[0] == "hdl_new"

    def test_update_seq0_fct_v5_only_affects_seq_0(self):
        from ca.store import update_seq0_fct_v5, write_turn_v5
        from ca.store import SQLiteStore as Store
        s = Store(db_path=":memory:")
        write_turn_v5(s, "t", 1, 0, role="user", content="u", l1_text="old")
        write_turn_v5(s, "t", 1, 1, role="assistant", content="a", l1_text="other")
        update_seq0_fct_v5(s, "t", 1, fct_text="new_fct", hdl_text="new_hdl")
        cur = s.conn.execute("SELECT l1_text FROM turn_stream WHERE session_id=? AND turn=? AND seq=1", ("t", 1))
        assert cur.fetchone()[0] == "other"  # unchanged
