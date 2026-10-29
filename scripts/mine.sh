#!/usr/bin/env bash
# Mine a list of "owner/name" repositories: clone, extract, delete, repeat.
#
#   ./scripts/mine.sh repos.txt out/ 8
#
# Unlike clone_batch.sh, which materialises every clone before extraction, this
# holds only `jobs` clones at a time. That is the difference between ~200 MB of
# working space and, at the scale this tool targets, several terabytes.
#
# Each worker writes its own CSV shard under out/parts/ so that parallel
# appends cannot interleave mid-row; `merge` combines them. Reruns skip
# repositories already recorded, so an interrupted run resumes where it
# stopped.
#
# Environment:
#   CAP=64k         blob size cap for the partial clone (see BENCHMARKS.md)
#   CONFIG=...      detector catalogue (default: config/catalogue.toml, the frozen
#                   merge of default.toml and marketplace.toml)
#   TIMEOUT=900     per-repository seconds before giving up
#   WORK=...        parent directory for clones (default: $TMPDIR)
#   CONTENT=...     content store, one file per distinct workflow revision of
#                   an emitted history (default: <out-dir>/content)
#   AWE_ARGS=...    extra arguments for awe, e.g. AWE_ARGS=--keep-all for the
#                   recall subset of validation/PLAN.md
#
# Each clone is deleted as soon as its repository has been extracted. Clones
# live in a private directory created under WORK, which is removed on exit --
# including Ctrl-C, kill and errors -- so an interrupted run leaves no clones
# behind. WORK itself is never deleted.
set -uo pipefail

LIST="${1:?usage: mine.sh <repo-list> <out-dir> [jobs]}"
OUT="${2:?usage: mine.sh <repo-list> <out-dir> [jobs]}"
JOBS="${3:-8}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAP="${CAP:-64k}"
CONFIG="${CONFIG:-$ROOT/config/catalogue.toml}"
TIMEOUT="${TIMEOUT:-900}"
CONTENT="${CONTENT:-$OUT/content}"
AWE_ARGS="${AWE_ARGS:-}"

# GNU timeout; macOS ships without it (`brew install coreutils` -> gtimeout).
# Without one, every clone "fails" instantly and is logged as clone-failed.
if command -v timeout > /dev/null; then
    TIMEOUT_BIN=timeout
elif command -v gtimeout > /dev/null; then
    TIMEOUT_BIN=gtimeout
else
    echo "mine.sh: needs GNU timeout (on macOS: brew install coreutils)" >&2
    exit 2
fi

# A private directory under WORK, so cleanup can never touch anything else.
WORK_PARENT="${WORK:-${TMPDIR:-/tmp}}"
mkdir -p "$WORK_PARENT"
WORK="$(mktemp -d "${WORK_PARENT%/}/awe-mine.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# On interrupt, stop the workers before deleting their clones; otherwise a
# worker mid-clone would recreate its directory after cleanup. TERM first,
# then KILL for the index-pack children that are known to survive TERM.
on_signal() {
    trap '' INT TERM
    kill -TERM %1 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -z "$(jobs -r)" ] && break
        sleep 1
    done
    kill -KILL %1 2>/dev/null
    wait 2>/dev/null
    exit "$1"
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

export ROOT CAP CONFIG TIMEOUT TIMEOUT_BIN WORK OUT CONTENT AWE_ARGS
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
# Never wait on a credential prompt: a deleted or private repository answers a
# clone with an authentication challenge, and without a terminal git would sit
# on it until the timeout. Fail at once instead.
export GIT_TERMINAL_PROMPT=0

mkdir -p "$OUT/parts" "$CONTENT"

# Provenance for the replication package: what produced this run. Appended, so
# a resumed or retried run leaves one block per invocation.
{
    echo "== $(date -u '+%Y-%m-%dT%H:%M:%SZ') on $(hostname)"
    echo "list:     $LIST ($(grep -cve '^\s*$' -e '^\s*#' "$LIST") repositories)"
    echo "jobs:     $JOBS   cap: $CAP   timeout: ${TIMEOUT}s   awe args: ${AWE_ARGS:-none}"
    echo "config:   $CONFIG"
    for f in "$(dirname "$CONFIG")"/*.toml "$LIST"; do
        printf '  sha256  %s  %s\n' \
            "$( (sha256sum "$f" 2>/dev/null || shasum -a 256 "$f") | cut -d' ' -f1)" "$(basename "$f")"
    done
    echo "git:      $(git --version)"
    echo "python:   $(python3 -V 2>&1)"
} >> "$OUT/RUN.txt"

# Repositories already recorded in a previous run, so this one can resume.
DONE="$OUT/.done"
: > "$DONE"
# `find -exec +`, not a glob: there is one shard per repository, and ~10^5 of
# them overflow ARG_MAX, which would leave .done empty and re-mine everything.
find "$OUT/parts" -name repositories.csv -exec tail -q -n +2 {} + 2>/dev/null \
    | cut -d, -f1 | sort -u > "$DONE"
export DONE

# Repositories the catalogue records but never mines ([exclude] in the config).
# Checked before cloning: `github/gh-aw` alone holds 235k workflow blobs, and
# cloning it only for awe to mark it excluded would pin a worker for nothing.
EXCLUDED="$OUT/.excluded"
python3 -c 'import sys; from awe.config import load_config; print("\n".join(load_config(sys.argv[1]).exclude))' \
    "$CONFIG" > "$EXCLUDED" || { echo "mine.sh: cannot read $CONFIG" >&2; exit 2; }
export EXCLUDED

mine_one() {
    local slug="$1"
    local safe="${slug//\//__}"
    local part="$OUT/parts/$$"
    local clone="$WORK/$safe"

    grep -qxF "$slug" "$DONE" 2>/dev/null && return 0

    mkdir -p "$part"
    rm -rf "$clone"

    # Recorded with status `excluded`, so the denominator still counts it.
    if grep -qixF "$slug" "$EXCLUDED" 2>/dev/null; then
        python3 -m awe.cli --repository "$slug" "$clone" \
            -o "$part" -c "$CONFIG" --quiet 2>>"$OUT/errors.log"
        return 0
    fi

    # --foreground so the signal reaches git even under xargs, and -k so a
    # clone that ignores TERM is killed rather than pinning a worker forever.
    # Without both, `git clone` on a very large repository outlives its
    # timeout: it forks index-pack children that survive the TERM, and a
    # handful of such repositories permanently reduce effective parallelism.
    if ! "$TIMEOUT_BIN" --foreground -k 30 "$TIMEOUT" \
            git clone --quiet --filter=blob:limit="$CAP" \
            --no-checkout --single-branch \
            "https://github.com/${slug}.git" "$clone" 2>/dev/null; then
        echo "$slug	clone-failed" >> "$OUT/failures.tsv"
        rm -rf "$clone"
        return 0
    fi

    if ! "$TIMEOUT_BIN" --foreground -k 30 "$TIMEOUT" python3 -m awe.cli --repository "$slug" "$clone" \
            -o "$part" -c "$CONFIG" --content-dir "$CONTENT" $AWE_ARGS \
            --quiet 2>>"$OUT/errors.log"; then
        echo "$slug	extract-failed" >> "$OUT/failures.tsv"
    fi

    # The clone is the expensive thing to keep, and nothing downstream needs it.
    # Make it writable first: on the 500-repository trial, deleting a clone that
    # had been through a backfill fetch failed with "Permission denied", and a
    # clone left behind here stays on disk until the whole run ends.
    chmod -R u+w "$clone" 2>/dev/null
    rm -rf "$clone"
}
export -f mine_one

TOTAL=$(grep -cve '^\s*$' -e '^\s*#' "$LIST")
echo "mining $TOTAL repositories, ${JOBS} at a time, cap=$CAP" >&2
START=$(date +%s)

# Run the workers as a background job in their own process group (set -m) and
# wait on it: bash defers traps until a foreground command returns, so a
# foreground pipeline would keep cloning until every worker drained.
set -m
grep -v '^\s*#' "$LIST" | grep -v '^\s*$' \
    | xargs -P "$JOBS" -I{} bash -c 'mine_one "$@"' _ {} &
wait $!
set +m

# Merge shards: keep the first header, append every body.
for name in repositories revisions steps ghaw_manifests; do
    first=1
    : > "$OUT/$name.csv"
    for shard in "$OUT"/parts/*/"$name.csv"; do
        [ -f "$shard" ] || continue
        if [ $first -eq 1 ]; then head -1 "$shard" > "$OUT/$name.csv"; first=0; fi
        tail -n +2 "$shard" >> "$OUT/$name.csv"
    done
done

# A repository whose clone or extraction failed -- most often killed by the
# timeout -- never reaches repositories.csv, and one that silently vanishes is
# indistinguishable from one that was never sampled. Give each such repository
# a row carrying its failure as status, unless a later attempt succeeded.
if [ -s "$OUT/failures.tsv" ]; then
    python3 - "$OUT/repositories.csv" "$OUT/failures.tsv" <<'PY'
import csv, sys
from awe.cli import SUMMARY_FIELDS
summary, failures = sys.argv[1], sys.argv[2]
try:
    with open(summary, newline="") as handle:
        recorded = {row["repository"] for row in csv.DictReader(handle)}
    header = recorded is not None and open(summary).read(1) != ""
except FileNotFoundError:
    recorded, header = set(), False
last = {}
with open(failures) as handle:
    for line in handle:
        slug, _, status = line.rstrip("\n").partition("\t")
        if slug:
            last[slug] = status or "failed"
with open(summary, "a", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
    if not header:
        writer.writeheader()
    for slug, status in last.items():
        if slug not in recorded:
            writer.writerow({field: "" for field in SUMMARY_FIELDS}
                            | {"repository": slug, "status": status})
PY
fi

ELAPSED=$(( $(date +%s) - START ))
DONE_N=$(( $(wc -l < "$OUT/repositories.csv") ))
DONE_N=$(( DONE_N > 0 ? DONE_N - 1 : 0 ))
echo "done: $DONE_N repositories in ${ELAPSED}s ($(( DONE_N ? ELAPSED * 1000 / DONE_N : 0 ))ms each)" >&2
if [ -s "$OUT/failures.tsv" ]; then
    echo "failures: $(cut -f2 "$OUT/failures.tsv" | sort | uniq -c | tr '\n' ' ')" >&2
    echo "  retry timeouts with a larger blob cap, which avoids the backfill fetch:" >&2
    echo "  cut -f1 $OUT/failures.tsv | sort -u > retry.txt && CAP=1m TIMEOUT=3600 $0 retry.txt $OUT 4" >&2
fi
