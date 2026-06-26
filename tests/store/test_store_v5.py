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
