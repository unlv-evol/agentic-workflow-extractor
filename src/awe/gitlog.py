"""First-parent git history traversal over a set of pathspecs.

Uses git plumbing directly rather than a library wrapper: at the scale this
tool targets (10^5 repositories) the cost of instantiating Python objects per
commit dominates, and `git log --raw` already hands us blob hashes, file modes
and rename detection in a single pass.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import count

# NUL-separated header, prefixed by \x01 so commits can be split apart even
# though the raw entries that follow are themselves NUL-separated.
_RECORD_SEP = "\x01"
# git terminates the commit format with its own NUL under -z, so the format
# must not end in %x00 itself: that would yield a spurious empty token.
_FORMAT = _RECORD_SEP + "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%aI%x00%cI"

_SYMLINK_MODE = "120000"
_NULL_MODE = "000000"
_NULL_SHA = "0" * 40

CHANGE_TYPES = {"A": "Added", "M": "Modified", "D": "Deleted", "R": "Renamed"}


@dataclass(frozen=True)
class FileRevision:
    """One touch of one matched file by one commit."""

    uid: str
    commit_hash: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    authored_date: str
    commit_date: str
    file_path: str
    previous_file_path: str | None
    file_hash: str | None
    previous_file_hash: str | None
    git_change_type: str
    symbolic_link: bool
    previous_symbolic_link: bool


class GitError(RuntimeError):
    pass


def _run(repo: str, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _iter_raw_commits(
    repo: str, pathspecs: list[str], rev: str, since: str | None
) -> Iterator[tuple[list[str], list[str]]]:
    """Yield (header fields, raw entry tokens) per commit, newest first."""
    args = [
        "log",
        "--first-parent",
        "--raw",
        "--no-abbrev",
        "-M",
        "-z",
        f"--format={_FORMAT}",
        rev,
    ]
    if since:
        args.append(f"--since={since}")
    args += ["--", *pathspecs]

    output = _run(repo, args)
    for chunk in output.split(_RECORD_SEP):
        if not chunk.strip():
            continue
        tokens = chunk.split("\x00")
        header, rest = tokens[:7], tokens[7:]
        if len(header) < 7:
            continue
        # The first raw entry is separated from the header by a newline.
        yield header, [t.lstrip("\n") for t in rest if t.strip()]


def _parse_entries(tokens: list[str]) -> Iterator[dict]:
    """Walk the token stream of one commit's raw entries."""
    index = 0
    while index < len(tokens):
        meta = tokens[index]
        if not meta.startswith(":"):
            index += 1
            continue
        fields = meta[1:].split()
        if len(fields) < 5:
            index += 1
            continue
        old_mode, new_mode, old_sha, new_sha, status = fields[:5]
        code = status[0]
        if code == "R":
            old_path, new_path = tokens[index + 1], tokens[index + 2]
            index += 3
        else:
            old_path = new_path = tokens[index + 1]
            index += 2
        yield {
            "old_mode": old_mode,
            "new_mode": new_mode,
            "old_sha": old_sha,
            "new_sha": new_sha,
            "code": code,
            "old_path": old_path,
            "new_path": new_path,
        }


def iter_file_revisions(
    repo: str,
    pathspecs: list[str],
    rev: str = "HEAD",
    since: str | None = None,
    uid_prefix: str = "f",
) -> Iterator[FileRevision]:
    """Yield every revision of every file matching `pathspecs`, newest first.

    A `uid` is attached to each revision and survives both content changes and
    renames, so consumers can reconstruct the full history of a logical file
    even when it moved. Because history is walked newest-first, uids are
    allocated on first (i.e. latest) sight and propagated backwards through
    rename entries.
    """
    counter = count(1)
    alive: dict[str, str] = {}

    def uid_for(path: str) -> str:
        if path not in alive:
            alive[path] = f"{uid_prefix}{next(counter):08d}"
        return alive[path]

    for header, tokens in _iter_raw_commits(repo, pathspecs, rev, since):
        (
            commit_hash,
            author_name,
            author_email,
            committer_name,
            committer_email,
            authored_date,
            commit_date,
        ) = header

        for entry in _parse_entries(tokens):
            code = entry["code"]
            path = entry["new_path"]

            if code == "R":
                uid = uid_for(path)
                del alive[path]
                alive[entry["old_path"]] = uid
            elif code == "A":
                uid = uid_for(path)
                del alive[path]
            elif code == "D":
                uid = uid_for(path)
            else:  # M, T, C ...
                uid = uid_for(path)

            yield FileRevision(
                uid=uid,
                commit_hash=commit_hash,
                author_name=author_name,
                author_email=author_email,
                committer_name=committer_name,
                committer_email=committer_email,
                authored_date=authored_date,
                commit_date=commit_date,
                file_path=path,
                previous_file_path=(
                    entry["old_path"] if entry["old_path"] != path else None
                ),
                file_hash=None if entry["new_sha"] == _NULL_SHA else entry["new_sha"],
                previous_file_hash=(
                    None if entry["old_sha"] == _NULL_SHA else entry["old_sha"]
                ),
                git_change_type=CHANGE_TYPES.get(code, code),
                symbolic_link=entry["new_mode"] == _SYMLINK_MODE,
                previous_symbolic_link=entry["old_mode"] == _SYMLINK_MODE,
            )


def commit_count(repo: str, rev: str = "HEAD") -> int:
    """Total commits reachable from `rev`, or 0 if it cannot be determined.

    Cheap once a clone exists, and it is the only way to apply the "engineered
    project" threshold used by comparable datasets (>= 300 commits): GitHub's
    search API cannot filter on it, so the criterion has to be applied after
    the fact rather than in the sampling query.
    """
    try:
        return int(_run(repo, ["rev-list", "--count", rev]).strip() or 0)
    except (GitError, ValueError):
        return 0


def is_partial_clone(repo: str) -> bool:
    """Whether the clone omits blobs and backfills them from a promisor remote."""
    try:
        return bool(_run(repo, ["config", "--get", "remote.origin.promisor"]).strip())
    except GitError:
        return False


def partial_clone_filter(repo: str) -> str | None:
    """The filter spec a partial clone was made with, e.g. `blob:limit=65536`."""
    try:
        return (
            _run(repo, ["config", "--get", "remote.origin.partialclonefilter"]).strip()
            or None
        )
    except GitError:
        return None


def missing_blobs(repo: str, blob_hashes: list[str]) -> list[str]:
    """Which of these object ids are absent locally, touching no network.

    `GIT_NO_LAZY_FETCH=1` makes `cat-file` report `<oid> missing` instead of
    silently fetching from the promisor remote, so the whole set can be
    partitioned in one local pass. This replaces guessing from the clone's
    filter spec: a `blob:limit` clone has most of what we need but not the
    oversize files, and only a measurement says which.
    """
    wanted = [h for h in dict.fromkeys(blob_hashes) if h and h != _NULL_SHA]
    if not wanted:
        return []

    proc = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch-check"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    assert proc.stdin and proc.stdout

    def feed() -> None:
        try:
            for sha in wanted:
                proc.stdin.write(f"{sha}\n".encode())
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()

    absent: list[str] = []
    try:
        for sha in wanted:
            line = proc.stdout.readline().decode(errors="replace").strip()
            if not line:
                # Stream ended early; treat the remainder as unknown rather
                # than as present, so they are prefetched rather than lost.
                absent.append(sha)
                continue
            if line.endswith(("missing", "ambiguous")):
                absent.append(line.split()[0] if line.split() else sha)
    finally:
        writer.join(timeout=5)
        proc.stdout.close()
        proc.wait()
    return absent


def prefetch_blobs(repo: str, blob_hashes: list[str], batch: int = 2000) -> int:
    """Fetch missing blobs from the promisor remote in bulk.

    On a `--filter=blob:none` clone, `git cat-file` backfills missing objects
    one at a time, costing a network round trip per blob. Requesting the object
    ids up front collapses that into a handful of fetches, which is the
    difference between minutes and seconds per repository. Everything not asked
    for stays undownloaded, so a repository's non-workflow blobs are never
    transferred at all.

    We deliberately do not first test which blobs are absent: the only
    local-only way to ask is `cat-file` under GIT_NO_LAZY_FETCH, which aborts
    on the first missing object instead of reporting it. Requesting every id is
    cheaper than the alternative, since git resolves duplicates server-side.

    Returns the number of object ids requested.
    """
    wanted = [h for h in dict.fromkeys(blob_hashes) if h and h != _NULL_SHA]
    if not wanted:
        return 0
    for start in range(0, len(wanted), batch):
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--quiet",
                "origin",
                *wanted[start : start + batch],
            ],
            capture_output=True,
        )
    return len(wanted)


def _read_exactly(stream, size: int) -> bytes | None:
    """Read exactly `size` bytes, or None at end of stream.

    `read(n)` on a pipe is allowed to return short, and a short read here
    desynchronises the whole `cat-file --batch` stream: the next header lands
    mid-payload and every subsequent blob is mis-sliced. Loop until satisfied.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def iter_blobs(repo: str, blob_hashes: list[str]) -> Iterator[tuple[str, str]]:
    """Yield `(sha, content)` for each requested blob, via one `cat-file --batch`.

    The request list is fed from a writer thread rather than written up front.
    Writing it all first deadlocks on any large repository: the object ids run
    to megabytes, git cannot consume them without emitting output, its stdout
    pipe fills at ~64 KB, and both processes then block on each other. It
    survives small repositories, which is what makes it dangerous -- on
    `github/gh-aw` (235,511 blobs, 9.6 MB of ids) the extractor simply hung,
    at 0% CPU, with no error.

    Yielding rather than returning a dict also keeps peak memory bounded by the
    largest single blob instead of by the whole of a repository's history.
    """
    wanted = [h for h in dict.fromkeys(blob_hashes) if h and h != _NULL_SHA]
    if not wanted:
        return

    proc = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout

    def feed() -> None:
        try:
            for sha in wanted:
                proc.stdin.write(f"{sha}\n".encode())
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        for _ in wanted:
            header = proc.stdout.readline().decode(errors="replace").strip()
            if not header or header.endswith(("missing", "ambiguous")):
                continue
            parts = header.split()
            if len(parts) != 3 or not parts[2].isdigit():
                # Anything but `<sha> <type> <size>` means we have lost sync
                # with the stream; there is no safe way to resume mid-object,
                # so stop rather than mis-slice the remaining blobs.
                break
            sha, _kind, size = parts[0], parts[1], int(parts[2])
            payload = _read_exactly(proc.stdout, size)
            if payload is None:
                break
            _read_exactly(proc.stdout, 1)  # trailing newline
            yield sha, payload.decode("utf-8", errors="replace")
    finally:
        writer.join(timeout=5)
        proc.stdout.close()
        proc.wait()


def read_blobs(repo: str, blob_hashes: list[str]) -> dict[str, str]:
    """Eager form of `iter_blobs`, for callers that need random access."""
    return dict(iter_blobs(repo, blob_hashes))


def repository_name(repo: str) -> str:
    """Best-effort owner/name for a clone, falling back to the directory name."""
    import os
    import re

    try:
        url = _run(repo, ["config", "--get", "remote.origin.url"]).strip()
    except GitError:
        url = ""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if match:
        return match.group(1)
    return os.path.basename(os.path.abspath(repo.rstrip("/")))
