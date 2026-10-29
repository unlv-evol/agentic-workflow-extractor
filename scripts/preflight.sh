#!/usr/bin/env bash
# Check a machine before the full run: everything mine.sh needs, plus a
# three-repository smoke run. Prints PASS / WARN / FAIL per check and exits
# non-zero if anything FAILs.
#
#   ./scripts/preflight.sh [out-dir] [work-dir]
#
# out-dir and work-dir are where the full run will write (defaults: out/full and
# $WORK or $TMPDIR), so their disks and filesystem types are the ones checked.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/out/full}"
WORK_DIR="${2:-${WORK:-${TMPDIR:-/tmp}}}"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
export GIT_TERMINAL_PROMPT=0

FAILED=0
pass() { printf '  PASS  %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=1; }

echo "== tools"
bash_major="${BASH_VERSINFO[0]}"
[ "$bash_major" -ge 4 ] && pass "bash $BASH_VERSION" || warn "bash $BASH_VERSION (4+ recommended)"

if command -v git > /dev/null; then
    gv="$(git --version | awk '{print $3}')"
    major="${gv%%.*}"; rest="${gv#*.}"; minor="${rest%%.*}"
    if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 30 ]; }; then
        pass "git $gv"
    else
        fail "git $gv (2.30+ required for --filter=blob:limit clones)"
    fi
else
    fail "git not found"
fi

if command -v timeout > /dev/null; then pass "timeout ($(command -v timeout))"
elif command -v gtimeout > /dev/null; then pass "gtimeout ($(command -v gtimeout))"
else fail "neither timeout nor gtimeout (install GNU coreutils)"; fi

if command -v python3 > /dev/null; then
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        pass "python3 $(python3 -c 'import platform; print(platform.python_version())')"
    else
        fail "python3 $(python3 -V 2>&1) (3.11+ required: tomllib)"
    fi
    python3 -c 'import yaml' 2>/dev/null && pass "PyYAML" || fail "PyYAML missing (pip install -e .)"
    where="$(python3 -c 'import awe, os; print(os.path.dirname(awe.__file__))' 2>/dev/null)"
    [ "$where" = "$ROOT/src/awe" ] && pass "awe imports from $where" \
        || fail "awe imports from '${where:-nowhere}', expected $ROOT/src/awe"
else
    fail "python3 not found"
fi

echo "== inputs"
LIST="$ROOT/data/seart/repos-2026-10-03.txt"
if [ -s "$LIST" ]; then
    pass "repository list: $(grep -cve '^\s*$' -e '^\s*#' "$LIST") repositories"
else
    fail "repository list missing: $LIST"
fi
if python3 -c 'import sys; from awe.config import load_config; c = load_config(sys.argv[1]); print(len(c.agents))' \
        "$ROOT/config/catalogue.toml" > /dev/null 2>&1; then
    pass "catalogue loads: $(cd "$ROOT/config" && (sha256sum catalogue.toml 2>/dev/null || shasum -a 256 catalogue.toml) | cut -c1-16)..."
else
    fail "config/catalogue.toml does not load"
fi

echo "== disks"
mkdir -p "$OUT" "$WORK_DIR" 2>/dev/null
for dir in "$OUT" "$WORK_DIR"; do
    avail_kb="$(df -Pk "$dir" | awk 'NR==2 {print $4}')"
    avail_gb=$(( avail_kb / 1024 / 1024 ))
    [ "$avail_gb" -ge 50 ] && pass "$dir: ${avail_gb} GB free" \
        || warn "$dir: only ${avail_gb} GB free (the dry run suggests a few GB of output; giant clones need tens of GB transiently)"
done
# GNU stat only: BSD/macOS `stat -f` means something else entirely.
fstype=""
stat --version > /dev/null 2>&1 && fstype="$(stat -f -c %T "$WORK_DIR" 2>/dev/null || true)"
case "$fstype" in
    tmpfs|ramfs) fail "work dir $WORK_DIR is $fstype: clones would live in RAM; set WORK=/some/disk/path" ;;
    "") warn "could not determine the filesystem type of $WORK_DIR" ;;
    *) pass "work dir filesystem: $fstype" ;;
esac
nofile="$(ulimit -n)"
[ "$nofile" = unlimited ] || [ "$nofile" -ge 1024 ] && pass "open files limit: $nofile" \
    || warn "open files limit $nofile is low"
echo "  INFO  cores: $(getconf _NPROCESSORS_ONLN 2>/dev/null || echo ?)"

echo "== network and git behaviour"
SCRATCH="$(mktemp -d "${WORK_DIR%/}/awe-preflight.XXXXXX")"
trap 'chmod -R u+w "$SCRATCH" 2>/dev/null; rm -rf "$SCRATCH"' EXIT
if git ls-remote https://github.com/octocat/Hello-World.git HEAD > /dev/null 2>&1; then
    pass "github.com reachable over https"
else
    fail "cannot reach github.com over https"
fi
# The prefetch path depends on GIT_NO_LAZY_FETCH: under it, cat-file must
# report an absent blob as missing instead of fetching it. Test the behaviour,
# not the version number.
if git clone --quiet --filter=blob:none --no-checkout \
        https://github.com/octocat/Hello-World.git "$SCRATCH/lazy" 2>/dev/null; then
    blob="$(git -C "$SCRATCH/lazy" ls-tree HEAD README | awk '{print $3}')"
    answer="$(echo "$blob" | GIT_NO_LAZY_FETCH=1 git -C "$SCRATCH/lazy" cat-file --batch-check 2>/dev/null)"
    case "$answer" in
        *missing*) pass "GIT_NO_LAZY_FETCH honoured (missing blobs found without fetching)" ;;
        *) warn "GIT_NO_LAZY_FETCH not honoured by git $(git --version | awk '{print $3}'): missing blobs will be fetched one round trip each; a newer git is faster" ;;
    esac
else
    fail "could not make a partial clone"
fi

echo "== smoke run (3 repositories)"
printf 'octocat/Hello-World\ngithub/gh-aw\nawe-preflight-no-such-owner/no-such-repo\n' > "$SCRATCH/list.txt"
if WORK="$SCRATCH/work" TIMEOUT=120 "$ROOT/scripts/mine.sh" "$SCRATCH/list.txt" "$SCRATCH/out" 3 \
        > "$SCRATCH/mine.log" 2>&1; then
    # csv writes \r\n line endings; drop the \r before comparing.
    got="$(tr -d '\r' < "$SCRATCH/out/repositories.csv" | cut -d, -f1,13 | tail -n +2 | sort | tr '\n' ' ')"
    want="awe-preflight-no-such-owner/no-such-repo,clone-failed github/gh-aw,excluded octocat/Hello-World,ok "
    [ "$got" = "$want" ] && pass "statuses ok / excluded / clone-failed as expected" \
        || fail "unexpected statuses: $got (log: $SCRATCH/mine.log)"
    [ -f "$SCRATCH/out/RUN.txt" ] && pass "run provenance written (RUN.txt)" || fail "RUN.txt not written"
    [ -d "$SCRATCH/out/content" ] && pass "content store created" || fail "content store not created"
else
    fail "mine.sh smoke run failed:"; sed 's/^/        /' "$SCRATCH/mine.log"
fi

echo
if [ "$FAILED" -ne 0 ]; then echo "preflight: FAILED"; exit 1; fi
echo "preflight: ready"
