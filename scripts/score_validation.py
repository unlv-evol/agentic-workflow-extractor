#!/usr/bin/env python3
"""Score the validation labels and emit the paper's macros.

    python3 scripts/score_validation.py -d validation/

Reads what `sample_validation.py` wrote plus the coders' filled-in sheets:

    precision_sample.csv, precision_strata.csv,
    precision_sheet_A.csv, precision_sheet_B.csv  [, precision_adjudication.csv]
    recall_sample.csv, recall_pool_size.csv,
    recall_sheet_A.csv, recall_sheet_B.csv        [, recall_adjudication.csv]

Either half may be absent. Where the coders disagree on Q1, an adjudication
sheet listing exactly those units is written for the coders to resolve; until
every disagreement has a `q1_final`, agreement is reported but precision is
not, because precision computed over half-resolved labels would be published
by accident.

Macros go to manuscripts/shared/numbers-validation.tex, which main.tex loads
after numbers.tex. Only figures that could be computed are written, so the rest
keep rendering as [??].
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEX = ROOT / "manuscripts" / "shared" / "numbers-validation.tex"

OVERALL_STRATA = ("P", "G", "U", "R", "F")
STRONG_STRATA = ("G", "U", "R", "F")
WEAK_STRATUM = "W"
GENERIC_STRATUM = "X"
# Pre-registered (PLAN.md, decision rules): the generic group joins the agentic
# counts only if the lower bound of its 95% Wilson interval reaches this.
GENERIC_FOLD_IN = 0.90

Z95 = 1.959963984540054
ANSWERS = {"y": "yes", "yes": "yes", "n": "no", "no": "no", "m": "maybe", "maybe": "maybe"}


class LabelError(ValueError):
    pass


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def norm_answer(value: str | None, where: str) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text not in ANSWERS:
        raise LabelError(f"{where}: '{value}' is not yes/no/maybe")
    return ANSWERS[text]


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval; well behaved at proportions near 0 or 1."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa over paired categorical labels."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in set(left) | set(right)) / (n * n)
    if expected >= 1:
        return 1.0
    return (observed - expected) / (1 - expected)


def weighted_precision(
    per_stratum: dict[str, tuple[int, int]],
    population: dict[str, int],
    strata: tuple[str, ...],
) -> tuple[float, float, float] | None:
    """Population-weighted precision over `strata`, with a 95% interval.

    p = sum W_h p_h, Var = sum W_h^2 p_h(1-p_h)/n_h (1 - n_h/N_h). The variance
    uses (k+2)/(n+4) in place of p_h so that a stratum labelled 100% correct
    does not contribute zero variance and collapse the interval to a point.
    """
    used = [h for h in strata if per_stratum.get(h, (0, 0))[1] > 0]
    if not used:
        return None
    total = sum(population[h] for h in used)
    estimate = 0.0
    variance = 0.0
    for h in used:
        k, n = per_stratum[h]
        weight = population[h] / total
        estimate += weight * k / n
        adjusted = (k + 2) / (n + 4)
        fpc = max(0.0, 1 - n / population[h]) if population[h] else 0.0
        variance += weight * weight * adjusted * (1 - adjusted) / n * fpc
    half = Z95 * math.sqrt(variance)
    return estimate, max(0.0, estimate - half), min(1.0, estimate + half)


# ---------------------------------------------------------------------------
# label handling
# ---------------------------------------------------------------------------


def load_pair(directory: Path, prefix: str) -> tuple[dict, dict] | None:
    a = directory / f"{prefix}_sheet_A.csv"
    b = directory / f"{prefix}_sheet_B.csv"
    if not (a.exists() and b.exists()):
        return None
    return (
        {r["unit_id"]: r for r in read_csv(a)},
        {r["unit_id"]: r for r in read_csv(b)},
    )


def resolve_q1(
    directory: Path, prefix: str, sheets: tuple[dict, dict], sample: dict
) -> tuple[dict[str, str], list[tuple[str, str]], list[str], int]:
    """Final Q1 per unit, the coded pairs, and the units still unresolved.

    Returns (final labels, pairs for kappa, unresolved unit ids, uncoded count).
    Writes or extends the adjudication sheet when disagreements exist.
    """
    sheet_a, sheet_b = sheets
    adjudication_path = directory / f"{prefix}_adjudication.csv"
    adjudicated: dict[str, str] = {}
    existing: list[dict] = []
    if adjudication_path.exists():
        existing = read_csv(adjudication_path)
        for row in existing:
            final = norm_answer(row.get("q1_final"), f"{adjudication_path.name} {row['unit_id']}")
            if final:
                adjudicated[row["unit_id"]] = final

    final: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    disagreements: list[dict] = []
    uncoded = 0
    for unit_id in sample:
        ra, rb = sheet_a.get(unit_id, {}), sheet_b.get(unit_id, {})
        qa = norm_answer(ra.get("q1_invocation"), f"{prefix}_sheet_A {unit_id}")
        qb = norm_answer(rb.get("q1_invocation"), f"{prefix}_sheet_B {unit_id}")
        if qa is None or qb is None:
            uncoded += 1
            continue
        pairs.append((qa, qb))
        if qa == qb:
            final[unit_id] = qa
            continue
        disagreements.append(
            {
                "unit_id": unit_id,
                "permalink": sample[unit_id].get("permalink", ""),
                "q1_A": qa,
                "q1_B": qb,
                "notes_A": ra.get("notes", ""),
                "notes_B": rb.get("notes", ""),
                "q1_final": adjudicated.get(unit_id, ""),
                "rationale": next(
                    (e.get("rationale", "") for e in existing if e["unit_id"] == unit_id), ""
                ),
            }
        )
        if unit_id in adjudicated:
            final[unit_id] = adjudicated[unit_id]

    if disagreements:
        fields = ["unit_id", "permalink", "q1_A", "q1_B", "notes_A", "notes_B",
                  "q1_final", "rationale"]
        with adjudication_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(disagreements)
    unresolved = [d["unit_id"] for d in disagreements if d["unit_id"] not in adjudicated]
    return final, pairs, unresolved, uncoded


# ---------------------------------------------------------------------------


def pct(x: float) -> str:
    return f"{100 * x:.1f}"


def count(n: int) -> str:
    return f"{n:,}".replace(",", "{,}")


def score_precision(directory: Path, macros: dict, report: list[str]) -> bool:
    sheets = load_pair(directory, "precision")
    if sheets is None:
        report.append("precision: no coder sheets yet")
        return True
    sample = {r["unit_id"]: r for r in read_csv(directory / "precision_sample.csv")}
    strata_rows = read_csv(directory / "precision_strata.csv")
    population = {r["stratum"]: int(r["n_units"]) for r in strata_rows}
    names = {r["stratum"]: r["name"] for r in strata_rows}

    final, pairs, unresolved, uncoded = resolve_q1(directory, "precision", sheets, sample)
    agree = sum(a == b for a, b in pairs)
    kappa = cohen_kappa(pairs)
    macros["nCoders"] = "2"
    if pairs:
        macros["agreeOverall"] = pct(agree / len(pairs))
        macros["kappaOverall"] = f"{kappa:.2f}"
    report.append(
        f"precision: {len(pairs)} units coded by both, {uncoded} not yet; "
        f"agreement {agree}/{len(pairs)}"
        + (f", kappa {kappa:.3f}" if kappa is not None else "")
    )

    if uncoded or unresolved:
        report.append(
            f"  {len(unresolved)} disagreement(s) await q1_final in precision_adjudication.csv;"
            " precision not reported until coding and adjudication are complete"
        )
        return False
    macros["nSampled"] = count(len(final))
    macros["nSampledNamed"] = count(
        sum(1 for u in final if sample[u]["stratum"] in OVERALL_STRATA)
    )

    strict: dict[str, tuple[int, int]] = {}
    lenient: dict[str, tuple[int, int]] = {}
    report.append(f"  {'stratum':<16}{'n':>5}{'yes':>5}{'maybe':>6}{'no':>4}"
                  f"{'strict':>9}{'95% Wilson':>17}{'lenient':>9}{'kappa':>7}")
    for h in population:
        ids = [u for u in final if sample[u]["stratum"] == h]
        if not ids:
            continue
        labels = Counter(final[u] for u in ids)
        n, yes, maybe = len(ids), labels["yes"], labels["maybe"]
        strict[h] = (yes, n)
        lenient[h] = (yes + maybe, n)
        lo, hi = wilson(yes, n)
        h_pairs = [
            (norm_answer(sheets[0][u]["q1_invocation"], u), norm_answer(sheets[1][u]["q1_invocation"], u))
            for u in ids
        ]
        hk = cohen_kappa(h_pairs)
        report.append(
            f"  {h} {names[h]:<14}{n:>5}{yes:>5}{maybe:>6}{labels['no']:>4}"
            f"{pct(yes / n):>8}%{pct(lo):>8}-{pct(hi):>6}%{pct((yes + maybe) / n):>8}%"
            f"{hk:>7.2f}"
        )

    overall = weighted_precision(strict, population, OVERALL_STRATA)
    strong = weighted_precision(strict, population, STRONG_STRATA)
    if overall:
        p, lo, hi = overall
        macros["precisionOverall"] = f"{pct(p)}\\%"
        macros["precisionLow"] = f"{pct(lo)}\\%"
        macros["precisionHigh"] = f"{pct(hi)}\\%"
        loose = weighted_precision(lenient, population, OVERALL_STRATA)
        report.append(f"  overall (weighted, strict): {pct(p)}% [{pct(lo)}, {pct(hi)}]"
                      + (f"; lenient {pct(loose[0])}%" if loose else ""))
    if strong:
        macros["precStrong"] = f"{pct(strong[0])}\\%"
        report.append(f"  strong strata (weighted): {pct(strong[0])}% "
                      f"[{pct(strong[1])}, {pct(strong[2])}]")
    if WEAK_STRATUM in strict:
        k, n = strict[WEAK_STRATUM]
        macros["precWeak"] = f"{pct(k / n)}\\%"
    if GENERIC_STRATUM in strict:
        k, n = strict[GENERIC_STRATUM]
        lo, hi = wilson(k, n)
        macros["precGeneric"] = f"{pct(k / n)}\\%"
        macros["precGenericLow"] = f"{pct(lo)}\\%"
        fold = lo >= GENERIC_FOLD_IN
        report.append(
            f"  generic_ai: {pct(k / n)}% [{pct(lo)}, {pct(hi)}] -> "
            + ("FOLD IN: lower bound >= 0.90, count it as agentic"
               if fold else "REPORT SEPARATELY: lower bound < 0.90")
        )

    confirmed = [u for u, label in final.items() if label == "yes"]
    sheet_a, sheet_b = sheets

    def agent_consensus(unit_id: str) -> str | None:
        a = (sheet_a.get(unit_id, {}).get("q2_agent") or "").strip().lower()
        b = (sheet_b.get(unit_id, {}).get("q2_agent") or "").strip().lower()
        return a if a and a == b else None

    agent_units = [u for u in confirmed if agent_consensus(u)]
    if agent_units:
        right = sum(agent_consensus(u) == sample[u]["agent"].lower() for u in agent_units)
        macros["accAgent"] = pct(right / len(agent_units))
        report.append(f"  agent label: {right}/{len(agent_units)} correct "
                      f"(units where both coders named the same agent)")

    return True


def score_recall(directory: Path, macros: dict, report: list[str]) -> bool:
    sheets = load_pair(directory, "recall")
    if sheets is None:
        report.append("recall: no coder sheets yet")
        return True
    sample = {r["unit_id"]: r for r in read_csv(directory / "recall_sample.csv")}
    pool_file = directory / "recall_pool_size.csv"
    if pool_file.exists():
        pool = read_csv(pool_file)[0]
        macros["nRecallRepos"] = count(int(pool["repositories"]))

    final, pairs, unresolved, uncoded = resolve_q1(directory, "recall", sheets, sample)
    agree = sum(a == b for a, b in pairs)
    report.append(f"recall: {len(pairs)} units coded by both, {uncoded} not yet; "
                  f"agreement {agree}/{len(pairs)}")
    if uncoded or unresolved:
        report.append(f"  {len(unresolved)} disagreement(s) await q1_final in "
                      "recall_adjudication.csv; misses not reported yet")
        return False
    missed = sum(label == "yes" for label in final.values())
    n = len(final)
    lo, hi = wilson(missed, n)
    macros["nRecallPool"] = count(n)
    macros["nMissed"] = count(missed)
    macros["pctMissed"] = pct(missed / n) if n else "0.0"
    report.append(f"  missed invocations: {missed}/{n} = {pct(missed / n) if n else 0}% "
                  f"[{pct(lo)}, {pct(hi)}]")
    return True


def write_tex(path: Path, macros: dict) -> None:
    lines = [
        "% Generated by scripts/score_validation.py. Do not edit.",
        "% Macros absent here fall back to numbers-defaults.tex and render as [??].",
    ]
    lines += [f"\\def\\{name}{{{value}}}" for name, value in sorted(macros.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-d", "--dir", default="validation", help="validation directory")
    parser.add_argument("--tex", default=str(DEFAULT_TEX), help="macro file to write")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    macros: dict[str, str] = {}
    report: list[str] = []
    try:
        complete = score_precision(directory, macros, report)
        complete = score_recall(directory, macros, report) and complete
    except LabelError as error:
        print(f"score_validation: {error}", file=sys.stderr)
        return 2

    print("\n".join(report), file=sys.stderr)
    write_tex(Path(args.tex), macros)
    print(f"wrote {len(macros)} macro(s) to {args.tex}", file=sys.stderr)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
