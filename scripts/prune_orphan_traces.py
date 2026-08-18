"""Delete the trace rows left behind by the pre-fix streaming bug.

Until `bind_context` pinned the SSE generator to one context, a streamed chat turn
produced two useless kinds of row:

* a `chat.turn` / `web_chat` trace that recorded only `operation_started` and
  `context` before its handle went out of scope, and
* a detached `chat.turn` / `implicit` trace, opened by `AIClient.stream_chat` when
  it could no longer see its parent, carrying no chat session to correlate against.

Neither can be repaired: only 14 of the 62 detached traces even overlap a parent in
time. This removes the web_chat stubs whose chat session no longer exists (deleted
sessions plus what leaked in from test runs) and every detached `chat.turn`, and
leaves everything else — including web_chat traces for sessions you still have.

    python scripts/prune_orphan_traces.py            # report only
    python scripts/prune_orphan_traces.py --apply    # delete
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DB = PROJECT_ROOT / "sbpeye.db"
DEBUG_DB = Path(os.getenv("SBPEYE_DEBUG_DB") or (PROJECT_ROOT / "sbpeye_debug.db"))


def orphan_ids(debug: sqlite3.Connection, live_session_ids: set[str]) -> dict[str, list[str]]:
    """The two orphan classes, as `{reason: [trace_id, ...]}`."""
    stubs, detached = [], []
    for trace_id, origin, session_id in debug.execute(
        "SELECT id, origin, chat_session_id FROM llm_traces WHERE operation = 'chat.turn'"
    ):
        if origin == "implicit":
            detached.append(trace_id)
        elif origin == "web_chat":
            # Workspace sessions are synthetic ids with no chat_sessions row; they are
            # legitimate and must survive.
            if session_id and not str(session_id).startswith("workspace:") \
                    and session_id not in live_session_ids:
                stubs.append(trace_id)
    return {
        "web_chat stub, chat session no longer exists": stubs,
        "detached chat.turn (no parent, no session)": detached,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete instead of reporting")
    args = parser.parse_args()

    if not DEBUG_DB.exists():
        print(f"No debug database at {DEBUG_DB}", file=sys.stderr)
        return 1

    with sqlite3.connect(f"file:{APP_DB}?mode=ro", uri=True) as app:
        live = {row[0] for row in app.execute("SELECT id FROM chat_sessions")}

    debug = sqlite3.connect(DEBUG_DB)
    try:
        groups = orphan_ids(debug, live)
        targets = [trace_id for ids in groups.values() for trace_id in ids]
        total_traces = debug.execute("SELECT count(*) FROM llm_traces").fetchone()[0]

        for reason, ids in groups.items():
            if not ids:
                print(f"  0  {reason}")
                continue
            marks = ",".join("?" * len(ids))
            events, payload = debug.execute(
                f"SELECT count(*), coalesce(sum(payload_bytes), 0) "
                f"FROM llm_trace_events WHERE trace_id IN ({marks})", ids
            ).fetchone()
            print(f"{len(ids):>3}  {reason}  ({events} events, {payload / 1024:.0f} KB)")

        print(f"\n{len(targets)} of {total_traces} traces selected; "
              f"{total_traces - len(targets)} kept.")
        if not args.apply:
            print("Dry run — pass --apply to delete.")
            return 0
        if not targets:
            return 0

        marks = ",".join("?" * len(targets))
        debug.execute(f"DELETE FROM llm_trace_events WHERE trace_id IN ({marks})", targets)
        debug.execute(f"DELETE FROM llm_traces WHERE id IN ({marks})", targets)
        debug.commit()
        debug.execute("VACUUM")
        print(f"Deleted {len(targets)} traces. "
              f"{debug.execute('SELECT count(*) FROM llm_traces').fetchone()[0]} remain.")
        return 0
    finally:
        debug.close()


if __name__ == "__main__":
    raise SystemExit(main())
