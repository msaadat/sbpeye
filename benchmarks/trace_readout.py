"""What the traces say about a round, as distinct from what the answers say.

    python benchmarks/trace_readout.py benchmarks/results/2026-08-26-sbpeye/answers

Joins each captured answer to its `chat.turn` trace in `sbpeye_debug.db` by session id and
reports the things a rater cannot see:

  * **Own model time** — `llm_traces.duration_ms`, not the wall clock the runner recorded.
    Whenever a round is not run serially the runner's elapsed time is the batch total, not
    the item's: in the 2026-08-23 round three items each reported 279.1 s, which was the
    span of all three. This is the per-item number that column should have held.
  * **Tool calls, and whether the turn hit the iteration ceiling.** The `_MAX_TOOL_ITERATIONS`
    fallback rebuilds the turn from the tool results gathered so far, and has historically
    been where evidence went missing — in the 2026-08-23 round the three turns that fell
    through were exactly the three worst answers.
  * **Dropped citations** — `citation_drop` events, one per handle the renderer refused.
    A per-round count says whether a prompt change broke citation compliance without
    grading a single answer. The 2026-08-23 baseline is 8 handles across 15 items and 3
    reruns; 3 of those fell in the round proper.
  * **Which tools were called**, so the reach of `search_corpus` / `get_law_details` into
    the laws corpus can be read off directly rather than inferred from the prose.

Requires `llm_debug_enabled` to have been on for the round.
"""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def turn_trace(db: sqlite3.Connection, session_id: str) -> dict | None:
    row = db.execute(
        "SELECT id, duration_ms, prompt_tokens, completion_tokens, total_tokens, status "
        "FROM llm_traces WHERE chat_session_id = ? AND operation = 'chat.turn' "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    trace_id, duration_ms, prompt, completion, total, status = row
    events = db.execute(
        "SELECT kind, stage, payload_json FROM llm_trace_events "
        "WHERE trace_id = ? ORDER BY sequence",
        (trace_id,),
    ).fetchall()

    tools = Counter()
    dropped: list[str] = []
    iterations = 0
    synthesis = False
    for kind, stage, payload_json in events:
        if kind == "tool_request":
            payload = json.loads(payload_json)
            name = payload.get("name") or payload.get("tool") or "?"
            tools[name] += 1
        if kind == "citation_drop":
            # Each entry is {"reason": …, "handle": …}; the handle is the readable half.
            for entry in json.loads(payload_json).get("dropped", []):
                dropped.append(
                    entry.get("handle", "?") if isinstance(entry, dict) else str(entry)
                )
        if stage and stage.startswith("chat.iteration."):
            iterations = max(iterations, int(stage.rsplit(".", 1)[1]))
        if stage == "chat.final_synthesis":
            synthesis = True

    return {
        "trace_id": trace_id[:8],
        "status": status,
        "duration_ms": duration_ms,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "tool_calls": sum(tools.values()),
        "tools": dict(tools),
        "iterations": iterations,
        "hit_ceiling": synthesis,
        "dropped": dropped,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("answers", help="a round's answers directory")
    parser.add_argument("--db", default="sbpeye_debug.db")
    args = parser.parse_args()

    files = sorted(Path(args.answers).glob("P*.json"))
    if not files:
        raise SystemExit(f"{args.answers}: no answer files found")

    db = sqlite3.connect(args.db)
    rows = []
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        session_id = record.get("session_id")
        trace = turn_trace(db, session_id) if session_id else None
        rows.append((record.get("item", path.stem), record.get("elapsed_s"), trace))

    print(f"\nTrace readout — {len(rows)} item(s) against {args.db}\n")
    header = (
        f"{'item':<5}{'trace':<10}{'wall':>7}{'model':>8}{'tools':>7}{'iters':>7}"
        f"{'fallback':>10}{'in_tok':>9}{'out_tok':>9}  drops"
    )
    print(header)
    print("-" * len(header))

    totals = Counter()
    for item, wall, trace in rows:
        if not trace:
            print(f"{item:<5}{'-':<10}{wall or 0:>7.1f}   no trace")
            continue
        seconds = (trace["duration_ms"] or 0) / 1000
        totals["tool_calls"] += trace["tool_calls"]
        totals["ceilings"] += trace["hit_ceiling"]
        totals["drops"] += len(trace["dropped"])
        totals["in"] += trace["prompt_tokens"] or 0
        totals["out"] += trace["completion_tokens"] or 0
        print(
            f"{item:<5}{trace['trace_id']:<10}{wall or 0:>7.1f}{seconds:>8.1f}"
            f"{trace['tool_calls']:>7}{trace['iterations']:>7}"
            f"{'YES' if trace['hit_ceiling'] else '-':>10}"
            f"{trace['prompt_tokens'] or 0:>9}{trace['completion_tokens'] or 0:>9}"
            f"  {', '.join(trace['dropped'])}"
        )

    tool_totals = Counter()
    for _, _, trace in rows:
        if trace:
            tool_totals.update(trace["tools"])

    print(
        f"\n  {totals['tool_calls']} tool calls · {totals['ceilings']} turns hit the "
        f"iteration ceiling · {totals['drops']} dropped citation handles\n"
        f"  tokens: {totals['in']:,} in, {totals['out']:,} out\n"
    )
    print("  tool usage across the round:")
    for name, count in tool_totals.most_common():
        print(f"    {count:>4}  {name}")
    print()


if __name__ == "__main__":
    main()
