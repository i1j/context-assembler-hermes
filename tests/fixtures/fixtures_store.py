"""Store fixtures — v5_store, v5_turn_stream."""

import pytest


@pytest.fixture
def v5_store(tmp_path):
    """v5 内存模式 SQLiteStore，已创建 turn_stream 表。"""
    from ca.store import SQLiteStore

    s = SQLiteStore(db_path=str(tmp_path / "v5_test.db"))
    yield s
    s.close()


@pytest.fixture
def v5_turn_stream(v5_store):
    """预填入 2 轮数据（共 6 行）的 v5 turn_stream 表。"""
    from ca.store import write_turn_v5

    s = v5_store
    # turn 0
    write_turn_v5(
        s, "test", 0, 0, role="user", elm_text="你好",
        fct_text='{"core_change":"开始讨论了","new_materials":["问候"]}',
    )
    write_turn_v5(
        s, "test", 0, 1, role="assistant", elm_text="",
        tool_calls_json='[{"id":"c0","function":{"name":"test","arguments":{}}}]',
    )
    write_turn_v5(
        s, "test", 0, 2, role="tool", elm_text="ok", tool_call_id="c0",
        fct_text='{"core_change":"工具执行成功","new_materials":["结果 ok"]}',
    )
    # turn 1
    write_turn_v5(
        s, "test", 1, 0, role="user", elm_text="继续",
        fct_text='{"core_change":"用户请求继续","new_materials":["追问"]}',
    )
    write_turn_v5(
        s, "test", 1, 1, role="assistant", elm_text="",
        tool_calls_json='[{"id":"c1","function":{"name":"terminal","arguments":{"cmd":"ls"}}}]',
    )
    write_turn_v5(
        s, "test", 1, 2, role="tool", elm_text="file1.txt", tool_call_id="c1",
        fct_text='{"core_change":"列出文件","new_materials":["file1.txt"]}',
    )
    yield s
