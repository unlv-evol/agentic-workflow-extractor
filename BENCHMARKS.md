# Benchmarks

Measurements against github.com, fresh clone per arm, outputs verified
identical across arms.

## Clone strategy

Blobless clones lose outright. A size-capped filter wins, and **64k is the
default**.

| strategy | vite | uv |
| -------- | ---- | -- |
| `--filter=blob:none` + batched prefetch | 210 s | >300 s (timeout) |
| full clone, no filter | 13 s | 72 s |
| `--filter=blob:limit=...` | 5-7 s | 65-74 s |

### Why blobless loses

On a blobless clone `git cat-file` backfills missing objects one network round
trip at a time -- 54.3 s for the 278 workflow blobs of `cline/cline` against
0.6 s for a batched prefetch, so bulk prefetch is a real 90x win *within* that
strategy. It just does not scale: prefetch time tracks **repository size, not
the number of object ids requested**. `astro` needed 710 blobs and finished in
5 s; `vite` needed 485 and took 113 s; `uv` needed 1,928 and never finished. A
filtered clone sidesteps it: the server builds one packfile by the route it is
optimised for.

### Choosing the cap

| repository | class | cap | total | disk |
| ---------- | ----- | --- | ----- | ---- |
| vite | ordinary | **64k** | **5 s** | **31 MB** |
| vite | | 1m | 7 s | 66 MB |
| langchain | large | **64k** | **18 s** | **108 MB** |
| langchain | | 1m | 33 s | 300 MB |
| godot | large | **64k** | **42 s** | **454 MB** |
| godot | | 1m | 71 s | 1176 MB |
| amplihack | gh-aw adopter | 64k | 41 s | 48 MB |
| amplihack | | **1m** | **16 s** | 196 MB |

Every arm produced identical output with zero missing blobs, so this is a
time/bandwidth trade rather than a correctness one.

**64k wins everywhere except on gh-aw adopters**, whose compiled workflows reach
240 kB and must therefore be backfilled -- and a backfill costs a full fetch
negotiation per repository regardless of how few objects it needs (targeting
only the absent blobs did not close the gap). On large repositories 64k is
about 1.8x faster and 2.7x smaller, because a large repository's bulk is
*sub-1MB source files*, which a 1m cap pulls and a 64k cap does not.

The pilot supplies the weighting: gh-aw adopters are **1.1% of engineered
repositories**. Defaulting to 1m would optimise that 1% and tax the other 99%,
so the default is 64k and the adopters pay roughly 25 s each. Pass `CAP=1m` to
`mine.sh` when mining a list known to be gh-aw-heavy.

An earlier revision of this file recommended 1m. That recommendation was drawn
from `amplihack` and `vite` alone -- a gh-aw adopter and a small repository --
and did not test a large non-adopter, which is where a full run actually spends
its time.

## What is the bottleneck

Extraction, not cloning, once the cap is right. `uv` spends 53 s of its 65 s
CPU-bound in YAML parsing; traversal was never the constraint (`git log` stayed
at 0.1-0.4 s even on the slow repositories). Extraction parallelises cleanly
across repositories where clone bandwidth does not, so size a large run on
cores rather than on network.

## Pilot: 480 repositories

Star-banded GitHub sample, 10 workers, `CAP=1m` (before the default changed):

* 479 mined, **0 failures**, 53,853 blobs requested and 53,853 read
* 348 (73%) pass the >= 300 commit threshold
* 30 (8.6%) are agentic; 4 (1.1%) use gh-aw
* wall time was dominated by a handful of giants, which is what prompted the
  cap re-measurement above

`timeout 900` did **not** bound `git clone` on the largest repositories:
`git` forks `index-pack` children that survive the TERM, so `linux`,
`tensorflow` and `TypeScript` pinned three workers for over an hour.
`mine.sh` now uses `timeout --foreground -k 30`.

## Pathological repositories

`github/gh-aw` -- the framework's own development repository -- holds **235,511
workflow blobs across 242,003 revisions**, roughly a thousand times any real
adopter. It exposed three bugs (a `cat-file` stdin deadlock, unbounded memory,
and filter-blind prefetch) and is excluded from the dataset by
`[exclude].repositories` in the catalogue, recorded with `status = excluded` so
the denominator stays honest.

## Open

**Repository-level concurrency** is unmeasured: how many concurrent clones
github.com tolerates before throttling.

Reproduce with `scripts/benchmark_prefetch.py`.

## Dry run: 500 repositories from the SEART frame

Seeded random 500 from `data/seart/repos-2026-10-03.txt` (seed 20261003),
16 workers, `CAP=64k`, frozen `config/catalogue.toml`, one 8-core Mac, 2026-10-03:

* 500 mined, **0 failures**, 0 missing blobs, every repository >= 300 commits
* 499 finished in about 8 minutes (about 1 s of wall time per repository); the
  last, `clickhouse/clickhouse` (296,841 commits), took 13.6 minutes on its own
* 1,932 s of user CPU in total, about 3.9 CPU-seconds per repository
* 13 (2.6%) agentic, all through named agents (Claude 6, Copilot 5, Codex 3);
  no `generic_ai` match and no gh-aw adopter

ClickHouse spent its time in the backfill fetch for its 64k-plus workflow
files, and not in the network: after the fetch, git checks connectivity with a
`rev-list --objects ... --not --all` that walks the whole repository (4 min 47 s
of CPU and counting when inspected). Repositories like it will meet the
900-second timeout in a full run; `mine.sh` now records them as
`extract-failed` in `repositories.csv` and prints the `CAP=1m` retry command,
since a 1m cap leaves no blob to backfill.

Projection for the full 172,525: about 2 days at 16 workers on this machine,
CPU-bound at roughly 23 hours of 8-core time, plus a retry pass for timeouts.
