"""Store v5 数据写入/读取综合测试。

覆盖:
  - turn_stream 写入 + 读取
  - update_fin_fct_v5
  - read_turn_stream_all
  - 多 session_id 隔离
"""
import pytest


class TestWriteAndReadV5:
    """write_turn_v5 + read_turn_elm_rows 基础写入读取"""

    def test_write_then_read(self, v5_store):
        from ca.store import write_turn_v5, read_turn_elm_rows
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="你好")
        rows = read_turn_elm_rows(v5_store, "test", 0)
        assert len(rows) == 1
        assert rows[0][2] == "你好"

    def test_multiple_turns(self, v5_store):
        from ca.store import write_turn_v5, read_turn_elm_rows
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="t0_u")
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", elm_text="t0_a")
        write_turn_v5(v5_store, "test", 1, 0, role="user", elm_text="t1_u")
        rows0 = read_turn_elm_rows(v5_store, "test", 0)
        rows1 = read_turn_elm_rows(v5_store, "test", 1)
        assert len(rows0) == 2
        assert len(rows1) == 1

    def test_session_id_isolation(self, v5_store):
        from ca.store import write_turn_v5, read_turn_elm_rows
        write_turn_v5(v5_store, "sess_a", 0, 0, role="user", elm_text="A")
        write_turn_v5(v5_store, "sess_b", 0, 0, role="user", elm_text="B")
        rows_a = read_turn_elm_rows(v5_store, "sess_a", 0)
        rows_b = read_turn_elm_rows(v5_store, "sess_b", 0)
        assert rows_a[0][2] == "A"
        assert rows_b[0][2] == "B"
        assert len(rows_a) == 1
        assert len(rows_b) == 1


class TestWriteTurnV5Idempotency:
    """02-store 契约：同内容重放跳过，不回抹已回填 Fct/Hdl。"""

    def test_same_core_replay_preserves_backfilled_fct(self, v5_store):
        from ca.store import write_turn_v5, update_fin_fct_v5, read_turn_stream_all
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", elm_text="回复",
                      finish_reason="stop")
        update_fin_fct_v5(v5_store, "test", 0, 1,
                          '{"core_change":"异步摘要"}', "摘要")
        # 重放 post_llm_call：核心列一致，但本次调用 Fct/Hdl=None
        assert write_turn_v5(v5_store, "test", 0, 1, role="assistant",
                             elm_text="回复", finish_reason="stop") is True
        rows = read_turn_stream_all(v5_store, "test")
        assert rows[0]["Fct"] == '{"core_change":"异步摘要"}'
        assert rows[0]["Hdl"] == "摘要"

    def test_different_core_still_overwrites(self, v5_store):
        from ca.store import write_turn_v5, read_turn_stream_all
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="旧内容")
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="新内容")
        rows = read_turn_stream_all(v5_store, "test")
        assert len(rows) == 1
        assert rows[0]["Elm"] == "新内容"


class TestTopicConnThreadLocal:
    """ca_topics.db 连接必须是线程本地（后台多线程并发写共享连接会丢写）。"""

    def test_different_thread_gets_different_connection(self, tmp_path):
        import threading
        from ca.store import _get_topic_conn

        db = tmp_path / "ca_topics.db"
        main_conn = _get_topic_conn(db)
        result = {}

        def worker():
            result["conn"] = _get_topic_conn(db)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert result["conn"] is not main_conn
        main_conn.execute("SELECT 1")
        result["conn"].execute("SELECT 1")


class TestUpdateFinFctV5:
    """update_fin_fct_v5 Fct/Hdl 回写"""

    def test_update_fin_fct(self, v5_store):
        from ca.store import write_turn_v5, update_fin_fct_v5, read_fct_v5
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", elm_text="",
                      finish_reason="stop")
        update_fin_fct_v5(v5_store, "test", 0, 1,
                          '{"core_change":"F-stage 摘要"}', "摘要")
        result = read_fct_v5(v5_store, "test", 0, 1)
        assert "F-stage 摘要" in result

    def test_update_nonexistent_turn(self, v5_store):
        from ca.store import update_fin_fct_v5
        result = update_fin_fct_v5(v5_store, "test", 999, 1,
                                    '{"core_change":"x"}', "x")
        assert result is True


class TestReadTurnStreamAll:
    """read_turn_stream_all — 全量导出"""

    def test_returns_all_rows(self, v5_store):
        from ca.store import write_turn_v5, read_turn_stream_all
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="u0")
        write_turn_v5(v5_store, "test", 1, 0, role="user", elm_text="u1")
        rows = read_turn_stream_all(v5_store, "test")
        assert len(rows) == 2

    def test_returns_empty_for_unknown_session(self, v5_store):
        from ca.store import read_turn_stream_all
        rows = read_turn_stream_all(v5_store, "nonexistent")
        assert rows == []

    def test_columns_match_turn_stream_schema(self, v5_store):
        from ca.store import write_turn_v5, read_turn_stream_all
        write_turn_v5(v5_store, "test", 0, 0, role="user", elm_text="你好",
                      fct_text='{"core_change":"测试"}')
        rows = read_turn_stream_all(v5_store, "test")
        assert len(rows) == 1
        conn = v5_store.conn
        cur = conn.execute(
            "SELECT name FROM pragma_table_info('turn_stream') WHERE name IN "
            "('biz_category','tool_name','status','duration_ms','written_at')"
        )
        cols = {r[0] for r in cur.fetchall()}
        assert cols == {"biz_category", "tool_name", "status", "duration_ms", "written_at"}, \
            f"turn_stream 表应包含元数据列: {cols}"
