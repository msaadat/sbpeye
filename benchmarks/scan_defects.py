"""Mechanical defect probes over a round's captured answers.

    python benchmarks/scan_defects.py benchmarks/results/2026-08-26-sbpeye/answers

Counts recurring answer-quality flaws off the answer text rather than off a rater's
judgement, so a round can be compared with an earlier one on the things that do not need
a human. It is a diagnostic, not a grading input — the same standing as
`check_citations.py`.

The `D` numbers below are stable ids from this project's own defect catalogue, kept
because they are the keys in the `--json` output and because they are how these flaws are
referred to elsewhere. Each probe is deliberately shallow, and the plain-English column is
the definition — nothing here depends on having the catalogue to hand:

  D14  runaway generation      longest consecutive repeat of a short substring
  D2   corrupted emission      not detectable mechanically; length outliers only
  D4   malformed handles       `[[` that no citation token accounts for
  D5   filename-as-reference   attachment tokens whose label is a filename
  D8   retrieval vocabulary    our own internal terms surfacing in prose
  D9   thinking-aloud preamble opening sentence patterns
  D10  closing offer           trailing offer-to-continue patterns
  D13  length                  characters, against the item's tier

A probe firing is a prompt to go and read that answer. None of them is a verdict, and
the corruption class (D2) in particular has no reliable mechanical signature — it is
listed so the absence of a probe for it stays visible.

**The probes are not equally durable, and a zero does not mean the same thing in each.**
D4, D5, D13 and D14 are *structural* — they match on shape, so they stay true however the
prompts are worded. D8, D9 and D10 match *phrases*, and those phrases were read off the
2026-08-23 answers and the prompt strings that supplied them. Rewriting a prompt retires
its phrases and the probe goes quiet whether or not the behaviour stopped: the 2026-08-26
round scored 0 leaks while P10 said "the SBP circulars and regulations available to me",
which is the same habit in vocabulary this list does not carry. Re-read a sample by hand
before reporting a clean phrase probe, and add the new wording here when you find it.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent

TOKEN = re.compile(r"\[\[(circular|attachment|law):([^\]|\n]*)(?:\|([^\]\n]*))?\]\]")

# "| P01 | Easy | Concept / definition |" from the question set's tier table.
TIER_ROW = re.compile(r"^\|\s*(P\d+)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)

# D8 — phrases that describe our plumbing rather than the regulator's instruments. Each
# one is quoted from an observed answer or from the prompt string that supplied it.
LEAK_PATTERNS = [
    r"retrieved passage",
    r"retrieved document",
    r"database lookup",
    r"lookup result",
    r"in my database",
    r"the provided sources",
    r"provided circulars",
    r"available in my database",
    r"as extracted",
    r"regulatory value database",
    r"in the excerpts",
    r"excerpts I could retrieve",
    r"the search results",
]

# D9 — narration of the system's own process, before the answer starts.
PREAMBLE_PATTERNS = [
    r"^I now have",
    r"^Let me provide",
    r"^I've traced",
    r"^I have traced",
    r"^Based on the retrieved",
    r"^Based on the latest",
    r"^I can answer this directly",
    r"^Here is the summary",
    r"^Let me (search|look|check)",
    r"^I'?ll (search|look|check)",
]

# D10 — an offer standing in for content. Checked against the tail only.
CLOSING_PATTERNS = [
    r"Would you like",
    r"Let me know if",
    r"I can also (provide|fetch|pull|retrieve|share)",
    r"if you'?d like",
    r"Do you want me to",
    r"happy to (provide|fetch|pull|share)",
    r"Shall I",
]

def load_tiers(path: Path) -> dict[str, str]:
    """Tiers, read from the question set rather than restated here.

    `pilot-v1-questions.md` calls itself the single source of truth and `run_pilot.py`
    parses the questions out of it for that reason. A second copy of the tier column in
    this file would go stale the first time an item is re-tiered or the set is enlarged —
    and it would go stale silently, mislabelling every row it printed.
    """
    tiers = {
        item: ("abstain" if tier in {"—", "-", ""} else tier)
        for item, tier in TIER_ROW.findall(path.read_text(encoding="utf-8"))
    }
    if not tiers:
        raise SystemExit(f"{path}: no tier table found (expected rows like '| P01 | Easy | …')")
    return tiers


def longest_repeat(text: str, unit_max: int = 20, min_runs: int = 8) -> tuple[str, int]:
    """The most-repeated short substring that repeats *consecutively*, and its run length.

    Degeneration in this system's observed form is a short unit repeated thousands of
    times (`C-C-C-C-…`), which no vocabulary check would catch but a run-length scan
    finds immediately.
    """
    best = ("", 0)
    for unit in range(1, unit_max + 1):
        index = 0
        while index + unit <= len(text):
            piece = text[index : index + unit]
            if not piece.strip():
                index += 1
                continue
            runs = 1
            while text[index + runs * unit : index + (runs + 1) * unit] == piece:
                runs += 1
            if runs >= min_runs and runs > best[1]:
                best = (piece, runs)
            index += max(1, runs * unit)
    return best


def unaccounted_brackets(answer: str) -> int:
    """`[[` occurrences no well-formed citation token explains — D4's invisible half."""
    return answer.count("[[") - len(TOKEN.findall(answer))


def looks_like_filename(label: str) -> bool:
    return bool(re.match(r"^[\w.\-]+\.(pdf|PDF|docx?|xlsx?)$", label.strip()))


def scan(record: dict, tiers: dict[str, str]) -> dict:
    answer = record.get("answer", "") or ""
    head = answer.lstrip()[:200]
    tail = answer[-500:]
    tokens = TOKEN.findall(answer)

    unit, runs = longest_repeat(answer)
    return {
        "item": record.get("item", "?"),
        "tier": tiers.get(record.get("item", ""), "-"),
        "chars": len(answer),
        "elapsed_s": record.get("elapsed_s"),
        "session_id": record.get("session_id"),
        "error": record.get("error"),
        "citations": len(tokens),
        "attachment_filename_cites": sum(
            1 for kind, _, label in tokens if kind == "attachment" and looks_like_filename(label)
        ),
        "D14_repeat": f"{unit!r}x{runs}" if runs >= 8 else "",
        "D4_stray_brackets": unaccounted_brackets(answer),
        "D8_leaks": sorted({
            p for p in LEAK_PATTERNS if re.search(p, answer, re.IGNORECASE)
        }),
        "D9_preamble": next(
            (p for p in PREAMBLE_PATTERNS if re.search(p, head, re.IGNORECASE)), ""
        ),
        "D10_closing": next(
            (p for p in CLOSING_PATTERNS if re.search(p, tail, re.IGNORECASE)), ""
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("answers", help="a round's answers directory")
    parser.add_argument("--questions", default=str(HERE / "pilot-v1-questions.md"))
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args()

    files = sorted(Path(args.answers).glob("P*.json"))
    if not files:
        raise SystemExit(f"{args.answers}: no answer files found")

    tiers = load_tiers(Path(args.questions))
    rows = [scan(json.loads(path.read_text(encoding="utf-8")), tiers) for path in files]

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    print(f"\nDefect probes — {len(rows)} answer(s) in {args.answers}\n")
    header = f"{'item':<5}{'tier':<8}{'chars':>7}{'secs':>7}{'cites':>6}{'fn':>4}{'[[':>4}  flags"
    print(header)
    print("-" * len(header))
    for row in rows:
        flags = []
        if row["error"]:
            flags.append(f"ERROR {row['error']}")
        if row["D14_repeat"]:
            flags.append(f"D14 {row['D14_repeat']}")
        if row["D8_leaks"]:
            flags.append(f"D8 {len(row['D8_leaks'])}: {'; '.join(row['D8_leaks'])}")
        if row["D9_preamble"]:
            flags.append(f"D9 {row['D9_preamble']}")
        if row["D10_closing"]:
            flags.append(f"D10 {row['D10_closing']}")
        print(
            f"{row['item']:<5}{row['tier']:<8}{row['chars']:>7}{row['elapsed_s'] or 0:>7.1f}"
            f"{row['citations']:>6}{row['attachment_filename_cites']:>4}"
            f"{row['D4_stray_brackets']:>4}  {' | '.join(flags)}"
        )

    totals = Counter()
    for row in rows:
        totals["chars"] += row["chars"]
        totals["citations"] += row["citations"]
        totals["attachment_filename_cites"] += row["attachment_filename_cites"]
        totals["stray_brackets"] += max(0, row["D4_stray_brackets"])
        totals["D8"] += bool(row["D8_leaks"])
        totals["D9"] += bool(row["D9_preamble"])
        totals["D10"] += bool(row["D10_closing"])
        totals["D14"] += bool(row["D14_repeat"])

    lengths = sorted(row["chars"] for row in rows)
    median = lengths[len(lengths) // 2]
    print(
        f"\n  {totals['citations']} citation tokens, {totals['attachment_filename_cites']} of them "
        f"filename-labelled (D5)\n"
        f"  {totals['stray_brackets']} unaccounted '[[' (D4, the half the drop log never sees)\n"
        f"  D8 leaks in {totals['D8']} answers · D9 preamble in {totals['D9']} · "
        f"D10 closing offer in {totals['D10']} · D14 repetition in {totals['D14']}\n"
        f"  length: median {median}, range {lengths[0]}–{lengths[-1]}\n"
    )


if __name__ == "__main__":
    main()
