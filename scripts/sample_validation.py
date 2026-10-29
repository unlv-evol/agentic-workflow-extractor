#!/usr/bin/env python3
"""Draw the validation samples described in validation/PLAN.md.

Two samples, one subcommand each:

    # precision: stratified sample of distinct invocations from steps.csv
    python3 scripts/sample_validation.py precision out/steps.csv \
        --revisions out/revisions.csv -o validation/

    # recall: keyword pool of non-agentic workflows from a --keep-all run
    python3 scripts/sample_validation.py recall recall-out/revisions.csv \
        --content-dir recall-out/content -o validation/

Each writes the sample with the detector's columns (kept away from coders),
plus one blank sheet per coder that carries only what a coder needs to look at.
Sheets are identical, and each coder fills in their own, so neither sees the
other's answers or what the detector decided.

Everything is seeded, so the same inputs and seed always draw the same sample.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Mutually exclusive strata, assigned in this order (PLAN.md, "Strata").
STRATA = ("X", "P", "G", "U", "R", "F", "W")
STRATUM_NAMES = {
    "X": "generic-ai",
    "P": "parse-failed",
    "G": "gh-aw",
    "U": "uses",
    "R": "run",
    "F": "path",
    "W": "weak-only",
}
# The strata behind the overall precision figure: named agents, strong or
# unparsed evidence. `--total` sizes these alone and only they take surplus.
# Weak-only (W) and the generic group (X) are reported on their own, each at
# the floor, because each answers a separate question (PLAN.md, C2 and C7).
EXTRA_STRATA = ("P", "G", "U", "R", "F")
GENERIC_AGENT = "generic_ai"

CODERS = ("A", "B")
# Q1 invocation (yes/no/maybe) and Q2 agent, as in the paper (validation/PLAN.md).
PRECISION_QUESTIONS = ("q1_invocation", "q2_agent", "notes")
RECALL_QUESTIONS = ("q1_invocation", "q2_agent", "notes")

# Over-inclusive on purpose: the pool is meant to catch what the catalogue
# missed, so it has to reach past every pattern the catalogue already holds.
DEFAULT_KEYWORDS = (
    r"claude|anthropic|codex|openai|copilot|gemini|cursor|aider|devin|"
    r"coderabbit|llm|gpt|ai[-_ ]review|agent"
)


def read_csv(path: str | Path) -> list[dict]:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def permalink(repository: str, commit: str, file_path: str) -> str:
    return f"https://github.com/{repository}/blob/{commit}/{file_path}"


def is_true(value: str | None) -> bool:
    return str(value).strip().lower() == "true"


# ---------------------------------------------------------------------------
# precision
# ---------------------------------------------------------------------------


def stratum_of(row: dict) -> str | None:
    if row.get("agent") == GENERIC_AGENT:
        return "X"
    if not is_true(row.get("parse_ok")):
        return "P"
    kind = row.get("evidence_kind")
    return {
        "ghaw": "G",
        "uses": "U",
        "run": "R",
        "path": "F",
        "job": "W",
        "url": "W",
        "env": "W",
    }.get(kind)


def unit_key(row: dict) -> tuple[str, str, str, str]:
    """A distinct invocation: the same step in the same content is one unit."""
    return (
        row.get("content_sha256") or "",
        row.get("agent") or "",
        row.get("job_id") or "",
        row.get("step_index") or "",
    )


def build_units(steps: list[dict], rng: random.Random) -> list[dict]:
    """Deduplicate step rows into units, keeping a random representative.

    The representative decides which repository and commit the coder is sent
    to. Picking it at random, rather than the first row seen, avoids always
    sending coders to the newest revision of every file.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in steps:
        if stratum_of(row) is None:
            continue
        grouped[unit_key(row)].append(row)

    units = []
    for key in sorted(grouped):
        members = grouped[key]
        chosen = dict(rng.choice(members))
        chosen["stratum"] = stratum_of(chosen)
        chosen["n_rows"] = len(members)
        chosen["n_repositories"] = len({m["repository"] for m in members})
        units.append(chosen)
    return units


def available(units: list[dict], cap: int) -> int:
    """How many units a stratum can supply under the per-repository cap."""
    per_repo = Counter(u["repository"] for u in units)
    return sum(min(cap, n) for n in per_repo.values())


def allocate(
    population: dict[str, int],
    capacity: dict[str, int],
    total: int,
    floor: int,
) -> dict[str, int]:
    """Floor per stratum, surplus to the named-agent strata in proportion to size.

    `total` is the size of the named-agent strata (EXTRA_STRATA) together; the
    weak-only and generic strata get the floor on top of it. A stratum smaller
    than the floor is taken whole, and whatever it could not absorb is
    redistributed, repeatedly, until the total is met or no stratum has room
    left.
    """
    alloc = {s: min(floor, capacity.get(s, 0)) for s in population}
    remaining = total - sum(alloc[s] for s in EXTRA_STRATA if s in alloc)
    while remaining > 0:
        open_strata = [
            s
            for s in EXTRA_STRATA
            if s in population and alloc[s] < capacity.get(s, 0)
        ]
        if not open_strata:
            break
        weight = sum(population[s] for s in open_strata)
        shares = {s: remaining * population[s] / weight for s in open_strata}
        given = 0
        for s in open_strata:
            room = capacity[s] - alloc[s]
            add = min(room, int(shares[s]))
            alloc[s] += add
            given += add
        if given == 0:
            # Shares all rounded down to zero: hand out one at a time, largest
            # stratum first, so the loop always makes progress.
            for s in sorted(open_strata, key=lambda s: -population[s]):
                if remaining - given <= 0:
                    break
                if alloc[s] < capacity[s]:
                    alloc[s] += 1
                    given += 1
        remaining -= given
    return alloc


def allocate_proportional(
    population: dict[str, int], capacity: dict[str, int], total: int, minimum: int
) -> dict[str, int]:
    """`total` units in proportion to stratum size, at least `minimum` each.

    Strata whose proportional share falls below the minimum are fixed at it,
    and the rest is re-shared among the others until no share falls below it.
    Largest remainders settle the rounding, so the result sums to `total`
    (unless capacities make that impossible).
    """
    fixed: dict[str, int] = {}
    while True:
        free = {s: n for s, n in population.items() if s not in fixed}
        left = total - sum(fixed.values())
        weight = sum(free.values())
        shares = {s: left * n / weight for s, n in free.items()} if weight else {}
        small = {s for s, v in shares.items() if v < minimum}
        if not small:
            break
        for s in small:
            fixed[s] = min(minimum, capacity.get(s, 0))
    alloc = dict(fixed)
    floors = {s: int(v) for s, v in shares.items()}
    alloc.update(floors)
    for s in sorted(shares, key=lambda s: shares[s] - floors[s], reverse=True)[: left - sum(floors.values())]:
        alloc[s] += 1
    return {s: min(n, capacity.get(s, n)) for s, n in alloc.items()}


def subsample_precision(
    sample: list[dict], strata: list[dict], total: int, minimum: int, seed: int
) -> tuple[list[dict], list[dict]]:
    """Shrink the named-agent strata of an existing draw; keep W and X whole.

    Each stratum keeps a seeded random subset of the units already drawn for
    it, so the result is still a random sample of that stratum (and still
    honours the per-repository cap of the original draw). The global, shuffled
    order and the unit ids are preserved; only rows are removed.
    """
    rng = random.Random(seed)
    population = {r["stratum"]: int(r["n_units"]) for r in strata if r["stratum"] in EXTRA_STRATA}
    drawn = {s: [u for u in sample if u["stratum"] == s] for s in population}
    alloc = allocate_proportional(population, {s: len(u) for s, u in drawn.items()}, total, minimum)
    keep: set[str] = set()
    for s, units in drawn.items():
        ids = sorted(u["unit_id"] for u in units)
        keep.update(rng.sample(ids, alloc[s]))
    kept = [u for u in sample if u["stratum"] not in population or u["unit_id"] in keep]
    counts = Counter(u["stratum"] for u in kept)
    new_strata = [dict(r, n_sampled=counts.get(r["stratum"], 0)) for r in strata]
    return kept, new_strata


def systematic_draw(units: list[dict], n: int, cap: int, rng: random.Random) -> list[dict]:
    """Systematic sample over units ordered by agent, honouring a repository cap.

    Ordering by agent before stepping through the list gives every agent in the
    stratum a share proportional to its size (implicit stratification). When a
    pick would exceed its repository's cap, the next eligible unit is taken.
    """
    if n <= 0 or not units:
        return []
    ordered = list(units)
    rng.shuffle(ordered)
    ordered.sort(key=lambda u: u.get("agent") or "")
    size = len(ordered)
    step = size / n
    start = rng.random() * step

    taken: set[int] = set()
    per_repo: Counter = Counter()
    picks: list[dict] = []
    for i in range(n):
        index = int(start + i * step) % size
        for offset in range(size):
            j = (index + offset) % size
            unit = ordered[j]
            if j in taken or per_repo[unit["repository"]] >= cap:
                continue
            taken.add(j)
            per_repo[unit["repository"]] += 1
            picks.append(unit)
            break
    return picks


def sample_precision(
    steps: list[dict],
    revisions: list[dict] | None,
    total: int,
    floor: int,
    cap: int,
    seed: int,
) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    units = build_units(steps, rng)

    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for unit in units:
        by_stratum[unit["stratum"]].append(unit)
    rows_by_stratum = Counter(s for s in map(stratum_of, steps) if s)

    population = {s: len(by_stratum[s]) for s in STRATA if by_stratum[s]}
    capacity = {s: available(by_stratum[s], cap) for s in population}
    alloc = allocate(population, capacity, total, floor)

    gated: dict[tuple, str] = {}
    if revisions:
        for r in revisions:
            gated[(r["repository"], r["commit_hash"], r["file_path"])] = r.get(
                "actor_gated", ""
            )

    sample: list[dict] = []
    for s in STRATA:
        if s not in population:
            continue
        sample.extend(systematic_draw(by_stratum[s], alloc[s], cap, rng))

    # Coders see units in a random order, not grouped by stratum, so that what
    # they labelled last does not prime the next one.
    rng.shuffle(sample)
    for number, unit in enumerate(sample, start=1):
        unit["unit_id"] = f"V{number:04d}"
        unit["permalink"] = permalink(
            unit["repository"], unit["commit_hash"], unit["file_path"]
        )
        unit["actor_gated"] = gated.get(
            (unit["repository"], unit["commit_hash"], unit["file_path"]), ""
        )

    total_units = sum(population.values())
    strata_rows = [
        {
            "stratum": s,
            "name": STRATUM_NAMES[s],
            "n_rows": rows_by_stratum[s],
            "n_units": population[s],
            "n_available": capacity[s],
            "n_sampled": sum(1 for u in sample if u["stratum"] == s),
            "weight": f"{population[s] / total_units:.6f}",
        }
        for s in STRATA
        if s in population
    ]
    return sample, strata_rows


def sample_calibration(
    steps: list[dict],
    exclude_repositories: set[str],
    n: int,
    seed: int,
) -> list[dict]:
    """Units for the joint calibration round (PLAN.md, "Procedure", step 1).

    Drawn from outside the precision sample: no repository already in it, so
    that no file the coders discussed together comes back during independent
    coding. Spread evenly over every stratum, weak-only and generic included,
    since calibration is about the definitions rather than any one estimate,
    and at most one unit per repository.
    """
    rng = random.Random(seed)
    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for unit in build_units(steps, rng):
        if unit["repository"] not in exclude_repositories:
            by_stratum[unit["stratum"]].append(unit)
    for units in by_stratum.values():
        rng.shuffle(units)

    sample: list[dict] = []
    used: set[str] = set()
    cursors = {s: 0 for s in by_stratum}
    while len(sample) < n:
        progressed = False
        for s in STRATA:
            if len(sample) >= n:
                break
            units = by_stratum.get(s, [])
            while cursors.get(s, 0) < len(units):
                unit = units[cursors[s]]
                cursors[s] += 1
                if unit["repository"] not in used:
                    used.add(unit["repository"])
                    sample.append(unit)
                    progressed = True
                    break
        if not progressed:
            break

    rng.shuffle(sample)
    for number, unit in enumerate(sample, start=1):
        unit["unit_id"] = f"C{number:03d}"
        unit["permalink"] = permalink(
            unit["repository"], unit["commit_hash"], unit["file_path"]
        )
    return sample


PRECISION_SAMPLE_FIELDS = [
    "unit_id", "stratum", "repository", "commit_hash", "commit_date", "file_path",
    "permalink", "scope", "job_id", "step_index", "step_name", "agent",
    "evidence_kind", "evidence_kinds", "pattern", "action", "action_version",
    "triggers", "tasks", "actor_gated", "parse_ok", "content_sha256",
    "n_rows", "n_repositories",
]
# What a coder needs to find the invocation, and nothing the detector decided.
PRECISION_SHEET_FIELDS = ["unit_id", "permalink", "scope", "job_id", "step_index", "step_name"]
STRATA_FIELDS = ["stratum", "name", "n_rows", "n_units", "n_available", "n_sampled", "weight"]


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


def sample_recall(
    revisions: list[dict],
    content_dir: Path,
    n: int,
    seed: int,
    keywords: str,
) -> tuple[list[dict], int]:
    """Latest revision of every never-agentic workflow file, keyword-filtered.

    Files agentic at any point are excluded: they are already in the dataset,
    so they cannot be misses. Deleted revisions have no content to read.
    """
    rng = random.Random(seed)
    pattern = re.compile(keywords, re.IGNORECASE)

    agentic_uids = {
        (r["repository"], r["uid"]) for r in revisions if is_true(r.get("agentic"))
    }
    latest: dict[tuple, dict] = {}
    for r in revisions:
        key = (r["repository"], r["uid"])
        if key in agentic_uids or not r.get("content_sha256"):
            continue
        if key not in latest or r["commit_date"] > latest[key]["commit_date"]:
            latest[key] = r

    pool: list[dict] = []
    for key in sorted(latest):
        r = latest[key]
        path = content_dir / f"{r['content_sha256']}.yml"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = sorted({m.group(0).lower() for m in pattern.finditer(text)})
        if hits:
            pool.append({**r, "keyword_hits": "|".join(hits)})

    picks = rng.sample(pool, min(n, len(pool)))
    for number, unit in enumerate(picks, start=1):
        unit["unit_id"] = f"R{number:04d}"
        unit["permalink"] = permalink(unit["repository"], unit["commit_hash"], unit["file_path"])
    return picks, len(pool)


RECALL_SAMPLE_FIELDS = [
    "unit_id", "repository", "uid", "commit_hash", "commit_date", "file_path",
    "permalink", "content_sha256", "keyword_hits", "agents", "parse_ok",
]
RECALL_SHEET_FIELDS = ["unit_id", "permalink"]


# ---------------------------------------------------------------------------


def write_sheets(out: Path, prefix: str, sample: list[dict], fields: list[str], questions) -> None:
    for coder in CODERS:
        rows = [{f: unit.get(f, "") for f in fields} for unit in sample]
        write_csv(out / f"{prefix}_sheet_{coder}.csv", rows, fields + list(questions))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("precision", help="stratified sample of distinct invocations")
    p.add_argument("steps", help="steps.csv (or .csv.gz) from the full run")
    p.add_argument("--revisions", help="revisions.csv, to attach actor_gated for Q4")
    p.add_argument("--total", type=int, default=664,
                   help="sample size of the named-agent strata, which carry the overall "
                        "precision figure; weak-only and generic get --floor each on top")
    p.add_argument("--floor", type=int, default=96, help="minimum units per stratum")
    p.add_argument("--per-repo-cap", type=int, default=2)

    r = sub.add_parser("recall", help="keyword pool of non-agentic workflows")
    r.add_argument("revisions", help="revisions.csv from a --keep-all run")
    r.add_argument("--content-dir", required=True, help="content store of that run")
    r.add_argument("-n", type=int, default=200, help="pool sample size")
    r.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="regex for the pool")

    c = sub.add_parser("calibration", help="joint calibration units from outside the sample")
    c.add_argument("steps", help="steps.csv (or .csv.gz) from the full run")
    c.add_argument("--exclude", required=True,
                   help="precision_sample.csv; its repositories are left out")
    c.add_argument("-n", type=int, default=30, help="number of calibration units")

    q = sub.add_parser("subsample",
                       help="shrink the named-agent strata of the drawn precision sample in place")
    q.add_argument("--total", type=int, default=384,
                   help="named-agent units: 384 is a +-5%% margin at 95%% confidence")
    q.add_argument("--minimum", type=int, default=30, help="minimum units per named stratum")
    q.add_argument("--seed", type=int, default=20261029)
    q.add_argument("--archive", required=True,
                   help="directory to copy the current sample and sheets to first")

    for sp in (p, r, c, q):
        sp.add_argument("-o", "--out-dir", default="validation")
    for sp in (p, r):
        sp.add_argument("--seed", type=int, default=20261017)
    # A seed of its own, so the calibration draw never mirrors the sample's.
    c.add_argument("--seed", type=int, default=20261016)

    args = parser.parse_args(argv)
    out = Path(args.out_dir)

    if args.mode == "precision":
        steps = read_csv(args.steps)
        revisions = read_csv(args.revisions) if args.revisions else None
        sample, strata = sample_precision(
            steps, revisions, args.total, args.floor, args.per_repo_cap, args.seed
        )
        write_csv(out / "precision_sample.csv", sample, PRECISION_SAMPLE_FIELDS)
        write_csv(out / "precision_strata.csv", strata, STRATA_FIELDS)
        write_sheets(out, "precision", sample, PRECISION_SHEET_FIELDS, PRECISION_QUESTIONS)
        print(f"{'stratum':<14}{'rows':>9}{'units':>8}{'avail':>8}{'sampled':>9}", file=sys.stderr)
        for s in strata:
            print(
                f"{s['stratum']} {s['name']:<12}{s['n_rows']:>9}{s['n_units']:>8}"
                f"{s['n_available']:>8}{s['n_sampled']:>9}",
                file=sys.stderr,
            )
        print(f"total sampled: {len(sample)} (seed {args.seed})", file=sys.stderr)
        if not revisions:
            print("note: no --revisions given, so actor_gated is blank and Q4 cannot be scored",
                  file=sys.stderr)
    elif args.mode == "subsample":
        sample = read_csv(out / "precision_sample.csv")
        strata = read_csv(out / "precision_strata.csv")
        for coder in CODERS:
            sheet = read_csv(out / f"precision_sheet_{coder}.csv")
            if any((r.get("q1_invocation") or "").strip() for r in sheet):
                raise SystemExit(f"precision_sheet_{coder}.csv holds answers; refusing to subsample")
        archive = Path(args.archive)
        archive.mkdir(parents=True, exist_ok=True)
        for name in ("precision_sample.csv", "precision_strata.csv",
                     "precision_sheet_A.csv", "precision_sheet_B.csv"):
            shutil.copyfile(out / name, archive / name)
        kept, new_strata = subsample_precision(sample, strata, args.total, args.minimum, args.seed)
        write_csv(out / "precision_sample.csv", kept, PRECISION_SAMPLE_FIELDS)
        write_csv(out / "precision_strata.csv", new_strata, STRATA_FIELDS)
        write_sheets(out, "precision", kept, PRECISION_SHEET_FIELDS, PRECISION_QUESTIONS)
        for row in new_strata:
            print(f"  {row['stratum']} {row['name']:<14}{row['n_sampled']:>5}", file=sys.stderr)
        print(f"precision sample: {len(sample)} -> {len(kept)} units (seed {args.seed}); "
              f"previous version in {archive}", file=sys.stderr)
    elif args.mode == "calibration":
        exclude = {u["repository"] for u in read_csv(args.exclude)}
        sample = sample_calibration(read_csv(args.steps), exclude, args.n, args.seed)
        write_csv(out / "calibration_sample.csv", sample, PRECISION_SAMPLE_FIELDS)
        # One sheet: calibration is coded jointly, not independently.
        write_csv(
            out / "calibration_sheet.csv",
            [{f: unit.get(f, "") for f in PRECISION_SHEET_FIELDS} for unit in sample],
            PRECISION_SHEET_FIELDS + list(PRECISION_QUESTIONS),
        )
        counts = Counter(u["stratum"] for u in sample)
        print("  ".join(f"{s}={counts[s]}" for s in STRATA if counts[s]), file=sys.stderr)
        print(f"calibration units: {len(sample)} from {len(sample)} repositories outside "
              f"the {len(exclude)} in the sample (seed {args.seed})", file=sys.stderr)
    else:
        revisions = read_csv(args.revisions)
        sample, pool = sample_recall(
            revisions, Path(args.content_dir), args.n, args.seed, args.keywords
        )
        write_csv(out / "recall_sample.csv", sample, RECALL_SAMPLE_FIELDS)
        write_csv(out / "recall_pool_size.csv", [{"pool": pool, "sampled": len(sample),
                  "repositories": len({r['repository'] for r in revisions})}],
                  ["pool", "sampled", "repositories"])
        write_sheets(out, "recall", sample, RECALL_SHEET_FIELDS, RECALL_QUESTIONS)
        print(f"keyword pool: {pool} files, sampled {len(sample)} (seed {args.seed})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
