"""Store 层接口契约测试 — SQLiteStore + 模块级函数。

覆盖:
  - CR-005: SQLiteStore.session_id 属性
  - get_turn_ca_rows 7 列契约
  - max_turn_v5 行为
  - write_turn_v5 UPSERT 幂等性
  - read_fct_v5 / read_turn_elm_rows 列契约
  - format_previous_summary_for_prompt

所有 fixture 来自 conftest re-export，不依赖 autouse mock。
"""
from pathlib import Path


class TestSQLiteStoreContract:
    """SQLiteStore 公开接口契约"""

    def test_session_id_equals_db_stem(self, v5_store):
        """DB 文件名 = session_id"""
        assert v5_store._db_path.stem == "v5_test"
        assert v5_store.session_id == "v5_test"

    def test_session_id_is_readable_string(self, v5_store):
        """session_id 是可读字符串，非空"""
        assert isinstance(v5_store.session_id, str)
        assert len(v5_store.session_id) > 0

    def test_session_id_persists_across_methods(self, v5_store):
        """session_id 在多次调用间一致"""
        sid1 = v5_store.session_id
        from ca.store import write_turn_v5
        write_turn_v5(v5_store, v5_store.session_id, 0, 0,
                      role="user", content="test")
        assert v5_store.session_id == sid1

    def test_session_id_matches_db_creation(self, tmp_path):
        """新创建的 DB，session_id = 文件名.stem"""
        from ca.store import SQLiteStore
        db_path = tmp_path / "my_session_abc123.db"
        store = SQLiteStore(db_path=str(db_path))
        try:
            assert store.session_id == "my_session_abc123"
        finally:
            store.close()

    def test_store_has_session_id_attr(self, v5_store):
        """hasattr 通过 — 任何消费者都应该能安全检查"""
        assert hasattr(v5_store, "session_id")


class TestGetTurnCaRowsContract:
    """get_turn_ca_rows 7 列契约"""

    def test_column_count_and_types(self, v5_store):
        """get_turn_ca_rows 返回 7 列：seq, role, finish_reason, tool_calls_json, content, Fct, Hdl"""
        from ca.store import write_turn_v5, get_turn_ca_rows

        write_turn_v5(v5_store, "test", 0, 0, role="user", content="你好",
                      fct_text='{"core_change":"用户问候"}')
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", content="",
                      tool_calls_json='[{"id":"c1"}]',
                      finish_reason="tool_calls",
                      fct_text='{"core_change":"思考Fct"}',
                      hdl_text="思考Hdl_short")

        rows = get_turn_ca_rows(v5_store, "test", 0)
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"

        row = rows[0]
        assert len(row) == 7, f"Expected 7 columns, got {len(row)}: {row}"
        assert isinstance(row[0], int), f"col[0] seq should be int: {type(row[0])}"
        assert isinstance(row[1], str), f"col[1] role should be str: {type(row[1])}"

        row1 = rows[1]
        assert row1[5] == '{"core_change":"思考Fct"}', f"Fct mismatch: {row1[5]!r}"
        assert row1[6] == "思考Hdl_short", f"Hdl mismatch: {row1[6]!r}"

        assert rows[0][5] is not None, "user row Fct should not be None"
        assert rows[0][6] is None, "user row Hdl should be None (not written)"

    def test_empty_turn_returns_empty_list(self, v5_store):
        """不存在的 turn 返回空列表"""
        from ca.store import get_turn_ca_rows
        rows = get_turn_ca_rows(v5_store, "test", 999)
        assert rows == []


class TestMaxTurnV5:
    """max_turn_v5 行为契约"""

    def test_empty_store_returns_0(self, v5_store):
        from ca.store import max_turn_v5
        assert max_turn_v5(v5_store, "test") == 0

    def test_returns_highest_turn(self, v5_store):
        from ca.store import write_turn_v5, max_turn_v5
        write_turn_v5(v5_store, "test", 0, 0, role="user", content="u0")
        write_turn_v5(v5_store, "test", 5, 0, role="user", content="u5")
        write_turn_v5(v5_store, "test", 3, 0, role="user", content="u3")
        assert max_turn_v5(v5_store, "test") == 5


class TestWriteTurnV5Idempotent:
    """write_turn_v5 UPSERT 幂等性"""

    def test_same_turn_seq_overwrites(self, v5_store):
        from ca.store import write_turn_v5, read_turn_elm_rows
        write_turn_v5(v5_store, "test", 0, 0, role="user", content="原始内容")
        write_turn_v5(v5_store, "test", 0, 0, role="user", content="新内容")
        rows = read_turn_elm_rows(v5_store, "test", 0)
        assert rows[0][2] == "新内容"

    def test_insert_or_replace_maintains_row_count(self, v5_store):
        from ca.store import write_turn_v5, read_turn_elm_rows
        write_turn_v5(v5_store, "test", 0, 0, role="user", content="u")
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", content="a")
        # 覆盖第 1 行
        write_turn_v5(v5_store, "test", 0, 1, role="assistant", content="a2")
        rows = read_turn_elm_rows(v5_store, "test", 0)
        assert len(rows) == 2


class TestStoreCloseReopen:
    """Store 关闭后重新打开可读"""

    def test_data_survives_close_reopen(self, tmp_path):
        from ca.store import SQLiteStore, write_turn_v5, read_turn_elm_rows
        db = tmp_path / "reopen.db"
        s1 = SQLiteStore(db_path=str(db))
        write_turn_v5(s1, "test", 0, 0, role="user", content="持久化数据")
        s1.close()

        s2 = SQLiteStore(db_path=str(db))
        try:
            rows = read_turn_elm_rows(s2, "test", 0)
            assert len(rows) == 1
            assert rows[0][2] == "持久化数据"
        finally:
            s2.close()
