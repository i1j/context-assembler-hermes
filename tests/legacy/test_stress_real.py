"""Real-data stress test for ContextAssembler.

Pulls actual Hermes session data → generates L0/L1 → feeds through
the full pipeline → reports timing, upgrade counts, and output quality.

Run:  cd ~/projects/context-assembler && python tests/test_stress_real.py
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Add Hermes + project to path
HERMES_PATH = os.path.expanduser("~/.hermes/hermes-agent")
PROJECT_PATH = os.path.expanduser("~/projects/context-assembler")
for p in [PROJECT_PATH, HERMES_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Pull real session data
# ---------------------------------------------------------------------------

def pull_session_data(limit=3):
    """Use session_search to get real Hermes sessions."""
    from hermes_state import SessionDB, DEFAULT_DB_PATH

    db_path = DEFAULT_DB_PATH
    print(f"Session DB: {db_path}")
    db = SessionDB(db_path)

    sessions = db.list_sessions_rich(limit=limit, include_children=False, order_by_last_active=True)
    print(f"Found {len(sessions)} sessions in DB")

    results = []
    for entry in sessions[:limit]:
        sid = entry.get("id", "")
        if not sid:
            continue
        try:
            msgs = db.get_messages(sid)
            if not (msgs and len(msgs) >= 2):
                continue
            # Build turns: pairs of user→assistant (with tool calls in between)
            turns = build_turns(msgs)
            if len(turns) >= 3:
                results.append({"session_id": sid, "turns": turns, "raw_msgs": len(msgs)})
                print(f"  Session {sid}: {len(turns)} turns ({len(msgs)} raw msgs)")
        except Exception as e:
            print(f"  Session {sid}: error {e}")

    return results


def build_turns(messages):
    """Group messages into (user_msg, assistant_response) turns."""
    turns = []
    current_user = None

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "user":
            current_user = msg
        elif role == "assistant" and current_user is not None:
            content = msg.get("content", "") or ""
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if isinstance(p, dict)]
                content = " ".join(texts)
            user_text = current_user.get("content", "") or ""
            if isinstance(user_text, list):
                texts = [p.get("text", "") for p in user_text if isinstance(p, dict)]
                user_text = " ".join(texts)
            if user_text.strip():
                turns.append({
                    "user": str(user_text)[:2000],
                    "assistant": str(content)[:2000],
                    "user_raw": str(user_text),
                    "assistant_raw": str(content),
                })
            current_user = None

    return turns[:100]  # cap at 100 turns per session


# ---------------------------------------------------------------------------
# Build L0/L1 from real data
# ---------------------------------------------------------------------------

def make_l0(turn: dict, idx: int) -> str:
    """Generate L0 (~100 chars) from a turn."""
    u = turn["user"][:60].replace("\n", " ")
    a = turn["assistant"][:40].replace("\n", " ")
    return f"Turn {idx}: User asked \"{u}...\" → Assistant responded \"{a}...\""


def make_l1(turn: dict, idx: int) -> str:
    """Generate L1 (~2K chars) from a turn."""
    u = turn["user"][:500]
    a = turn["assistant"][:500]
    return (
        f"[TURN {idx}]\n"
        f"User: {u}\n{'' if u.endswith('.') else '.'}\n"
        f"Assistant: {a}\n{'' if a.endswith('.') else '.'}\n"
        f"---\n"
    )


# ---------------------------------------------------------------------------
# Embedding: fake deterministic vectors for stress test
# ---------------------------------------------------------------------------

def fake_embedding(text: str, dim: int = 128) -> list[float]:
    """Deterministic pseudo-embedding."""
    import hashlib
    h = hashlib.md5(text.encode()).digest()
    import random
    rng = random.Random(int.from_bytes(h[:4], "big"))
    return [rng.random() * 2 - 1 for _ in range(dim)]


# ---------------------------------------------------------------------------
# Run the pipeline
# ---------------------------------------------------------------------------

def run_test(session_data, label=""):
    """Full pipeline: Phase 0 → Phase 1 → Phase 3."""
    from ca import ContextAssembler
    from ca.stats import AssembleStats

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_stress.db")

    try:
        # ── Initialise ──
        assembler = ContextAssembler(
            db_path=db_path,
        )

        # ── Phase 0: Write turns to SQLite ──
        store = assembler.store
        t0 = time.monotonic()

        for i, turn in enumerate(session_data["turns"]):
            l0 = make_l0(turn, i)
            l1 = make_l1(turn, i)
            l0_emb = fake_embedding(l0, dim=128)
            l1_emb = fake_embedding(l1, dim=128)
            bm25_tokens = l1.lower().split()[:50]

            ok = store.write_turn(
                "default", i,
                l0_text=l0,
                l1_text=l1,
                l0_embedding=l0_emb,
                l1_embedding=l1_emb,
                bm25_tokens=bm25_tokens,
                token_offset=i * 500,
            )
            if not ok:
                print(f"  ⚠ Failed to write turn {i}")

        write_ms = (time.monotonic() - t0) * 1000
        total = store.session_turn_count("default")
        print(f"  Phase 0: {total} turns written in {write_ms:.0f}ms")

        # ── Phase 1 + Phase 3: compress with real messages ──
        # Build simulated message list (head + L0 markers + tail + user input)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]

        # Add head turns verbatim
        for i in range(min(3, total)):
            messages.append({"role": "user", "content": f"[~/{i}] {make_l0(session_data['turns'][i], i)}"})
            messages.append({"role": "assistant", "content": f"[~/{i}] {make_l1(session_data['turns'][i], i)}"})

        # Add user's current input (e.g. asking about something from the middle of conversation)
        mid_idx = total // 2 if total > 2 else 0
        questions = [
            f"Tell me more about what happened in turn {mid_idx}",
            "What configuration changes did we discuss?",
            "Can you summarize the technical decisions?",
            "How did we resolve the issue?",
            f"Go back to the discussion about {session_data['turns'][mid_idx]['user'][:30]}...",
        ]
        user_query = questions[hash(label or "") % len(questions)]
        messages.append({"role": "user", "content": user_query})

        # ── Measure Phase 1 separately ──
        stats1 = AssembleStats()
        with stats1.time_phase("pre_assemble"):
            from ca.cache import CacheBuilder
            builder = CacheBuilder(store)
            cache, records = builder.build("default")
        print(f"  Pre-assemble: {stats1.phase_timing.get('pre_assemble', 0)}ms "
              f"(cache: {cache.n_l0} L0, {cache.n_l1} L1, "
              f"{cache.bm25_index.document_count() if cache.bm25_index else 0} BM25 docs)")

        # ── Run full compress() ──
        t1 = time.monotonic()
        result = assembler.assemble(user_input=user_query, messages=messages, context_length=5000 + total * 500)
        compress_ms = (time.monotonic() - t1) * 1000

        # ── Report ──
        n_before = len(messages)
        n_after = len(result)
        total_tokens_after = sum(
            len(str(m.get("content", ""))) // 4 + 1
            for m in result
        )

        print(f"\n  ═══ Results ═══")
        print(f"  Messages: {n_before} → {n_after}")
        print(f"  Estimated tokens after compress: ~{total_tokens_after}")
        print(f"  Compress time: {compress_ms:.0f}ms")

        # Check for [~/N] markers in output
        marker_count = sum(
            1 for m in result
            if isinstance(m.get("content"), str) and "[~/" in m["content"]
        )
        print(f"  [~/N] markers in output: {marker_count}")

        return {"messages_before": n_before, "messages_after": n_after,
                "compress_ms": compress_ms, "marker_count": marker_count,
                "total_tokens": total_tokens_after}

    finally:
        store.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("ContextAssembler — Real-Data Stress Test")
    print("=" * 60)

    # Pull real session data
    print("\n[1] Pulling session data from Hermes DB...")
    sessions = pull_session_data(limit=3)

    if not sessions:
        print("\n⚠ No session data found. Running synthetic test instead.")
        # Generate synthetic data
        sessions = []
        for sid in range(3):
            turns = []
            for i in range(50):
                topics = [
                    "debugging the database connection",
                    "configuring the logger",
                    "setting up the API endpoint",
                    "fixing the timeout issue",
                    "reviewing the PR",
                    "deploying to staging",
                    "testing the embedding model",
                    "updating the dependencies",
                    "refactoring the main module",
                    "writing the documentation",
                ]
                topic = topics[i % len(topics)]
                turns.append({
                    "user": f"Can you help me with {topic}? I need to check the configuration file at /path/to/config.yaml for the parameter setting.",
                    "assistant": f"Sure! Let me look at {topic}. I checked the configuration and found that the setting was incorrect. I've updated it to the proper value and restarted the service. The output now shows correct behavior.",
                })
            sessions.append({"session_id": f"synth_{sid}", "turns": turns, "raw_msgs": len(turns) * 2})

    # Run test for each session
    print(f"\n[2] Running pipeline tests ({len(sessions)} sessions)...")
    all_results = []
    for sd in sessions:
        label = sd["session_id"][:16]
        print(f"\n{'─' * 50}")
        print(f"Session: {label} ({len(sd['turns'])} turns)")
        print(f"{'─' * 50}")
        result = run_test(sd, label=label)
        all_results.append(result)

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for i, r in enumerate(all_results):
        print(f"  Session {i}: {r['messages_before']}→{r['messages_after']} msgs, "
              f"{r['compress_ms']:.0f}ms, {r['marker_count']} markers, "
              f"~{r['total_tokens']}tok")
    avg_ms = sum(r["compress_ms"] for r in all_results) / max(1, len(all_results))
    print(f"\n  Average compress time: {avg_ms:.0f}ms")
    print("  All tests completed ✓")


if __name__ == "__main__":
    main()
