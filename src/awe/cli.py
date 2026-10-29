"""Command line interface for awe."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict
from pathlib import Path

from .config import ConfigError, load_config
from .extract import RepoSummary, extract_repository
from .gitlog import GitError, repository_name

REVISION_FIELDS = [
    "repository",
    "uid",
    "commit_hash",
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
    "authored_date",
    "commit_date",
    "file_path",
    "previous_file_path",
    "file_hash",
    "previous_file_hash",
    "content_sha256",
    "git_change_type",
    "symbolic_link",
    "previous_symbolic_link",
    "parse_ok",
    "workflow_name",
    "triggers",
    "n_jobs",
    "n_steps",
    "permissions",
    "agents",
    "strong_agents",
    "n_evidence",
    "agentic",
    "actor_gated",
]

STEP_FIELDS = [
    "repository",
    "uid",
    "commit_hash",
    "commit_date",
    "file_path",
    "content_sha256",
    "parse_ok",
    "agent",
    "scope",
    "evidence_kind",
    "evidence_kinds",
    "n_evidence",
    "pattern",
    "job_id",
    "step_index",
    "step_name",
    "action",
    "action_version",
    "triggers",
    "tasks",
]

MANIFEST_FIELDS = [
    "repository",
    "uid",
    "commit_hash",
    "commit_date",
    "file_path",
    "content_sha256",
    "artifact_kind",
    "parse_ok",
    "agent_id",
    "engine_version",
    "schema_version",
    "compiler_version",
    "model",
    "strict",
    "n_tools",
    "tools",
    "n_mcp_tools",
    "mcp_tools",
    "n_safe_outputs",
    "safe_outputs",
    "n_secrets",
    "secrets",
    "n_actions",
    "n_containers",
    "n_imports",
    "imports",
    "sandboxed",
    "network",
    "permissions",
    "n_evals",
    "timeout_minutes",
    "max_turns",
    "frontmatter_keys",
]

SUMMARY_FIELDS = [
    "repository",
    "n_commits",
    "n_workflow_files",
    "n_workflow_revisions",
    "n_agentic_files",
    "n_agentic_revisions",
    "agents",
    "first_agentic_date",
    "last_agentic_date",
    "n_blobs_requested",
    "n_blobs_read",
    "n_blobs_missing",
    "status",
]

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.toml"


class _Writer:
    """Append-only CSV writer that creates the header on first use."""

    def __init__(self, path: Path | None, fields: list[str]) -> None:
        self.fields = fields
        self.handle = None
        self.writer = None
        if path is not None:
            # Empty counts as new: a process killed between creating the file
            # and flushing its header leaves a zero-byte shard, and appending
            # to it headerless makes the merge drop the first data row.
            new = not path.exists() or path.stat().st_size == 0
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("a", newline="", encoding="utf-8")
            self.writer = csv.DictWriter(
                self.handle, fieldnames=fields, extrasaction="ignore"
            )
            if new:
                self.writer.writeheader()

    def write(self, rows) -> None:
        if self.writer is None:
            return
        for row in rows:
            self.writer.writerow(row)

    def close(self) -> None:
        if self.handle:
            self.handle.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awe",
        description=(
            "Agentic Workflow Extractor: mine git histories of CI workflow "
            "files and detect coding-agent invocations inside them."
        ),
    )
    parser.add_argument(
        "repositories",
        nargs="*",
        help="paths to local git clones (use --from-file for large batches)",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="detector catalogue (TOML). Defaults to the bundled catalogue.",
    )
    parser.add_argument(
        "--from-file",
        help="file listing one clone path per line",
    )
    parser.add_argument("-o", "--out-dir", help="directory for the CSV outputs")
    parser.add_argument(
        "--content-dir",
        help="store each distinct workflow revision as <sha256>.yml here",
    )
    parser.add_argument(
        "--rev", default="HEAD", help="commit reference to start from (default: HEAD)"
    )
    parser.add_argument(
        "--since", help="ignore commits older than this date (e.g. 2024-01-01)"
    )
    parser.add_argument(
        "--include-weak",
        action="store_true",
        help="count env/url evidence as sufficient to call a revision agentic",
    )
    parser.add_argument(
        "--keep-all",
        action="store_true",
        help="emit every workflow revision, not just agentic file histories",
    )
    parser.add_argument(
        "--no-prefetch",
        action="store_true",
        help=(
            "disable the batched blob prefetch used on partial (--filter=blob:none) "
            "clones; without it git backfills one blob per network round trip"
        ),
    )
    parser.add_argument(
        "--repository",
        help=(
            "owner/name of the single repository given, instead of reading it "
            "from the clone's remote; lets an excluded repository be recorded "
            "without cloning it"
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="abort on the first repository that errors out",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    targets = list(args.repositories)
    if args.from_file:
        targets += [
            line.strip()
            for line in Path(args.from_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not targets:
        print("awe: no repositories given", file=sys.stderr)
        return 2
    if args.repository and len(targets) != 1:
        print("awe: --repository names a single repository", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as error:
        print(f"awe: {error}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else None
    revision_writer = _Writer(
        out_dir / "revisions.csv" if out_dir else None, REVISION_FIELDS
    )
    step_writer = _Writer(out_dir / "steps.csv" if out_dir else None, STEP_FIELDS)
    summary_writer = _Writer(
        out_dir / "repositories.csv" if out_dir else None, SUMMARY_FIELDS
    )
    manifest_writer = _Writer(
        out_dir / "ghaw_manifests.csv" if out_dir else None, MANIFEST_FIELDS
    )
    stdout_writer = None
    if out_dir is None:
        stdout_writer = csv.DictWriter(
            sys.stdout, fieldnames=STEP_FIELDS, extrasaction="ignore"
        )
        stdout_writer.writeheader()

    failures = 0
    try:
        for index, target in enumerate(targets, start=1):
            try:
                result = extract_repository(
                    target,
                    config,
                    repository=args.repository,
                    rev=args.rev,
                    since=args.since,
                    include_weak=args.include_weak,
                    keep_all=args.keep_all,
                    content_dir=args.content_dir,
                    prefetch=False if args.no_prefetch else None,
                )
            except (GitError, OSError) as error:
                failures += 1
                print(f"awe: {target}: {error}", file=sys.stderr)
                # Still emit a row. A repository that vanishes from the summary
                # is indistinguishable from one that was never in the sample,
                # which quietly biases any adoption rate computed from it.
                summary_writer.write(
                    [
                        {
                            **asdict(
                                RepoSummary(
                                    repository=args.repository
                                    or repository_name(target)
                                )
                            ),
                            "status": "error",
                        }
                    ]
                )
                if args.fail_fast:
                    return 1
                continue

            revision_writer.write(result.revisions)
            step_writer.write(result.steps)
            summary_writer.write([asdict(result.summary)])
            manifest_writer.write(result.manifests)
            if stdout_writer:
                for row in result.steps:
                    stdout_writer.writerow(row)

            if not args.quiet:
                summary = result.summary
                print(
                    f"[{index}/{len(targets)}] {summary.repository}: "
                    f"{summary.n_agentic_files}/{summary.n_workflow_files} workflows "
                    f"agentic, {len(result.steps)} agent steps"
                    + (f", {len(result.manifests)} gh-aw" if result.manifests else "")
                    + f" ({summary.agents or 'none'})",
                    file=sys.stderr,
                )
    finally:
        revision_writer.close()
        step_writer.close()
        summary_writer.close()
        manifest_writer.close()

    return 1 if failures and args.fail_fast else 0


if __name__ == "__main__":
    raise SystemExit(main())
