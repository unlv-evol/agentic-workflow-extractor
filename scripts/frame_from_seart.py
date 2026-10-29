#!/usr/bin/env python3
"""Turn a SEART export into the sampling frame and the list mine.sh reads.

    python3 scripts/frame_from_seart.py data/seart/results-2026-10-03.csv.gz \\
        --query-date 2026-10-03

The SEART query (non-forks with at least 300 commits) is applied by SEART; the
activity criterion is applied here, from the export's own `lastCommit` column,
so that it is exactly reproducible from the archived export:

* active: last commit within the year before the query date. Commit dates in
  the future of the query date are clock errors and fail the criterion, as
  they would in SEART's own date filter.

Each repository is also flagged `ghagentfiles_comparable` when it was created at
least a year before the query, which is the remaining GHAgentFiles criterion,
so that their population can be reported as a subset of ours.

The repository list is shuffled with a fixed seed: any prefix of a run is then
a random sample of the frame, so a run cut short still yields unbiased rates.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import random
import sys
from datetime import date, timedelta
from pathlib import Path

FIELDS = ["name", "createdAt", "lastCommit", "pushedAt", "commits", "stargazers",
          "mainLanguage", "isArchived", "ghagentfiles_comparable"]


def one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("export", help="SEART CSV export (.csv or .csv.gz)")
    parser.add_argument("--query-date", required=True, help="date SEART was queried (YYYY-MM-DD)")
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("-o", "--out-dir", default=None, help="default: the export's directory")
    args = parser.parse_args(argv)

    export = Path(args.export)
    queried = date.fromisoformat(args.query_date)
    since = one_year_before(queried).isoformat()
    until = queried.isoformat()
    out = Path(args.out_dir) if args.out_dir else export.parent
    tag = queried.isoformat()

    csv.field_size_limit(sys.maxsize)
    opener = gzip.open if export.suffix == ".gz" else open
    with opener(export, "rt", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    kept, dropped = [], {"inactive": 0, "future-dated": 0, "no last commit": 0}
    for row in rows:
        last = (row.get("lastCommit") or "")[:10]
        if not last:
            dropped["no last commit"] += 1
        elif last > until:
            dropped["future-dated"] += 1
        elif last < since:
            dropped["inactive"] += 1
        else:
            row["ghagentfiles_comparable"] = str(row["createdAt"][:10] <= since).lower()
            kept.append(row)

    kept.sort(key=lambda r: r["name"].lower())
    with (out / f"frame-{tag}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)

    names = [r["name"] for r in kept]
    random.Random(args.seed).shuffle(names)
    comparable = sum(r["ghagentfiles_comparable"] == "true" for r in kept)
    with (out / f"repos-{tag}.txt").open("w", encoding="utf-8") as handle:
        handle.write(
            f"# awe sampling frame: {len(names)} repositories from SEART GitHub Search\n"
            f"# Export: {export.name} (queried {tag}: non-forks, >= 300 commits)\n"
            f"# Kept here: last commit in [{since}, {until}]; built by scripts/frame_from_seart.py\n"
            f"# {comparable} also created on or before {since} (GHAgentFiles-comparable)\n"
            f"# Shuffled with seed {args.seed}: any prefix of a run is a random sample.\n"
        )
        handle.write("\n".join(names) + "\n")

    print(f"export: {len(rows)} repositories", file=sys.stderr)
    for reason, n in dropped.items():
        print(f"  dropped, {reason}: {n}", file=sys.stderr)
    print(f"frame: {len(kept)} repositories, {comparable} GHAgentFiles-comparable, "
          f"{len(kept) - comparable} created after {since}", file=sys.stderr)
    print(f"wrote {out / f'frame-{tag}.csv'} and {out / f'repos-{tag}.txt'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
