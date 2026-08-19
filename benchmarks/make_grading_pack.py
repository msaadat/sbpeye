"""Assemble captured answers into one document a human can grade from.

    python benchmarks/make_grading_pack.py <answers-dir> --system SBPEye --out pack.md

Reads the per-item JSON written by a capture run and emits a single markdown file: one
section per item, carrying the question, the answer verbatim, the response time, and the
result of the mechanical citation check. The rater reads this beside the answer key and
fills in the scoresheet.

Citation findings are printed as an engineering diagnostic, NOT as a grading input. The
IDs they check are internal keys the user never sees; Grounding is scored on whether the
answer names the right circular reference (rubric dimension B). See check_citations.py.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from check_citations import TOKEN, check, known_ids, TABLES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("answers", help="directory of per-item answer .json files")
    parser.add_argument("--system", default="System under test")
    parser.add_argument("--db", default="sbpeye.db")
    parser.add_argument("--out", default="grading-pack.md")
    parser.add_argument("--model", default="", help="model id, recorded in the header")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    ids = {kind: known_ids(db, kind) for kind in TABLES}

    files = sorted(Path(args.answers).glob("*.json"))
    if not files:
        raise SystemExit(f"{args.answers}: no answer files")

    lines = [
        f"# Grading Pack — {args.system}",
        "",
        f"- **Items:** {len(files)}",
    ]
    if args.model:
        lines.append(f"- **Model:** `{args.model}`")
    lines += [
        "- **Score against:** `pilot-v1-answer-key.md` using `rubric-v1.md`",
        "- **Citation findings below are an engineering diagnostic, not a grading input.**",
        "  They check internal link IDs the user never sees. Score Grounding on whether the",
        "  answer names the right circular reference (e.g. \"BPRD Circular No. 05 of 2020\").",
        "",
        "---",
        "",
    ]

    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        answer = record.get("answer", "")
        findings = check(answer, ids)
        total = len(TOKEN.findall(answer))

        lines += [
            f"## {record.get('item', path.stem)}",
            "",
            f"**Question.** {record.get('question', '')}",
            "",
            f"*Response time: {record.get('elapsed_s', '?')}s · {len(answer)} characters*",
            "",
        ]
        if record.get("error"):
            lines += [f"> **TURN FAILED:** `{record['error']}`", ""]

        if total == 0:
            lines.append("*Citation check: no citation tokens emitted.*")
        elif not findings:
            lines.append(f"*Citation check: {total} token(s), all resolve to real records.*")
        else:
            lines.append(f"*Citation check: **{len(findings)} of {total} token(s) do not resolve**.*")
            lines.append("")
            for finding in findings:
                shown = finding["id"] or "(empty)"
                label = f' — labelled "{finding["label"]}"' if finding["label"] else ""
                lines.append(f"> - `{finding['verdict']}` `{finding['kind']}:{shown}`{label}")
        lines += ["", "### Answer", "", answer, "", "---", ""]

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out} ({len(files)} items)")


if __name__ == "__main__":
    main()
