"""Check every citation token in an answer against the database.

    python benchmarks/check_citations.py <answers-dir-or-file.json> [--db sbpeye.db]

SBPEye answers cite sources as ``[[circular:ID|label]]``, ``[[attachment:ID|label]]`` or
``[[law:ID|label]]``, where the ID must be one the model was given verbatim in a tool
result. A token whose ID is malformed, or well-formed but absent from the database, renders
as a broken source link.

**This is an engineering diagnostic, not a benchmark input.** These IDs are internal keys
the user never sees — what an auditor reads is the label, "BPRD Circular No. 05 of 2020".
Grounding is therefore scored on whether that *reference* is right (rubric dimension B),
not on whether the link behind it resolves. A broken link on a correctly named circular is
a defect to fix, not marks to deduct, and scoring it would make the benchmark unusable on
any system that does not emit these tokens.

Use it to catch link rot after changes to the citation path, and to measure a fix. It
cannot tell whether a *valid* citation actually supports the sentence it is attached to.
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

TOKEN = re.compile(r"\[\[(circular|attachment|law):([^\]|]*)(?:\|([^\]]*))?\]\]")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

TABLES = {"circular": "circulars", "attachment": "attachments", "law": "reg_documents"}


def known_ids(db: sqlite3.Connection, kind: str) -> set[str]:
    return {row[0] for row in db.execute(f"select id from {TABLES[kind]}")}


def check(answer: str, ids: dict[str, set[str]]) -> list[dict]:
    findings = []
    for kind, raw, label in TOKEN.findall(answer):
        identifier = raw.strip()
        if not UUID.match(identifier):
            verdict = "MALFORMED"
        elif identifier not in ids[kind]:
            verdict = "NOT FOUND"
        else:
            continue  # a real id — nothing to report
        findings.append(
            {"kind": kind, "id": identifier, "label": (label or "").strip(), "verdict": verdict}
        )
    return findings


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

    flagged = 0
    print(f"\nCitation check — {len(files)} answer(s) against {args.db}\n")

    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        answer = record.get("answer", "")
        total = len(TOKEN.findall(answer))
        findings = check(answer, ids)

        if not total:
            print(f"  {record.get('item', path.stem):<5} no citation tokens emitted")
            continue
        if not findings:
            print(f"  {record.get('item', path.stem):<5} OK      {total} token(s), all resolve")
            continue

        flagged += 1
        print(f"  {record.get('item', path.stem):<5} FLAG    {len(findings)} of {total} token(s) do not resolve")
        for finding in findings:
            shown = finding["id"] if finding["id"] else "(empty)"
            label = f'  "{finding["label"]}"' if finding["label"] else ""
            print(f"           {finding['verdict']:<10} {finding['kind']}:{shown}{label}")

    print(f"\n  {flagged} of {len(files)} answer(s) contain at least one unresolvable citation.")
    print("  Tick F on those items unless the rater judges the token a display artefact.\n")


if __name__ == "__main__":
    main()
