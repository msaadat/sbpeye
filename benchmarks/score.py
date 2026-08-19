"""Turn a filled-in rater scoresheet into the numbers that go in front of management.

    python benchmarks/score.py benchmarks/round-2026-08.csv

Reads the CSV produced from `scoresheet-template.csv` and prints, per system: the overall
score, the hallucination rate, the split by difficulty tier, and the per-dimension means.
Where two raters scored the same item it averages them and reports their agreement, because
a comparison between systems means nothing until the raters agree with each other.

Scoring is defined in `rubric-v1.md`; the weights below are the only place it is encoded.
"""

import csv
import sys
from collections import defaultdict
from statistics import mean

WEIGHTS = {"A": 35, "B": 20, "C": 15, "D": 15, "E": 15}
# Two item scores this far apart mean the raters read the rubric differently, not that the
# answer was borderline. Rubric section 5 puts the acceptable rate of that at 20%.
AGREEMENT_TOLERANCE = 10
# A silently substituted answer can still be well-formed prose, so it keeps partial credit
# rather than zeroing — but never enough to look like a pass. See rubric section 3a.
SUBSTITUTION_CAP = 25


def ticked(row: dict, column: str) -> bool:
    return (row.get(column) or "").strip().upper() in {"Y", "YES", "1", "TRUE"}


def item_score(row: dict) -> float:
    """Score one rated item, 0-100.

    A fabrication zeroes it (rubric section 3); a silent substitution caps it at 25
    (section 3a) — the facts may be right, but they answer about the wrong instrument.
    """
    if ticked(row, "F"):
        return 0.0
    if (row.get("mode") or "").strip() == "abstain":
        raw = {0: 0.0, 1: 50.0, 2: 100.0}[int(row["abstain"])]
    else:
        raw = sum(WEIGHTS[k] * int(row[k]) for k in WEIGHTS) / 2
    return min(raw, SUBSTITUTION_CAP) if ticked(row, "S") else raw


def fabricated(row: dict) -> bool:
    return ticked(row, "F")


def problem(row: dict) -> str | None:
    """Why this row cannot be scored, or None if it can.

    A half-filled row is excluded rather than guessed at — a benchmark that quietly
    treats a blank cell as a zero reports a number nobody can defend.
    """
    mode = (row.get("mode") or "").strip()
    if ticked(row, "F"):
        return None  # a fabrication scores 0 whatever the dimensions say

    if mode == "abstain":
        required = ["abstain"]
    elif mode == "five":
        required = list(WEIGHTS)
    else:
        return f"unknown mode {mode!r} (expected 'five' or 'abstain')"

    missing = [key for key in required if not (row.get(key) or "").strip()]
    if missing:
        return "not scored" if len(missing) == len(required) else f"missing {', '.join(missing)}"

    for key in required:
        value = (row.get(key) or "").strip()
        if value not in {"0", "1", "2"}:
            return f"{key}={value!r} is not 0, 1 or 2"
    return None


def load(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        all_rows = [
            (index, row)
            for index, row in enumerate(csv.DictReader(handle), start=2)
            if (row.get("system") or "").strip()
        ]
    if not all_rows:
        sys.exit(f"{path}: no rows with a 'system' value — fill in the system column.")

    rows, skipped = [], []
    for lineno, row in all_rows:
        reason = problem(row)
        (skipped.append((lineno, row, reason)) if reason else rows.append(row))

    if skipped:
        print(f"\n  Excluded {len(skipped)} unscorable row(s):")
        for lineno, row, reason in skipped:
            item = (row.get("item") or "?").strip()
            print(f"    line {lineno}  {item:<5} {reason}")

    if not rows:
        sys.exit(
            f"\n{path}: nothing scored yet.\n"
            "Fill in A,B,C,D,E (0-2) for 'five' rows and the abstain column (0-2) for\n"
            "'abstain' rows, then run again. Score against pilot-v1-answer-key.md using\n"
            "rubric-v1.md. Ticking F alone is enough to score an item 0.\n"
        )
    return rows


def main(path: str) -> None:
    rows = load(path)

    # (system, item) -> one entry per rater, so disagreement stays visible.
    by_item: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_item[(row["system"].strip(), row["item"].strip())].append(row)

    systems = sorted({system for system, _ in by_item})
    spreads: list[float] = []

    print(f"\nSBPEye Chat Benchmark — {path}")
    print(f"{len(by_item)} item-scores across {len(systems)} system(s)\n")

    summary = []
    for system in systems:
        items = {item: rated for (sys_, item), rated in by_item.items() if sys_ == system}
        scores, tiers, dims = {}, defaultdict(list), defaultdict(list)

        for item, rated in items.items():
            per_rater = [item_score(r) for r in rated]
            if len(per_rater) > 1:
                spreads.append(max(per_rater) - min(per_rater))
            scores[item] = mean(per_rater)
            tiers[rated[0].get("tier", "-").strip() or "-"].append(scores[item])
            for key in WEIGHTS:
                values = [int(r[key]) for r in rated if (r.get(key) or "").strip()]
                if values:
                    dims[key].append(mean(values))

        halluc = [item for item, rated in items.items() if any(fabricated(r) for r in rated)]
        subs = [item for item, rated in items.items() if any(ticked(r, "S") for r in rated)]
        overall = mean(scores.values())
        summary.append((system, overall, len(halluc), len(items)))

        print(f"  {system}")
        print(f"    Overall score        {overall:5.1f} %   (n={len(items)})")
        print(
            f"    Hallucination rate   {100 * len(halluc) / len(items):5.1f} %"
            + (f"   items: {', '.join(sorted(halluc))}" if halluc else "")
        )
        if subs:
            print(
                f"    Silent substitution  {100 * len(subs) / len(items):5.1f} %"
                f"   items: {', '.join(sorted(subs))}"
            )
        for tier in ("Easy", "Medium", "Hard", "-"):
            if tier in tiers:
                label = "Abstention" if tier == "-" else tier
                print(f"    {label:<20} {mean(tiers[tier]):5.1f} %   (n={len(tiers[tier])})")
        if dims:
            parts = " ".join(f"{k}={mean(v):.2f}" for k, v in sorted(dims.items()))
            print(f"    Dimensions (0-2)     {parts}")
        print()

    if spreads:
        agreed = sum(1 for s in spreads if s <= AGREEMENT_TOLERANCE) / len(spreads)
        print(f"  Rater agreement        {100 * agreed:.0f} % of double-scored items within {AGREEMENT_TOLERANCE} points")
        if agreed < 0.8:
            print("  ** Below 80 % — repair the rubric before reporting any comparison. **")
        print()

    if len(systems) == 2:
        left, right = systems
        wins = losses = ties = 0
        for item in {i for _, i in by_item}:
            a = mean(item_score(r) for r in by_item[(left, item)])
            b = mean(item_score(r) for r in by_item[(right, item)])
            wins += a > b
            losses += a < b
            ties += a == b
        print(f"  Paired: {left} beat {right} on {wins}, lost {losses}, tied {ties}")
        print("  (Per-question comparison cancels item difficulty — see rubric section 6.)\n")

    if len(summary) and min(count for *_, count in summary) < 30:
        print("  NOTE: fewer than 30 items per system. Treat differences between systems as")
        print("  indicative only — this size tests the rubric, it does not rank systems.\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
