"""Replay captured answers through the citation gate to see what it would have shipped.

    python benchmarks/replay_gate.py <answers-dir-or-file.json> [--db sbpeye.db]

`check_citations.py` counts the broken source links in an answer. This runs the same
answers through :mod:`sbpeye.citation_handles` — the gate that now sits between the model
and the reader — and counts what is left afterwards. It is the cheap half of measuring the
fix: deterministic, offline, and it re-uses the round that actually failed rather than
waiting on a fresh one.

What it cannot show is whether the *model* now cites better. The gate removes dead links; it
does not make a wrong-but-valid citation right. That still needs a live round and a rater.

Each answer is replayed against a handle map seeded from the citations in that answer whose
ids exist in the database — standing in for the documents the turn had actually offered.
A token surviving the gate therefore means the reader gets a link that resolves.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from check_citations import TABLES, TOKEN, check, known_ids  # noqa: E402
from sbpeye.citation_handles import CitationHandles  # noqa: E402


def replay(answer: str, ids: dict[str, set[str]]) -> str:
    """Answer as the gate would render it, given the sources that turn could offer."""
    handles = CitationHandles()
    for kind, raw, label in TOKEN.findall(answer):
        identifier = (raw or "").strip()
        if identifier in ids[kind]:
            handles.mint(kind, identifier, (label or "").strip())
    return handles.expand(answer)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="answers directory, or a single answer .json")
    parser.add_argument("--db", default="sbpeye.db")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    ids = {kind: known_ids(db, kind) for kind in TABLES}
    target = Path(args.target)
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    if not files:
        sys.exit(f"{target}: no answer files found")

    print(f"\nCitation gate replay — {len(files)} answer(s) against {args.db}\n")
    print(f"  {'item':<6}{'tokens':>8}{'broken':>8}{'after':>8}")

    totals = [0, 0, 0]
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        answer = record.get("answer", "")
        gated = replay(answer, ids)
        counts = (
            len(TOKEN.findall(answer)),
            len(check(answer, ids)),
            len(check(gated, ids)),
        )
        totals = [running + value for running, value in zip(totals, counts)]
        item = record.get("item", path.stem)
        print(f"  {item:<6}{counts[0]:>8}{counts[1]:>8}{counts[2]:>8}")

    print(f"\n  {'total':<6}{totals[0]:>8}{totals[1]:>8}{totals[2]:>8}")
    if totals[2]:
        print(f"\n  {totals[2]} broken citation(s) still reach the reader.\n")
        sys.exit(1)
    print(f"\n  All {totals[1]} broken citation(s) stopped at the gate; "
          f"{totals[0] - totals[1]} good one(s) preserved.\n")


if __name__ == "__main__":
    main()
