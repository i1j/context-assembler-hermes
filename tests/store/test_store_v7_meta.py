"""决策 44 测试：store v7 — 列迁移 / llm_calls / think_trace / 幂等。"""
import json
import sqlite3

from ca.store import (
    SQLiteStore,
    write_turn_v5,
    read_turn_stream_all,
    write_llm_call_v1,
    patch_llm_call_stream_v1,
    read_llm_call_v1,
    write_think_card_v1,
    read_think_cards_v1,
    read_incremental_elm_detailed,
    _TURN_STREAM_V7_COLUMNS,
)


def _legacy_turn_stream_schema():
    return """
    CREATE TABLE IF NOT EXISTS turn_stream (
        session_id   TEXT    NOT NULL,
        turn         INTEGER NOT NULL,
        seq          INTEGER NOT NULL,
        role          TEXT    NOT NULL,
        Elm       TEXT    NOT NULL DEFAULT '',
        tool_name     TEXT,
        tool_call_id  TEXT,
        args_json     TEXT,
        status        TEXT,
        duration_ms   INTEGER,
        tool_calls_json TEXT,
        finish_reason  TEXT,
        usage_prompt_tokens     INTEGER,
        usage_completion_tokens INTEGER,
        biz_category  TEXT,
        written_at    REAL,
        Fct       TEXT,
        Hdl       TEXT,
        PRIMARY KEY (session_id, turn, seq)
    );
    """


class TestV7Migration:
    def test_legacy_db_gets_new_columns_idempotently(self, tmp_path):
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.executescript(_legacy_turn_stream_schema())
        conn.execute("INSERT INTO turn_stream (session_id,turn,seq,role,Elm) VALUES ('s',1,0,'user','旧数据')")
        conn.commit()
        conn.close()

        store = SQLiteStore(db)
        _ = store.conn  # 触发迁移
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(turn_stream)")}
        expected = {name for name, _ in _TURN_STREAM_V7_COLUMNS}
        assert expected.issubset(cols), f"缺列: {expected - cols}"
        rows = read_turn_stream_all(store, "s")
        assert rows[0]["Elm"] == "旧数据"

        # 二次打开不重复 ALTER、不丢数据
        store2 = SQLiteStore(db)
        _ = store2.conn
        assert len(read_turn_stream_all(store2, "s")) == 1

    def test_new_columns_readable_in_export(self, v5_store):
        write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="思考",
                      block_type="thinking", ooda_stage="decide",
                      request_id="r1", provider="deepseek", model="deepseek-v4",
                      reasoning_chars=2, is_fin=0)
        rows = read_turn_stream_all(v5_store, "s")
        assert rows[0]["block_type"] == "thinking"
        assert rows[0]["ooda_stage"] == "decide"
        assert rows[0]["request_id"] == "r1"
        assert rows[0]["provider"] == "deepseek"


class TestWriteTurnV7Idempotency:
    def test_replay_preserves_fct_with_new_core(self, v5_store):
        from ca.store import update_fin_fct_v5
        write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="回复",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", request_id="r1", is_fin=1)
        update_fin_fct_v5(v5_store, "s", 1, 1, '{"core_change":"已摘要"}', "标题")
        assert write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="回复",
                             finish_reason="stop", block_type="agent_reply",
                             ooda_stage="decide", request_id="r1", is_fin=1) is True
        rows = read_turn_stream_all(v5_store, "s")
        assert rows[0]["Fct"] == '{"core_change":"已摘要"}'

    def test_different_block_type_overwrites(self, v5_store):
        write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="x",
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="x",
                      block_type="agent_reply", ooda_stage="decide")
        rows = read_turn_stream_all(v5_store, "s")
        assert rows[0]["block_type"] == "agent_reply"


class TestLlmCallsV1:
    def test_write_and_read(self, v5_store):
        assert write_llm_call_v1(v5_store, "s", "r1", request_seq=1, turn=2,
                                  provider="deepseek", model="m", usage_json='{"output_tokens":1}',
                                  finish_kind="stop", status="completed")
        row = read_llm_call_v1(v5_store, "s", "r1")
        assert row["provider"] == "deepseek"
        assert json.loads(row["usage_json"])["output_tokens"] == 1

    def test_upsert_latest_wins(self, v5_store):
        write_llm_call_v1(v5_store, "s", "r1", provider="p1", finish_kind="stop")
        write_llm_call_v1(v5_store, "s", "r1", provider="p2", finish_kind="length")
        rows = v5_store.conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE session_id=? AND request_id=?", ("s", "r1")).fetchone()[0]
        assert rows == 1
        assert read_llm_call_v1(v5_store, "s", "r1")["provider"] == "p2"

    def test_stream_patch_does_not_clobber_authoritative_fields(self, v5_store):
        # stream 先到：建立最小行
        patch_llm_call_stream_v1(v5_store, "s", "r1", provider="deepseek",
                                 model="m", reasoning_chars=10, text_chars=20,
                                 chunk_count=3)
        # post_api 后到：主写，stream 计数取 max
        write_llm_call_v1(v5_store, "s", "r1", provider="deepseek", model="m",
                          reasoning_chars=8, text_chars=20, chunk_count=3,
                          usage_json='{"total_tokens":9}', finish_kind="stop")
        row = read_llm_call_v1(v5_store, "s", "r1")
        assert row["reasoning_chars"] == 10
        assert row["text_chars"] == 20
        assert row["finish_kind"] == "stop"
        assert json.loads(row["usage_json"])["total_tokens"] == 9

    def test_stream_patch_after_main_write_merges(self, v5_store):
        write_llm_call_v1(v5_store, "s", "r1", provider="p", finish_kind="stop",
                          reasoning_chars=5, text_chars=6)
        patch_llm_call_stream_v1(v5_store, "s", "r1", reasoning_chars=50,
                                 text_chars=60, chunk_count=9)
        row = read_llm_call_v1(v5_store, "s", "r1")
        assert row["reasoning_chars"] == 50
        assert row["text_chars"] == 60
        assert row["chunk_count"] == 9
        assert row["finish_kind"] == "stop"


class TestThinkTraceV1:
    def test_write_read_unique_latest_wins(self, v5_store):
        card = {"session_id": "s", "turn": 1, "seq": 2, "txn_id": 1,
                "source_kind": "cloud_think", "card_kind": "decision",
                "raw_len": 100, "preview": "思", "status": "raw"}
        assert write_think_card_v1(v5_store, card)
        cards = read_think_cards_v1(v5_store, "s")
        assert len(cards) == 1
        card["card_kind"] = "conclusion"
        write_think_card_v1(v5_store, card)
        cards = read_think_cards_v1(v5_store, "s")
        assert len(cards) == 1
        assert cards[0]["card_kind"] == "conclusion"


class TestReadIncrementalElmDetailed:
    def test_returns_metadata_and_orders(self, v5_store):
        write_turn_v5(v5_store, "s", 1, 0, role="user", elm_text="问题",
                      block_type="user_message", ooda_stage="orient")
        write_turn_v5(v5_store, "s", 1, 1, role="assistant", elm_text="想",
                      block_type="thinking", ooda_stage="decide")
        write_turn_v5(v5_store, "s", 1, 2, role="tool", elm_text="结果",
                      tool_name="bash", block_type="tool_call_result",
                      ooda_stage="observe")
        write_turn_v5(v5_store, "s", 1, 3, role="assistant", elm_text="答复",
                      finish_reason="stop", block_type="agent_reply",
                      ooda_stage="decide", is_fin=1)
        rows = read_incremental_elm_detailed(v5_store, "s", 1, 3)
        assert [r["seq"] for r in rows] == [0, 1, 2, 3]
        assert rows[2]["block_type"] == "tool_call_result"
        assert rows[3]["is_fin"] == 1
