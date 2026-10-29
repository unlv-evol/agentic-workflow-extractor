import subprocess
import textwrap
from pathlib import Path

import pytest

from awe.config import Config, PurePosixPathLike, load_config
from awe.detect import analyse, is_gated
from awe.extract import extract_repository
from awe.gitlog import iter_file_revisions

CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.toml"


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(CONFIG)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "dev@example.org")
    git(path, "config", "user.name", "Dev")
    (path / ".github" / "workflows").mkdir(parents=True)
    return path


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


PLAIN = textwrap.dedent(
    """
    name: CI
    on: [push]
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: make test
    """
)

AGENTIC = textwrap.dedent(
    """
    name: Claude Review
    on:
      pull_request:
        types: [opened]
    jobs:
      review:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - name: Ask Claude
            uses: anthropics/claude-code-action@v1
            env:
              ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    """
)


# --- glob matching ---------------------------------------------------------


@pytest.mark.parametrize(
    "path,glob,expected",
    [
        (".github/workflows/ci.yml", ".github/workflows/*.yml", True),
        (".github/workflows/sub/ci.yml", ".github/workflows/*.yml", False),
        (".github/workflows/ci.yaml", ".github/workflows/*.yml", False),
        ("docs/AGENTS.md", "**/AGENTS.md", True),
        ("AGENTS.md", "**/AGENTS.md", True),
        ("a/b/c/AGENTS.md", "**/AGENTS.md", True),
        ("AGENTS.md.bak", "**/AGENTS.md", False),
    ],
)
def test_glob_matching(path, glob, expected):
    assert PurePosixPathLike(path).match_glob(glob) is expected


# --- detection -------------------------------------------------------------


def test_plain_workflow_is_not_agentic(config):
    facts = analyse(config, PLAIN)
    assert facts.parse_ok
    assert facts.triggers == ("push",)
    assert not facts.is_agentic()


def test_agentic_workflow_detected(config):
    facts = analyse(config, AGENTIC)
    assert facts.strong_agents == ("claude",)
    assert facts.triggers == ("pull_request",)
    assert facts.n_steps == 2
    kinds = {(e.agent, e.kind) for e in facts.evidence}
    assert ("claude", "uses") in kinds
    assert ("claude", "env") in kinds


def test_credential_alone_is_weak_evidence_only(config):
    """An API key feeding a test suite must not count as an agent invocation."""
    workflow = textwrap.dedent(
        """
        name: Tests
        on: [push]
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: pytest
                env:
                  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        """
    )
    facts = analyse(config, workflow)
    assert facts.agents == ("claude",)
    assert facts.strong_agents == ()
    assert not facts.is_agentic()
    assert facts.is_agentic(include_weak=True)


def test_cli_agent_invocation_detected(config):
    workflow = textwrap.dedent(
        """
        on: [schedule]
        jobs:
          nightly:
            steps:
              - run: |
                  npm install -g @openai/codex
                  codex exec "update the changelog"
        """
    )
    facts = analyse(config, workflow)
    assert facts.strong_agents == ("codex",)


def test_reusable_workflow_call_detected(config):
    workflow = textwrap.dedent(
        """
        on: [pull_request]
        jobs:
          review:
            uses: anthropics/claude-code-action@v1
        """
    )
    assert analyse(config, workflow).strong_agents == ("claude",)


def test_unquoted_on_key_survives_yaml_boolean_coercion(config):
    """YAML 1.1 parses a bare `on:` as the boolean True."""
    facts = analyse(config, "on: [push]\njobs: {}\n")
    assert facts.triggers == ("push",)


def test_malformed_yaml_falls_back_to_text_scan(config):
    facts = analyse(config, AGENTIC + "\n\tthis: is: not: yaml\n  - [")
    assert not facts.parse_ok
    assert facts.strong_agents == ("claude",)
    # Degraded mode reports one evidence item per agent, not one per pattern.
    assert len(facts.evidence) == 1


def test_impossible_date_falls_back_to_text_scan(config):
    """PyYAML raises ValueError, not YAMLError, for `2023-02-30`."""
    facts = analyse(config, AGENTIC + "\nreleased: 2023-02-30\n")
    assert not facts.parse_ok
    assert facts.strong_agents == ("claude",)


def test_ghaw_source_with_impossible_date_is_unparseable(config):
    content = "---\non: issues\nreleased: 2023-02-30\n---\nDo the thing.\n"
    facts = analyse(config, content, ".github/workflows/refiner.md")
    assert facts.ghaw is not None and not facts.ghaw.parse_ok


def test_gate_detection():
    assert is_gated("if: github.actor == 'dependabot[bot]'")
    assert not is_gated("if: success()")


# --- history traversal -----------------------------------------------------


def test_uid_survives_rename_and_modification(repo, config):
    workflow = repo / ".github" / "workflows"
    (workflow / "claude.yml").write_text(AGENTIC)
    commit(repo, "add agent workflow")
    git(repo, "mv", ".github/workflows/claude.yml", ".github/workflows/review.yml")
    commit(repo, "rename")
    (workflow / "review.yml").write_text(AGENTIC + "\n# tweak\n")
    commit(repo, "tweak")

    revisions = list(iter_file_revisions(str(repo), config.pathspecs))
    assert len({r.uid for r in revisions}) == 1
    assert {r.git_change_type for r in revisions} == {"Added", "Renamed", "Modified"}


def test_deleted_and_recreated_files_get_distinct_uids(repo, config):
    workflow = repo / ".github" / "workflows"
    (workflow / "a.yml").write_text(AGENTIC)
    commit(repo, "add")
    git(repo, "rm", "-q", ".github/workflows/a.yml")
    commit(repo, "remove")
    workflow.mkdir(parents=True, exist_ok=True)  # git prunes emptied directories
    (workflow / "a.yml").write_text(AGENTIC)
    commit(repo, "re-add")

    revisions = list(iter_file_revisions(str(repo), config.pathspecs))
    assert len({r.uid for r in revisions}) == 2


# --- end to end ------------------------------------------------------------


def test_extract_keeps_agentic_history_and_repo_denominators(repo, config):
    workflow = repo / ".github" / "workflows"
    (workflow / "ci.yml").write_text(PLAIN)
    commit(repo, "add ci")
    (workflow / "agent.yml").write_text(PLAIN)
    commit(repo, "add second workflow")
    # The same file later becomes agentic; its pre-agentic revision must remain.
    (workflow / "agent.yml").write_text(AGENTIC)
    commit(repo, "adopt claude")

    result = extract_repository(str(repo), config, repository="acme/demo")

    assert result.summary.n_workflow_files == 2
    assert result.summary.n_agentic_files == 1
    assert result.summary.agents == "claude"
    paths = {row["file_path"] for row in result.revisions}
    assert paths == {".github/workflows/agent.yml"}
    assert len(result.revisions) == 2, "pre-adoption revision must be retained"
    assert {row["agentic"] for row in result.revisions} == {True, False}
    assert all(row["repository"] == "acme/demo" for row in result.steps)


def test_keep_all_emits_non_agentic_workflows(repo, config):
    (repo / ".github" / "workflows" / "ci.yml").write_text(PLAIN)
    commit(repo, "add ci")
    result = extract_repository(str(repo), config, keep_all=True)
    assert len(result.revisions) == 1
    assert result.revisions[0]["agentic"] is False
    assert result.steps == []


def test_content_store_is_content_addressed(repo, config, tmp_path):
    (repo / ".github" / "workflows" / "agent.yml").write_text(AGENTIC)
    commit(repo, "add")
    store = tmp_path / "content"
    result = extract_repository(str(repo), config, content_dir=store)
    files = list(store.glob("*.yml"))
    assert len(files) == 1
    assert files[0].stem == result.revisions[0]["content_sha256"]


COPILOT_SETUP = textwrap.dedent(
    """
    name: Copilot setup steps
    on: workflow_dispatch
    jobs:
      copilot-setup-steps:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: npm ci
    """
)


def test_reserved_filename_detects_copilot_coding_agent(config):
    """Copilot is wired in by filename; there is no `uses:` or `run:` to match."""
    facts = analyse(
        config, COPILOT_SETUP, ".github/workflows/copilot-setup-steps.yml"
    )
    assert facts.strong_agents == ("copilot",)
    assert ("copilot", "path") in {(e.agent, e.kind) for e in facts.evidence}


def test_reserved_job_id_alone_is_corroborating_only(config):
    """GitHub only honours the reserved job inside the reserved file."""
    facts = analyse(config, COPILOT_SETUP, ".github/workflows/setup.yml")
    assert facts.strong_agents == ()
    assert not facts.is_agentic()
    assert facts.is_agentic(include_weak=True)
    assert ("copilot", "job") in {(e.agent, e.kind) for e in facts.evidence}


def test_path_evidence_survives_unparseable_yaml(config):
    facts = analyse(
        config, "steps: [ unclosed", ".github/workflows/copilot-setup-steps.yml"
    )
    assert not facts.parse_ok
    assert facts.strong_agents == ("copilot",)


def test_workflow_level_env_is_attributed_once_not_per_step(config):
    """A top-level credential must not mark every step an agent invocation."""
    workflow = textwrap.dedent(
        """
        name: CI
        on: [push]
        env:
          ANTHROPIC_API_KEY: ${{ secrets.A }}
        jobs:
          build:
            steps:
              - uses: actions/checkout@v4
              - run: npm run lint
              - uses: anthropics/claude-code-action@v1
        """
    )
    facts = analyse(config, workflow)
    env_rows = [e for e in facts.evidence if e.kind == "env"]
    assert len(env_rows) == 1
    assert env_rows[0].job_id is None and env_rows[0].step_index is None
    assert facts.n_steps == 3


def test_job_level_env_is_attributed_to_the_job(config):
    workflow = textwrap.dedent(
        """
        name: CI
        on: [push]
        jobs:
          build:
            env:
              ANTHROPIC_API_KEY: ${{ secrets.A }}
            steps:
              - run: a
              - run: b
        """
    )
    facts = analyse(config, workflow)
    env_rows = [e for e in facts.evidence if e.kind == "env"]
    assert len(env_rows) == 1
    assert env_rows[0].job_id == "build"
    assert env_rows[0].step_index is None


def test_summary_reports_blob_accounting(repo, config):
    (repo / ".github" / "workflows" / "agent.yml").write_text(AGENTIC)
    commit(repo, "add agent workflow")
    result = extract_repository(str(repo), config)
    summary = result.summary
    assert summary.n_blobs_requested == 1
    assert summary.n_blobs_read == 1
    assert summary.n_blobs_missing == 0
    assert summary.status == "ok"


def test_unreadable_blobs_are_reported_not_silently_dropped(repo, config, monkeypatch):
    """A blob that never arrives must not look like a non-agentic repository."""
    (repo / ".github" / "workflows" / "agent.yml").write_text(AGENTIC)
    commit(repo, "add agent workflow")

    monkeypatch.setattr("awe.extract.iter_blobs", lambda *a, **k: iter(()))
    result = extract_repository(str(repo), config)

    summary = result.summary
    assert summary.n_workflow_revisions == 1
    assert summary.n_blobs_requested == 1
    assert summary.n_blobs_read == 0
    assert summary.n_blobs_missing == 1
    assert summary.status == "incomplete"
    # Without the counters this row is indistinguishable from a clean miss.
    assert summary.n_agentic_files == 0


GHAW_LOCK = (
    '# gh-aw-metadata: {"schema_version":"v4","agent_id":"copilot",'
    '"strict":true,"engine_versions":{"copilot":"1.0.80"}}\n'
    '# gh-aw-manifest: {"version":1,"secrets":["GH_AW_GITHUB_TOKEN"],'
    '"actions":[{"repo":"actions/checkout","sha":"abc","version":"v7"}],'
    '"containers":[{"image":"ghcr.io/github/gh-aw-node"}],'
    '"mcp_servers":[{"name":"github","tools":["get_commit","list_issues"]},'
    '{"name":"safeoutputs","tools":["add_comment"]}]}\n'
    "# This file was automatically generated by gh-aw. DO NOT EDIT.\n"
    "name: Refiner\n"
    "on: pull_request\n"
    "jobs:\n"
    "  agent:\n"
    "    steps:\n"
    "      - uses: actions/checkout@abc\n"
)

GHAW_SOURCE = textwrap.dedent(
    """\
    ---
    on: pull_request
    engine: claude
    permissions:
      contents: read
    tools:
      github:
        mode: local
      bash: true
    safe-outputs:
      create-pull-request:
        draft: false
      add-comment:
        max: 1
    imports:
      - shared/base.md
      - uses: shared/policy.md
        with:
          min-integrity: approved
    sandbox:
      agent:
        runtime: cloud-hypervisor
    network:
      allowed: [example.org]
    evals:
      - id: did_it
        question: Did it work?
    timeout-minutes: 30
    ---

    # Refiner

    You are an automated code refinement system.
    """
)


def test_ghaw_lock_file_manifest_is_extracted(config):
    facts = analyse(config, GHAW_LOCK, ".github/workflows/refiner.lock.yml")
    assert facts.strong_agents == ("copilot",)
    assert facts.ghaw is not None
    assert facts.ghaw.kind == "lock"
    assert facts.ghaw.agent_id == "copilot"
    assert facts.ghaw.engine_version == "1.0.80"
    assert facts.ghaw.schema_version == "v4"
    assert facts.ghaw.strict is True
    assert facts.ghaw.secrets == ("GH_AW_GITHUB_TOKEN",)
    assert facts.ghaw.actions == ("actions/checkout",)
    assert facts.ghaw.mcp_tools == (
        "github:get_commit",
        "github:list_issues",
        "safeoutputs:add_comment",
    )


def test_ghaw_markdown_source_governance_is_extracted(config):
    facts = analyse(config, GHAW_SOURCE, ".github/workflows/refiner.md")
    assert facts.strong_agents == ("claude",)
    assert facts.triggers == ("pull_request",)
    ghaw = facts.ghaw
    assert ghaw is not None and ghaw.kind == "source"
    assert ghaw.tools == ("github", "bash")
    assert ghaw.safe_outputs == ("create-pull-request", "add-comment")
    assert ghaw.imports == ("shared/base.md", "shared/policy.md")
    assert ghaw.sandboxed is True
    assert ghaw.network == "custom"
    assert ghaw.n_evals == 1
    assert ghaw.timeout_minutes == 30


def test_ghaw_source_is_not_run_through_the_workflow_parser(config):
    """Markdown is prose with frontmatter; it has no jobs to walk."""
    facts = analyse(config, GHAW_SOURCE, ".github/workflows/refiner.md")
    assert facts.parse_ok
    assert facts.n_jobs == 0 and facts.n_steps == 0


def test_ordinary_markdown_is_not_a_ghaw_artifact(config):
    for path, body in (
        (".github/workflows/README.md", "# Notes\n"),
        (".github/workflows/x.md", "---\ntitle: hi\n---\nbody\n"),
        (".github/workflows/ci.yml", "name: CI\non: [push]\n"),
    ):
        assert analyse(config, body, path).ghaw is None


def test_ghaw_manifest_rows_reach_the_result(repo, config):
    workflows = repo / ".github" / "workflows"
    (workflows / "refiner.lock.yml").write_text(GHAW_LOCK)
    (workflows / "refiner.md").write_text(GHAW_SOURCE)
    commit(repo, "add agentic workflow")

    result = extract_repository(str(repo), config)
    kinds = {row["artifact_kind"] for row in result.manifests}
    assert kinds == {"lock", "source"}
    assert {row["agent_id"] for row in result.manifests} == {"copilot", "claude"}
    source_row = next(r for r in result.manifests if r["artifact_kind"] == "source")
    assert source_row["safe_outputs"] == "create-pull-request|add-comment"
    assert source_row["n_imports"] == 2


def test_many_blobs_do_not_deadlock_cat_file(repo, config):
    """Feeding cat-file stdin up front deadlocks once the id list exceeds a pipe.

    Small repositories hide this: the whole request fits in the 64 KB pipe
    buffer, git drains it, and nothing blocks. Only at scale do both processes
    stall on each other, at 0% CPU and with no error.
    """
    from awe.gitlog import iter_blobs

    workflows = repo / ".github" / "workflows"
    for index in range(400):
        (workflows / f"w{index}.yml").write_text(
            AGENTIC.replace("Claude Review", f"Review {index}")
        )
    commit(repo, "many workflows")

    revisions = list(iter_file_revisions(str(repo), config.pathspecs))
    hashes = [r.file_hash for r in revisions if r.file_hash]
    assert len(hashes) == 400

    got = dict(iter_blobs(str(repo), hashes))
    assert len(got) == 400
    assert all(text.startswith("\nname: Review") for text in got.values())


def test_prefetch_targets_only_absent_blobs(repo, config, monkeypatch):
    """Requesting already-local blobs is what made prefetch pathological."""
    from awe import extract as extract_mod

    workflows = repo / ".github" / "workflows"
    (workflows / "a.yml").write_text(AGENTIC)
    (workflows / "b.yml").write_text(PLAIN)
    commit(repo, "two workflows")

    requested: list[list[str]] = []
    monkeypatch.setattr(extract_mod, "is_partial_clone", lambda *a: True)
    monkeypatch.setattr(
        extract_mod, "missing_blobs", lambda repo_, hashes: list(hashes)[:1]
    )
    monkeypatch.setattr(
        extract_mod, "prefetch_blobs", lambda repo_, hashes: requested.append(hashes)
    )

    extract_repository(str(repo), config)
    assert requested == [requested[0]]
    assert len(requested[0]) == 1


def test_prefetch_skipped_entirely_when_nothing_is_absent(repo, config, monkeypatch):
    from awe import extract as extract_mod

    (repo / ".github" / "workflows" / "a.yml").write_text(AGENTIC)
    commit(repo, "one workflow")

    called = []
    monkeypatch.setattr(extract_mod, "is_partial_clone", lambda *a: True)
    monkeypatch.setattr(extract_mod, "missing_blobs", lambda *a: [])
    monkeypatch.setattr(
        extract_mod, "prefetch_blobs", lambda *a: called.append(1)
    )

    extract_repository(str(repo), config)
    assert called == []



GHAW_LOCK_V2 = (
    "#\n"
    "#    ___                   _   _\n"
    "#   / _ \\                 | | (_)\n"
    "#  | |_| | __ _  ___ _ __ | |_ _  ___\n"
    "#\n"
    "# This file was automatically generated by gh-aw (v0.56.2). DO NOT EDIT.\n"
    "#\n"
    "# For more information: https://github.github.com/gh-aw/\n"
    "#\n"
    '# gh-aw-metadata: {"schema_version":"v2","compiler_version":"v0.56.2","strict":true}\n'
    "\n"
    "name: Bot detection\n"
    "on: issues\n"
)


def test_older_lock_header_layout_is_recognised(config):
    """The banner comes first in earlier releases; a fixed window missed it."""
    facts = analyse(config, GHAW_LOCK_V2, ".github/workflows/bot-detection.lock.yml")
    assert facts.ghaw is not None
    assert facts.ghaw.kind == "lock"
    assert facts.ghaw.schema_version == "v2"
    assert facts.ghaw.compiler_version == "0.56.2"
    assert facts.ghaw.strict is True


def test_compiler_version_is_normalised_across_layouts(config):
    """v2 metadata says "v0.56.2"; the v4 marker says "0.44.0"."""
    marker_only = (
        "# This file was automatically generated by gh-aw (v0.44.0). DO NOT EDIT.\n"
        "name: x\n"
    )
    facts = analyse(config, marker_only, ".github/workflows/a.lock.yml")
    assert facts.ghaw is not None
    assert facts.ghaw.compiler_version == "0.44.0"
    assert facts.ghaw.schema_version is None
    assert facts.strong_agents == ("unknown",)


def test_excluded_repositories_are_recorded_but_not_mined(repo, config, tmp_path):
    """The framework's own repo is not an adopter and would swamp aggregates."""
    (repo / ".github" / "workflows" / "agent.yml").write_text(AGENTIC)
    commit(repo, "add agent workflow")

    result = extract_repository(str(repo), config, repository="github/gh-aw")
    assert result.summary.status == "excluded"
    assert result.revisions == [] and result.steps == [] and result.manifests == []
    # Still present in the denominator, and the choice is visible.
    assert result.summary.repository == "github/gh-aw"

    included = extract_repository(str(repo), config, repository="some/adopter")
    assert included.summary.status == "ok"
    assert included.summary.n_agentic_files == 1


def test_cli_records_an_excluded_repository_without_a_clone(tmp_path):
    """mine.sh relies on this to skip cloning github/gh-aw altogether."""
    import csv

    from awe.cli import DEFAULT_CONFIG, main

    out = tmp_path / "out"
    missing_clone = tmp_path / "never-cloned"
    code = main([
        "--repository", "github/gh-aw", str(missing_clone),
        "-o", str(out), "-c", str(DEFAULT_CONFIG), "--quiet",
    ])
    assert code == 0
    assert not missing_clone.exists()
    with (out / "repositories.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["repository"], r["status"]) for r in rows] == [("github/gh-aw", "excluded")]


def test_empty_shard_left_by_a_killed_run_still_gets_a_header(tmp_path):
    """A timeout can kill awe after it creates a shard but before the header
    is flushed; the next process reusing that PID directory appends to it."""
    import csv

    from awe.cli import DEFAULT_CONFIG, main

    out = tmp_path / "out"
    out.mkdir()
    (out / "repositories.csv").touch()
    code = main([
        "--repository", "github/gh-aw", str(tmp_path / "never-cloned"),
        "-o", str(out), "-c", str(DEFAULT_CONFIG), "--quiet",
    ])
    assert code == 0
    with (out / "repositories.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r["repository"] for r in rows] == ["github/gh-aw"]


def test_cli_rejects_repository_name_for_several_targets(tmp_path):
    from awe.cli import main

    assert main(["--repository", "a/b", "x", "y", "--quiet"]) == 2


def test_exclusion_matching_is_case_insensitive_and_exact(config):
    assert config.is_excluded("github/gh-aw")
    assert config.is_excluded("GitHub/GH-AW")
    assert not config.is_excluded("someone/gh-aw-fork")
    assert not config.is_excluded("github/gh-aw-extras")


def test_steps_are_keyed_by_invocation_not_by_evidence_item(config):
    """One step with two kinds of evidence is one invocation, not two."""
    from awe.detect import group_invocations

    workflow = textwrap.dedent(
        """
        name: Review
        on: [pull_request]
        jobs:
          review:
            steps:
              - uses: anthropics/claude-code-action@v1
                env:
                  ANTHROPIC_API_KEY: ${{ secrets.A }}
        """
    )
    facts = analyse(config, workflow)
    assert len(facts.evidence) == 2  # uses + env, same step

    groups = group_invocations(facts.evidence)
    assert len(groups) == 1
    strongest, members = groups[0]
    assert strongest.kind == "uses"  # the more decisive kind represents it
    assert {m.kind for m in members} == {"uses", "env"}


def test_file_scoped_evidence_collapses_to_one_row(repo, config):
    """path and job evidence describe the file, not two separate invocations."""
    (repo / ".github" / "workflows" / "copilot-setup-steps.yml").write_text(
        COPILOT_SETUP
    )
    commit(repo, "add copilot setup")

    result = extract_repository(str(repo), config)
    assert len(result.steps) == 1
    row = result.steps[0]
    assert row["scope"] == "file"
    assert row["evidence_kind"] == "path"
    assert row["evidence_kinds"] == "path|job"
    assert row["n_evidence"] == 2


def test_distinct_steps_stay_distinct(repo, config):
    workflow = textwrap.dedent(
        """
        name: Two agents
        on: [push]
        jobs:
          build:
            steps:
              - uses: anthropics/claude-code-action@v1
              - uses: openai/codex-action@v1
              - run: make test
        """
    )
    (repo / ".github" / "workflows" / "two.yml").write_text(workflow)
    commit(repo, "two agent steps")

    result = extract_repository(str(repo), config)
    assert len(result.steps) == 2
    assert {r["agent"] for r in result.steps} == {"claude", "codex"}
    assert all(r["scope"] == "step" for r in result.steps)
    assert {r["step_index"] for r in result.steps} == {0, 1}


def test_same_step_index_in_different_jobs_stays_distinct(repo, config):
    """Step indices restart per job, so they cannot key an invocation alone."""
    workflow = textwrap.dedent(
        """
        name: Review and triage
        on: [pull_request]
        jobs:
          review:
            steps:
              - uses: anthropics/claude-code-action@v1
          triage:
            steps:
              - uses: anthropics/claude-code-action@v1
          shared:
            uses: anthropics/claude-code-action/.github/workflows/x.yml@v1
          shared_again:
            uses: anthropics/claude-code-action/.github/workflows/x.yml@v1
        """
    )
    (repo / ".github" / "workflows" / "jobs.yml").write_text(workflow)
    commit(repo, "same step index in several jobs")

    result = extract_repository(str(repo), config)
    keys = {(r["job_id"], r["step_index"]) for r in result.steps}
    assert keys == {("review", 0), ("triage", 0), ("shared", -1), ("shared_again", -1)}
    assert len(result.steps) == 4


# --- catalogue matching and merging -------------------------------------------


def test_uses_patterns_match_the_exact_action_only():
    from awe.config import AgentDetector

    agent = AgentDetector("x", "X", {"uses": ("chinmaymk/ra",)})
    assert agent.match("uses", "chinmaymk/ra@v1") == "chinmaymk/ra"
    assert agent.match("uses", "ChinmayMK/RA@main") == "chinmaymk/ra"
    assert agent.match("uses", "chinmaymk/ra/sub/dir@v2") == "chinmaymk/ra"
    assert agent.match("uses", "chinmaymk/ra/.github/workflows/w.yml@v1")
    # A longer repository name, or the same name under another owner, is not it.
    assert agent.match("uses", "chinmaymk/ra-anything@v1") is None
    assert agent.match("uses", "xchinmaymk/ra@v1") is None
    assert agent.match("uses", "some-chinmaymk/ra@v1") is None


def test_prefix_patterns_no_longer_capture_each_other():
    from awe.config import AgentDetector

    agent = AgentDetector("x", "X", {"uses": ("MateSousa/create-release",)})
    assert agent.match("uses", "MateSousa/create-release-bot@v1") is None


def test_run_patterns_still_match_as_substrings():
    from awe.config import AgentDetector

    agent = AgentDetector("x", "X", {"run": ("claude -p",)})
    assert agent.match("run", 'npx claude -p "review this"') == "claude -p"


def _toml(path, text):
    path.write_text(textwrap.dedent(text))
    return path


def test_include_unions_patterns_and_applies_aliases_and_overrides(tmp_path):
    _toml(tmp_path / "base.toml", """
        [paths]
        github_actions = [".github/workflows/*.yml"]
        [agents.pr_agent]
        display_name = "PR-Agent"
        uses = ["Codium-ai/pr-agent"]
        run = ["pr-agent"]
        """)
    _toml(tmp_path / "gen.toml", """
        [agents.qodo]
        display_name = "qodo"
        uses = ["qodo-ai/pr-agent-action", "other/thing-pr-agent"]
        [agents.generic_ai]
        uses = ["some/ai-action"]
        """)
    top = _toml(tmp_path / "top.toml", """
        include = ["base.toml", "gen.toml"]
        [aliases]
        qodo = "pr_agent"
        [overrides]
        "other/thing-pr-agent" = "generic_ai"
        """)
    config = load_config(top)
    agents = {a.key: a for a in config.agents}
    assert set(agents) == {"pr_agent", "generic_ai"}
    assert agents["pr_agent"].display_name == "PR-Agent"
    assert agents["pr_agent"].patterns["uses"] == ("Codium-ai/pr-agent", "qodo-ai/pr-agent-action")
    assert agents["pr_agent"].patterns["run"] == ("pr-agent",)
    assert "other/thing-pr-agent" in agents["generic_ai"].patterns["uses"]


def test_include_cycles_and_dangling_overrides_are_rejected(tmp_path):
    from awe.config import ConfigError

    _toml(tmp_path / "a.toml", 'include = ["b.toml"]\n')
    _toml(tmp_path / "b.toml", 'include = ["a.toml"]\n')
    with pytest.raises(ConfigError, match="cycle"):
        load_config(tmp_path / "a.toml")

    _toml(tmp_path / "base.toml", """
        [paths]
        g = [".github/workflows/*.yml"]
        [agents.claude]
        uses = ["anthropics/claude-code-action"]
        """)
    _toml(tmp_path / "bad.toml", """
        include = ["base.toml"]
        [overrides]
        "no/such-action" = "claude"
        """)
    with pytest.raises(ConfigError, match="matches no uses pattern"):
        load_config(tmp_path / "bad.toml")


def test_frozen_catalogue_keeps_hand_written_patterns_and_paths():
    root = Path(__file__).resolve().parents[1] / "config"
    merged = load_config(root / "catalogue.toml")
    default = load_config(root / "default.toml")
    agents = {a.key: a for a in merged.agents}
    assert merged.pathspecs == default.pathspecs
    assert merged.exclude == default.exclude
    # Every hand-written pattern survives the merge.
    for agent in default.agents:
        for kind, patterns in agent.patterns.items():
            assert set(patterns) <= set(agents[agent.key].patterns.get(kind, ()))
    assert "qodo" not in agents
    assert agents["pr_agent"].patterns["uses"] == ("Codium-ai/pr-agent",)
    assert "codota/tabnine-pr-agent" in agents["generic_ai"].patterns["uses"]


def test_content_store_holds_only_emitted_histories(repo, config, tmp_path):
    """Non-agentic workflows are read but not stored, unless --keep-all."""
    (repo / ".github" / "workflows" / "agent.yml").write_text(AGENTIC)
    (repo / ".github" / "workflows" / "build.yml").write_text(
        "on: push\njobs:\n  b:\n    steps:\n      - run: make\n"
    )
    commit(repo, "agentic and plain workflows")

    store = tmp_path / "content"
    result = extract_repository(str(repo), config, content_dir=store)
    stored = {f.stem for f in store.glob("*.yml")}
    assert stored == {r["content_sha256"] for r in result.revisions}
    assert len(stored) == 1
    assert not list(store.glob(".*.tmp"))

    everything = tmp_path / "all"
    extract_repository(str(repo), config, content_dir=everything, keep_all=True)
    assert len(list(everything.glob("*.yml"))) == 2
