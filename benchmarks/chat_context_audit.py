"""What a chat turn actually sends to the model, read off the trace database.

Every figure in `docs/CHAT_CONTEXT_PLAN.md` comes from here. It reads
`sbpeye_debug.db` — the LLM trace recorder's own file, written when
`llm_debug_enabled` is true — and never touches the corpus or a provider.

    .venv/bin/python benchmarks/chat_context_audit.py

The unit that matters is the **peak single request** of a turn, not the turn's
total: a 250k-window model rejects one oversized request, and the turn's total is
spread across five of them. `--section` limits the run:

    peak      per-turn peak request, and how many breach a given window
    fields    where a tool result's bytes go, per tool
    waste     redundancy: re-sent documents, repeat calls, uncited full letters
    cache     provider-side prompt-cache hit rate per stage
    simulate  the peak after each proposed change in the plan
    turn      one turn in full — pass --trace or --session

`--window` sets the ceiling breach counts are reported against (default 250000).

Token counts are estimated from characters. `chars / 4` is the median over 128
measured requests, but the densest request seen ran at 2.93 chars/token, so
anything used as a *ceiling* here divides by `SAFE_CHARS_PER_TOKEN` instead —
see §7 of the plan.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEBUG_DB = ROOT / "sbpeye_debug.db"

# Median chars/token over the traced chat requests. Fine for reporting.
CHARS_PER_TOKEN = 4.0
# Densest request observed (2.93). What a hard gate must budget with.
SAFE_CHARS_PER_TOKEN = 3.0

# Every list key a tool payload uses to carry document-shaped entries.
DOC_LISTS = ("lexical_results", "semantic_results", "results", "law_results", "reference_matches")

# Characters a "you already have this" stub costs, measured on the shape in §3.1.
STUB_CHARS = 130
# Assistant tool-call message overhead per round, median over the traced turns.
ASSISTANT_OVERHEAD = 450

# Circular handles only: the cited set below is circulars, and comparing it
# against every handle kind would count laws as offered-but-uncited.
HANDLE_RE = re.compile(r"\[\[c:([^\]|]*)\]\]")


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"no trace database at {path} — set llm_debug_enabled and run some chats")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def tokens(chars: int, safe: bool = False) -> int:
    return int(chars / (SAFE_CHARS_PER_TOKEN if safe else CHARS_PER_TOKEN))


def chat_traces(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return db.execute(
        "select id, started_at, coalesce(chat_session_id,'') from llm_traces "
        "where operation='chat.turn' order by started_at"
    ).fetchall()


def events(db: sqlite3.Connection, trace_id: str) -> list[tuple[str, str, dict]]:
    rows = db.execute(
        "select kind, coalesce(stage,''), payload_json from llm_trace_events "
        "where trace_id=? order by sequence",
        [trace_id],
    ).fetchall()
    return [(kind, stage, json.loads(payload)) for kind, stage, payload in rows]


def request_chars(kwargs: dict) -> int:
    """What this request puts on the wire: the messages plus the tool schema."""
    messages = json.dumps(kwargs.get("messages") or [])
    schema = json.dumps(kwargs.get("tools") or [])
    return len(messages) + len(schema)


# --------------------------------------------------------------------------- peak


def section_peak(db: sqlite3.Connection, window: int) -> None:
    peaks: list[tuple[str, str, int, str]] = []
    for trace_id, started, session in chat_traces(db):
        biggest = 0
        stage_of = ""
        for kind, stage, payload in events(db, trace_id):
            if kind != "provider_request":
                continue
            size = request_chars(payload["kwargs"])
            if size > biggest:
                biggest, stage_of = size, stage
        if biggest:
            peaks.append((started, session[:8], biggest, stage_of))

    if not peaks:
        print("no chat turns traced")
        return

    sizes = [p[2] for p in peaks]
    print(f"turns: {len(peaks)}")
    print(
        f"peak request  median {tokens(statistics.median(sizes)):>8,} tok"
        f"   p90 {tokens(sorted(sizes)[int(len(sizes) * 0.9) - 1]):>8,} tok"
        f"   max {tokens(max(sizes)):>8,} tok"
    )
    print(f"\nturns whose peak breaches a window ({len(peaks)} turns):")
    for limit in (32_768, 65_536, 131_072, window):
        over = sum(1 for s in sizes if tokens(s) > limit)
        print(f"  > {limit:>9,} tok : {over:3d}  ({over / len(peaks) * 100:4.0f}%)")

    print("\nlargest turns:")
    for started, session, size, stage in sorted(peaks, key=lambda p: -p[2])[:10]:
        print(f"  {started[:19]}  sess={session}  {tokens(size):>8,} tok  at {stage}")


# --------------------------------------------------------------------------- fields


def section_fields(db: sqlite3.Connection) -> None:
    field: dict[tuple[str, str], int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    calls: Counter = Counter()

    for trace_id, _started, _session in chat_traces(db):
        for kind, _stage, payload in events(db, trace_id):
            if kind != "tool_result" or not payload.get("success"):
                continue
            name = payload.get("name") or "?"
            result = payload.get("result") or ""
            total[name] += len(result)
            calls[name] += 1
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            for key, value in parsed.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    for item in value:
                        for inner, payload_value in item.items():
                            field[(name, inner)] += len(json.dumps(payload_value))
                else:
                    field[(name, f"<{key}>")] += len(json.dumps(value))

    for name in sorted(total, key=lambda n: -total[n]):
        avg = total[name] // max(calls[name], 1)
        print(f"\n{name}  n={calls[name]}  total {total[name]:,} ch  avg {avg:,} ch (~{tokens(avg):,} tok)")
        rows = sorted(((k[1], v) for k, v in field.items() if k[0] == name), key=lambda kv: -kv[1])
        for key, value in rows[:10]:
            print(f"    {key:32s} {value:>10,}  {value / total[name] * 100:5.1f}%")


# --------------------------------------------------------------------------- waste


def _entries(parsed: dict):
    for key in DOC_LISTS:
        value = parsed.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield key, item


def section_waste(db: sqlite3.Connection) -> None:
    from sbpeye.citation_handles import slugify

    tool_chars = repeat = resent = superseded = 0
    body_chars = body_cited = 0
    offered_total = cited_total = 0

    for trace_id, _started, _session in chat_traces(db):
        results: list[tuple[str, dict, str]] = []
        answer = ""
        for kind, _stage, payload in events(db, trace_id):
            if kind == "tool_result" and payload.get("success"):
                results.append((payload.get("name") or "?", payload.get("arguments") or {}, payload.get("result") or ""))
            elif kind == "normalized_result":
                answer = payload.get("response") or ""
        if not results:
            continue
        tool_chars += sum(len(r) for _n, _a, r in results)

        seen_bodies: set[str] = set()
        for _name, _args, raw in results:
            if raw in seen_bodies:
                repeat += len(raw)
            seen_bodies.add(raw)

        cited = {slugify(label) for label in re.findall(r"\[\[circular:[^|\]]*\|([^\]]*)\]\]", answer)}
        offered = set(HANDLE_RE.findall(" ".join(r for _n, _a, r in results)))
        if offered:
            offered_total += len(offered)
            cited_total += len(cited & offered)

        seen_docs: set[str] = set()
        detailed: set[str] = set()
        for name, _args, raw in results:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            for _key, item in _entries(parsed):
                citation = item.get("citation")
                if not citation:
                    continue
                if citation in seen_docs:
                    resent += len(json.dumps(item))
                seen_docs.add(citation)
                body = item.get("full_circular_text")
                if body:
                    body_chars += len(body)
                    handle = HANDLE_RE.match(str(citation))
                    if handle and handle.group(1).strip() in cited:
                        body_cited += len(body)
            if name.startswith("get_") and parsed.get("citation"):
                detailed.add(parsed["citation"])

        for name, _args, raw in results:
            if name.startswith("get_"):
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            for _key, item in _entries(parsed):
                if item.get("citation") in detailed:
                    superseded += len(json.dumps(item))

    print(f"tool output across all turns: {tool_chars:,} chars")
    for label, value in (
        ("same document re-sent in a later call", resent),
        ("search payload superseded by a later get_*_details", superseded),
        ("identical repeat results", repeat),
        ("full letters sent for circulars never cited", body_chars - body_cited),
    ):
        print(f"  {label:52s} {value:>10,}  {value / max(tool_chars, 1) * 100:5.1f}%")
    if offered_total:
        print(
            f"\ncirculars offered {offered_total}, cited {cited_total} "
            f"({cited_total / offered_total * 100:.1f}%)"
        )


# --------------------------------------------------------------------------- cache


def section_cache(db: sqlite3.Connection) -> None:
    agg: dict[str, list] = defaultdict(lambda: [0, 0, 0])
    for trace_id, _started, _session in chat_traces(db):
        for kind, stage, payload in events(db, trace_id):
            if kind != "provider_response":
                continue
            usage = payload.get("usage") or {}
            prompt = usage.get("prompt_tokens")
            if not prompt:
                continue
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            row = agg[stage]
            row[0] += 1
            row[1] += prompt
            row[2] += cached

    if not agg:
        print("no usage reported by the provider")
        return
    print(f"{'stage':24s} {'n':>3s} {'prompt':>11s} {'cached':>11s} {'hit':>6s} {'uncached':>11s}")
    grand = [0, 0, 0]
    for stage in sorted(agg):
        count, prompt, cached = agg[stage]
        grand = [grand[0] + count, grand[1] + prompt, grand[2] + cached]
        print(
            f"{stage:24s} {count:3d} {prompt:>11,} {cached:>11,} "
            f"{cached / prompt * 100:5.1f}% {prompt - cached:>11,}"
        )
    count, prompt, cached = grand
    print(
        f"{'TOTAL':24s} {count:3d} {prompt:>11,} {cached:>11,} "
        f"{cached / prompt * 100:5.1f}% {prompt - cached:>11,}"
    )


# --------------------------------------------------------------------------- simulate

VARIANTS: dict[str, dict[str, bool]] = {
    "baseline": {},
    "C1 document ledger": {"ledger": True},
    "C2 tiered full text": {"tier": True},
    "C4 merged arms": {"merge": True},
    "C5 repeat guard": {"repeat": True},
    "C7 inventory trim": {"inventory": True},
    "all of the above": {"ledger": True, "tier": True, "merge": True, "repeat": True, "inventory": True},
}


def _transform(name: str, raw: str, ledger: dict, seen: set, opts: dict) -> int:
    """The characters this tool result would occupy under `opts`."""
    if opts.get("repeat") and raw in seen:
        return STUB_CHARS
    seen.add(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return len(raw)
    if not isinstance(parsed, dict):
        return len(raw)

    if name.startswith("get_") and parsed.get("citation"):
        citation = parsed["citation"]
        if opts.get("ledger") and ledger.get(citation, 0) >= 3:
            return STUB_CHARS
        ledger[citation] = 3
        return len(raw)

    total = len(json.dumps({k: v for k, v in parsed.items() if k not in DOC_LISTS}))
    in_call: set[str] = set()
    for key, item in _entries(parsed):
        citation = item.get("citation")
        if opts.get("merge") and key == "semantic_results" and citation in in_call:
            total += STUB_CHARS
            continue
        if key in ("lexical_results", "semantic_results"):
            in_call.add(citation)
        if opts.get("ledger") and citation and ledger.get(citation, 0) >= 2:
            total += STUB_CHARS
            continue
        item = dict(item)
        if opts.get("tier") and item.get("full_circular_text"):
            lexical, semantic = item.get("lexical_rank"), item.get("semantic_rank")
            top = (isinstance(lexical, int) and lexical <= 3) or (isinstance(semantic, int) and semantic <= 3)
            if not top:
                item.pop("full_circular_text")
        if opts.get("inventory") and name == "search_regulatory_inventory" and item.get("passage"):
            item["passage"] = item["passage"][:200]
        if citation:
            ledger[citation] = max(ledger.get(citation, 0), 3 if item.get("full_circular_text") else 2)
        total += len(json.dumps(item))
    return total


def _replay(db: sqlite3.Connection, trace_id: str, opts: dict) -> int | None:
    prefix = None
    results: list[tuple[str, str]] = []
    for kind, stage, payload in events(db, trace_id):
        if kind == "provider_request" and stage == "chat.iteration.1":
            prefix = request_chars(payload["kwargs"])
        elif kind == "tool_result" and payload.get("success"):
            results.append((payload.get("name") or "?", payload.get("result") or ""))
    if prefix is None or not results:
        return None
    ledger: dict = {}
    seen: set = set()
    running = prefix
    peak = prefix
    for name, raw in results:
        running += _transform(name, raw, ledger, seen, opts) + ASSISTANT_OVERHEAD
        peak = max(peak, running)
    return peak


def section_simulate(db: sqlite3.Connection, window: int) -> None:
    traces = [t[0] for t in chat_traces(db)]
    results: dict[str, list[int]] = {}
    for label, opts in VARIANTS.items():
        peaks = [p for p in (_replay(db, t, opts) for t in traces) if p]
        if peaks:
            results[label] = peaks

    base = results["baseline"]
    b_med, b_p90, b_max = statistics.median(base), sorted(base)[int(len(base) * 0.9) - 1], max(base)
    print(f"peak request per turn, {len(base)} turns\n")
    print(f"{'variant':22s} {'median':>10s} {'p90':>10s} {'max':>10s}   {'med cut':>8s} {'p90 cut':>8s} {'>window':>8s}")
    for label, peaks in results.items():
        med = statistics.median(peaks)
        p90 = sorted(peaks)[int(len(peaks) * 0.9) - 1]
        top = max(peaks)
        over = sum(1 for p in peaks if tokens(p) > window)
        print(
            f"{label:22s} {tokens(med):>8,}t {tokens(p90):>8,}t {tokens(top):>8,}t   "
            f"{(1 - med / b_med) * 100:7.1f}% {(1 - p90 / b_p90) * 100:7.1f}% {over:>8d}"
        )
    print(
        "\nNote: a simulation, not a measurement. Validated against the traces it replays —"
        "\nsimulated baseline / actual largest request: median 0.98 (min 0.93, max 1.44)."
    )


# --------------------------------------------------------------------------- turn


def section_turn(db: sqlite3.Connection, trace_id: str | None, session: str | None) -> None:
    if session and not trace_id:
        row = db.execute(
            "select id from llm_traces where operation='chat.turn' and chat_session_id=? "
            "order by started_at desc limit 1",
            [session],
        ).fetchone()
        if not row:
            raise SystemExit(f"no chat turn traced for session {session}")
        trace_id = row[0]
    if not trace_id:
        row = db.execute(
            "select id from llm_traces where operation='chat.turn' order by started_at desc limit 1"
        ).fetchone()
        if not row:
            raise SystemExit("no chat turns traced")
        trace_id = row[0]

    meta = db.execute(
        "select started_at, status, model, duration_ms from llm_traces where id=?", [trace_id]
    ).fetchone()
    print(f"trace {trace_id}  {meta[0][:19]}  {meta[1]}  {meta[2]}  {meta[3]}ms\n")

    total_prompt = total_cached = 0
    for kind, stage, payload in events(db, trace_id):
        if kind == "context":
            print(f"  question: {payload.get('user_message', '')!r}")
            print(f"  selected: {payload.get('selected_circular_ids')}\n")
        elif kind == "provider_request":
            size = request_chars(payload["kwargs"])
            count = len(payload["kwargs"].get("messages") or [])
            print(f"  >>> {stage:22s} {count:2d} messages  {size:>9,} ch  ~{tokens(size):>7,} tok")
        elif kind == "provider_response":
            usage = payload.get("usage") or {}
            prompt = usage.get("prompt_tokens") or 0
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            total_prompt += prompt
            total_cached += cached
            print(
                f"  <<< {stage:22s} prompt {prompt:>7,}  cached {cached:>7,}"
                f"  new {prompt - cached:>7,}"
            )
        elif kind == "tool_result":
            args = json.dumps(payload.get("arguments") or {})
            print(f"        {payload.get('name')}({args[:96]}) -> {len(payload.get('result') or ''):,} ch")
        elif kind == "normalized_result":
            print(f"\n  answer: {len(payload.get('response') or ''):,} chars")
    if total_prompt:
        print(f"  turn total: {total_prompt:,} prompt tokens, {total_cached:,} cached")


SECTIONS = ("peak", "fields", "waste", "cache", "simulate", "turn")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEBUG_DB)
    parser.add_argument("--section", choices=SECTIONS)
    parser.add_argument("--window", type=int, default=250_000)
    parser.add_argument("--trace")
    parser.add_argument("--session")
    args = parser.parse_args()

    db = connect(args.db)
    wanted = [args.section] if args.section else [s for s in SECTIONS if s != "turn"]
    for name in wanted:
        print(f"\n{'=' * 78}\n### {name}\n{'=' * 78}")
        if name == "peak":
            section_peak(db, args.window)
        elif name == "fields":
            section_fields(db)
        elif name == "waste":
            section_waste(db)
        elif name == "cache":
            section_cache(db)
        elif name == "simulate":
            section_simulate(db, args.window)
        elif name == "turn":
            section_turn(db, args.trace, args.session)
    db.close()


if __name__ == "__main__":
    main()
