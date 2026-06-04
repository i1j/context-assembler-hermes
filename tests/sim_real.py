"""Real-data simulation: walk through a Hermes session turn by turn,
measuring C-stage (process_turn) + A-stage (assemble) timing."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.expanduser("~/projects/context-assembler"))

from ca import ContextAssembler

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("sim")

STATE_DB = os.path.expanduser("~/.hermes/profiles/python-api-dev/state.db")

# ── Helpers ────────────────────────────────────────────────────────────

def load_session(session_id: str) -> list[dict]:
    """Load messages for a session from Hermes state.db."""
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_call_id, timestamp "
        "FROM messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    conn.close()

    messages = []
    for row in rows:
        msg = {"role": row["role"]}
        if row["content"]:
            msg["content"] = row["content"]
        else:
            msg["content"] = ""
        if row["tool_calls"]:
            msg["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        messages.append(msg)
    return messages


def get_user_turn_pairs(messages: list[dict]) -> list[tuple[int, int]]:
    """Find (user_idx, asst_idx) pairs for each conversation turn."""
    pairs = []
    i = 0
    while i < len(messages):
        if messages[i]["role"] == "user":
            user_idx = i
            # Find the corresponding assistant response
            asst_idx = None
            for j in range(i + 1, min(i + 10, len(messages))):
                role = messages[j]["role"]
                if role == "assistant":
                    tc = messages[j].get("tool_calls")
                    if not tc:
                        asst_idx = j
                        break
                elif role == "user":
                    break
            pairs.append((user_idx, asst_idx))
        i += 1
    return pairs


# ── Simulation ──────────────────────────────────────────────────────────

def simulate(session_id: str, max_turns: int = 10):
    """Walk through turns, measuring C-stage + A-stage for each."""

    print(f"\n{'='*60}")
    print(f"Session: {session_id}")
    print(f"{'='*60}")

    messages = load_session(session_id)
    if not messages:
        print("  No messages found.")
        return

    pairs = get_user_turn_pairs(messages)
    pairs = [p for p in pairs if p[1] is not None]

    print(f"  Total messages: {len(messages)}")
    print(f"  User→Assistant turns: {len(pairs)}")
    print()

    ca = ContextAssembler(
        sqlite_path=f"/tmp/ca_sim_{session_id}.db",
        protect_first_n=0,
    )
    ca.set_session(session_id)
    ca.context_length = 200_000

    timings = {
        "astage_total_ms": 0,
        "cstage_total_ms": 0,
        "astage_calls": 0,
        "cstage_calls": 0,
    }

    for turn_n, (user_idx, asst_idx) in enumerate(pairs[:max_turns]):
        print(f"─── Turn {turn_n} ───")

        # Context up to this user message
        ctx_messages = messages[:user_idx + 1]
        user_msg = messages[user_idx]["content"]
        asst_msg = messages[asst_idx]["content"] if asst_idx and asst_idx < len(messages) else ""

        # ── A-stage: simulate pre_llm_call ──
        t0 = time.monotonic()
        sys_prompt = next(
            (m for m in ctx_messages if m["role"] == "system"), {}
        ).get("content", "") or ""
        # Build realistic message list for assembly
        assembly_msgs = []
        if sys_prompt:
            assembly_msgs.append({"role": "system", "content": sys_prompt})
        # Add head messages (first few non-system)
        head_msgs = [m for m in ctx_messages if m["role"] != "system"][:6]
        assembly_msgs.extend(head_msgs)
        # This turn's user message
        assembly_msgs.append({"role": "user", "content": user_msg})

        result = ca.assemble(
            assembly_msgs,
            current_tokens=5000,
            user_input=user_msg,
        )
        t1 = time.monotonic()
        astage_ms = (t1 - t0) * 1000
        timings["astage_total_ms"] += astage_ms
        timings["astage_calls"] += 1

        # ── C-stage: simulate post_llm_call ──
        if asst_msg:
            t0 = time.monotonic()
            l1 = ca.process_turn(
                user_message=user_msg,
                assistant_response=asst_msg,
                conversation_history=assembly_msgs,
            )
            t1 = time.monotonic()
            cstage_ms = (t1 - t0) * 1000
            timings["cstage_total_ms"] += cstage_ms
            timings["cstage_calls"] += 1
        else:
            cstage_ms = 0
            l1 = ""

        # Report
        markers = sum(1 for m in result if "[检索 ~/" in str(m.get("content", ""))
                       or "[~/0]" in str(m.get("content", "")))
        nmarkers = sum(1 for m in result if "[~/0]" in str(m.get("content", "")))

        print(f"  A-stage: {astage_ms:.1f}ms  | C-stage: {cstage_ms:.1f}ms"
              f"  | L1: {len(l1)} chars"
              f"  | markers: {markers}"
              f"  | promoted: {len(ca._promoted_turns)}")

    # Summary
    a_avg = timings["astage_total_ms"] / max(timings["astage_calls"], 1)
    c_avg = timings["cstage_total_ms"] / max(timings["cstage_calls"], 1)
    total = timings["astage_total_ms"] + timings["cstage_total_ms"]

    print(f"\n{'='*60}")
    print(f"Summary")
    print(f"{'='*60}")
    print(f"  Turns simulated: {min(len(pairs), max_turns)}")
    print(f"  A-stage avg: {a_avg:.1f}ms/turn")
    print(f"  C-stage avg: {c_avg:.1f}ms/turn")
    print(f"  Total time:  {total:.0f}ms ({total/1000:.1f}s)")

    # Cleanup
    store = ca.store
    store.close()
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.unlink(f"/tmp/ca_sim_{session_id}.db{suffix}")
        except OSError:
            pass


# ── Main ───────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(STATE_DB)
    sessions = conn.execute(
        "SELECT id, title, message_count FROM sessions "
        "ORDER BY message_count DESC LIMIT 5"
    ).fetchall()
    conn.close()

    print("Available sessions:")
    for sid, title, count in sessions:
        display_title = (title or "")[:60]
        print(f"  {sid}: {display_title} ({count} msgs)")

    # Pick sessions with enough turns — use the longest one
    targets = []
    for sid, title, count in sessions:
        if count >= 50:
            targets.append(sid)

    for sid in targets[:2]:
        simulate(sid, max_turns=15)

    print("\nDone.")


if __name__ == "__main__":
    main()
