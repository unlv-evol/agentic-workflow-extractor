"""Per-repository extraction pipeline."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config
from .detect import WorkflowFacts, analyse, classify_tasks, group_invocations, is_gated
from .ghaw import GhawFacts
from .gitlog import (
    FileRevision,
    commit_count,
    is_partial_clone,
    iter_blobs,
    iter_file_revisions,
    missing_blobs,
    prefetch_blobs,
    repository_name,
)


@dataclass
class RepoSummary:
    """Denominators for the repository, retained even when nothing is agentic.

    The blob counters are not diagnostics: on a partial clone an unfetched blob
    is simply absent, so a repository whose blobs failed to transfer looks
    exactly like a repository with no agentic workflows. Recording what was
    asked for against what arrived makes that difference visible, and makes
    "every blob was read" a checkable property of the published dataset rather
    than an assumption.
    """

    repository: str
    n_commits: int = 0
    n_workflow_files: int = 0
    n_workflow_revisions: int = 0
    n_agentic_files: int = 0
    n_agentic_revisions: int = 0
    agents: str = ""
    first_agentic_date: str | None = None
    last_agentic_date: str | None = None
    n_blobs_requested: int = 0
    n_blobs_read: int = 0
    n_blobs_missing: int = 0
    status: str = "ok"


@dataclass
class Result:
    summary: RepoSummary
    revisions: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    manifests: list[dict] = field(default_factory=list)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _store(content_path: Path, text: str) -> None:
    """Write one revision under its SHA-256, atomically.

    Parallel workers share the content store, and two repositories often hold
    the same template file; a rename is atomic, so a reader never sees half a
    file and concurrent writers of the same content cannot interleave.
    """
    target = content_path / f"{_sha256(text)}.yml"
    if target.exists():
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def _joined(values) -> str:
    return "|".join(values)


def _manifest_row(
    repository: str,
    revision: FileRevision,
    ghaw: GhawFacts,
    content_sha: str | None,
) -> dict:
    """One row per revision of one gh-aw artifact.

    Unlike the rest of the dataset this is not "an agent was detected here".
    Every column is a constraint the maintainer placed on the agent -- which
    tools it may call, what it may emit, whether it runs sandboxed -- which is
    what makes the tier worth extracting separately.
    """
    return {
        "repository": repository,
        "uid": revision.uid,
        "commit_hash": revision.commit_hash,
        "commit_date": revision.commit_date,
        "file_path": revision.file_path,
        "content_sha256": content_sha,
        "artifact_kind": ghaw.kind,
        "parse_ok": ghaw.parse_ok,
        "agent_id": ghaw.agent_id,
        "engine_version": ghaw.engine_version,
        "schema_version": ghaw.schema_version,
        "compiler_version": ghaw.compiler_version,
        "model": ghaw.model,
        "strict": ghaw.strict,
        "n_tools": len(ghaw.tools),
        "tools": _joined(ghaw.tools),
        "n_mcp_tools": len(ghaw.mcp_tools),
        "mcp_tools": _joined(ghaw.mcp_tools),
        "n_safe_outputs": len(ghaw.safe_outputs),
        "safe_outputs": _joined(ghaw.safe_outputs),
        "n_secrets": len(ghaw.secrets),
        "secrets": _joined(ghaw.secrets),
        "n_actions": len(ghaw.actions),
        "n_containers": len(ghaw.containers),
        "n_imports": len(ghaw.imports),
        "imports": _joined(ghaw.imports),
        "sandboxed": ghaw.sandboxed,
        "network": ghaw.network,
        "permissions": ghaw.permissions,
        "n_evals": ghaw.n_evals,
        "timeout_minutes": ghaw.timeout_minutes,
        "max_turns": ghaw.max_turns,
        "frontmatter_keys": _joined(ghaw.frontmatter_keys),
    }


def _revision_row(
    repository: str,
    revision: FileRevision,
    facts: WorkflowFacts | None,
    content_sha: str | None,
    gated: bool,
    include_weak: bool,
) -> dict:
    row = asdict(revision)
    row["repository"] = repository
    row["content_sha256"] = content_sha
    if facts is None:
        row.update(
            parse_ok=None,
            workflow_name=None,
            triggers=None,
            n_jobs=None,
            n_steps=None,
            permissions=None,
            agents="",
            strong_agents="",
            n_evidence=0,
            agentic=False,
            actor_gated=None,
        )
    else:
        row.update(
            parse_ok=facts.parse_ok,
            workflow_name=facts.workflow_name,
            triggers="|".join(facts.triggers),
            n_jobs=facts.n_jobs,
            n_steps=facts.n_steps,
            permissions=facts.permissions,
            agents="|".join(facts.agents),
            strong_agents="|".join(facts.strong_agents),
            n_evidence=len(facts.evidence),
            agentic=facts.is_agentic(include_weak),
            actor_gated=gated,
        )
    return row


def extract_repository(
    repo: str,
    config: Config,
    rev: str = "HEAD",
    since: str | None = None,
    repository: str | None = None,
    include_weak: bool = False,
    keep_all: bool = False,
    content_dir: str | Path | None = None,
    prefetch: bool | None = None,
) -> Result:
    """Mine one repository for agentic CI workflow histories.

    By default only the histories of files that are agentic *at some point* are
    emitted, including their pre- and post-agentic revisions, so that adoption
    and abandonment are both observable. Repository-level denominators are kept
    regardless, so the proportion of workflows that are agentic can still be
    computed without storing every workflow revision ever made.
    """
    repository = repository or repository_name(repo)
    summary = RepoSummary(repository=repository)
    result = Result(summary=summary)

    if config.is_excluded(repository):
        summary.status = "excluded"
        return result

    summary.n_commits = commit_count(repo, rev)

    revisions = list(
        iter_file_revisions(repo, config.pathspecs, rev=rev, since=since)
    )
    if not revisions:
        return result

    summary.n_workflow_revisions = len(revisions)
    summary.n_workflow_files = len({r.uid for r in revisions})

    blob_hashes = [r.file_hash for r in revisions if r.file_hash]
    if prefetch is None:
        prefetch = is_partial_clone(repo)
    if prefetch:
        # Ask for exactly what is absent. Requesting everything is what made
        # prefetch pathological on size-capped clones -- the fetch is priced by
        # repository size, not by how many ids are in it, so re-requesting
        # hundreds of thousands of already-local blobs stalled for minutes and
        # downloaded nothing. Most clones need a handful of oversize files.
        absent = missing_blobs(repo, blob_hashes)
        if absent:
            prefetch_blobs(repo, absent)

    content_path = Path(content_dir) if content_dir else None
    if content_path:
        content_path.mkdir(parents=True, exist_ok=True)

    # Which paths each blob is seen at. Detection depends on the path as well
    # as the content, so a blob reachable at two paths is analysed once per
    # path; most are seen at exactly one.
    paths_by_blob: dict[str, set[str]] = {}
    for revision in revisions:
        if revision.file_hash:
            paths_by_blob.setdefault(revision.file_hash, set()).add(revision.file_path)

    # Blob contents are consumed as they stream out of `cat-file` and dropped
    # immediately, keeping only the derived facts. Holding every revision of
    # every workflow file in memory does not survive a large repository:
    # `github/gh-aw` alone has 235k workflow blobs of ~100 kB each.
    facts_cache: dict[tuple[str, str], WorkflowFacts] = {}
    gated_cache: dict[str, bool] = {}
    content_shas: dict[str, str] = {}

    summary.n_blobs_requested = len(paths_by_blob)
    for blob, text in iter_blobs(repo, blob_hashes):
        content_sha = _sha256(text)
        content_shas[blob] = content_sha
        gated_cache[blob] = is_gated(text)
        for path in paths_by_blob.get(blob, ()):
            facts_cache[(blob, path)] = analyse(config, text, path)

    summary.n_blobs_read = len(content_shas)
    summary.n_blobs_missing = summary.n_blobs_requested - summary.n_blobs_read
    if summary.n_blobs_missing:
        summary.status = "incomplete"

    def facts_for(revision: FileRevision) -> WorkflowFacts | None:
        if not revision.file_hash:
            return None
        return facts_cache.get((revision.file_hash, revision.file_path))

    agentic_uids = {
        r.uid
        for r in revisions
        if (facts := facts_for(r)) is not None and facts.is_agentic(include_weak)
    }
    summary.n_agentic_files = len(agentic_uids)

    if content_path:
        # Only what is emitted is stored. Agentic histories are a few percent of
        # workflow revisions (29k revisions per 500 repositories in the dry run,
        # 275 of them emitted), and storing every revision read would put about
        # ten million files in the content store for the full frame. Which
        # blobs are needed is only known after classification, hence a second,
        # small `cat-file` pass rather than writing during the first.
        needed = [
            r.file_hash
            for r in revisions
            if r.file_hash and (keep_all or r.uid in agentic_uids)
        ]
        for _blob, text in iter_blobs(repo, needed):
            _store(content_path, text)

    seen_agents: dict[str, None] = {}
    for revision in revisions:
        if not keep_all and revision.uid not in agentic_uids:
            continue

        facts = facts_for(revision)
        content_sha = content_shas.get(revision.file_hash) if revision.file_hash else None
        gated = gated_cache.get(revision.file_hash, False) if revision.file_hash else False

        result.revisions.append(
            _revision_row(repository, revision, facts, content_sha, gated, include_weak)
        )

        if facts is None or not facts.is_agentic(include_weak):
            continue

        if facts.ghaw is not None:
            result.manifests.append(
                _manifest_row(repository, revision, facts.ghaw, content_sha)
            )

        summary.n_agentic_revisions += 1
        date = revision.commit_date
        if summary.last_agentic_date is None or date > summary.last_agentic_date:
            summary.last_agentic_date = date
        if summary.first_agentic_date is None or date < summary.first_agentic_date:
            summary.first_agentic_date = date

        for item, members in group_invocations(facts.evidence):
            seen_agents.setdefault(item.agent)
            uses_ref = item.uses_ref or ""
            action, _, version = uses_ref.partition("@")
            result.steps.append(
                {
                    "repository": repository,
                    "uid": revision.uid,
                    "commit_hash": revision.commit_hash,
                    "commit_date": revision.commit_date,
                    "file_path": revision.file_path,
                    "content_sha256": content_sha,
                    "parse_ok": facts.parse_ok,
                    "agent": item.agent,
                    "scope": "step" if item.step_index is not None else "file",
                    "evidence_kind": item.kind,
                    "evidence_kinds": "|".join(
                        dict.fromkeys(m.kind for m in members)
                    ),
                    "n_evidence": len(members),
                    "pattern": item.pattern,
                    "job_id": item.job_id,
                    "step_index": item.step_index,
                    "step_name": item.step_name,
                    "action": action or None,
                    "action_version": version or None,
                    "triggers": "|".join(facts.triggers),
                    "tasks": "|".join(classify_tasks(config, facts, item)),
                }
            )

    summary.agents = "|".join(seen_agents)
    return result
