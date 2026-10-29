"""Content-based detection of coding-agent invocations in CI workflows.

Path-based detection is not an option here: an agentic workflow is an ordinary
`.github/workflows/*.yml` file. Detection therefore parses the workflow, walks
its jobs and steps, and matches each step against the agent catalogue. When a
revision does not parse as YAML (which happens routinely mid-history), we fall
back to a whole-file text scan and flag the revision so downstream users can
exclude it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .config import STRONG_EVIDENCE, Config
from .ghaw import GhawFacts
from .ghaw import analyse as analyse_ghaw

_GATE_MARKERS = (
    "github.actor",
    "github.event.sender",
    "github.event.comment.body",
    "github.triggering_actor",
    "contains(github.event",
    "association",
)


@dataclass
class Evidence:
    agent: str
    kind: str
    pattern: str
    job_id: str | None
    step_index: int | None
    step_name: str | None
    uses_ref: str | None


@dataclass
class WorkflowFacts:
    """Everything one revision of one workflow file tells us."""

    parse_ok: bool
    workflow_name: str | None = None
    triggers: tuple[str, ...] = ()
    n_jobs: int = 0
    n_steps: int = 0
    permissions: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    ghaw: GhawFacts | None = None

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(e.agent for e in self.evidence))

    @property
    def strong_agents(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                e.agent for e in self.evidence if e.kind in STRONG_EVIDENCE
            )
        )

    def is_agentic(self, include_weak: bool = False) -> bool:
        return bool(self.agents if include_weak else self.strong_agents)


def _as_text(value) -> str:
    """Flatten an arbitrary YAML subtree into searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return " ".join(f"{k} {_as_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(v) for v in value)
    return str(value)


def _normalise_triggers(on_value) -> tuple[str, ...]:
    if on_value is None:
        return ()
    if isinstance(on_value, str):
        return (on_value,)
    if isinstance(on_value, list):
        return tuple(str(v) for v in on_value)
    if isinstance(on_value, dict):
        return tuple(str(k) for k in on_value)
    return ()


def _env_keys(*scopes) -> list[str]:
    keys: list[str] = []
    for scope in scopes:
        if isinstance(scope, dict):
            keys.extend(str(k) for k in scope)
    return keys


def _scan_scope_env(
    config: Config, scope: dict | None, job_id: str | None
) -> list[Evidence]:
    """Credential evidence for a workflow- or job-level `env:` block.

    Attributed once, at the scope that declares it. Folding these keys into
    every step's haystack instead would emit one row per step in the workflow
    -- claiming `actions/checkout` is an agent invocation because the workflow
    happens to export ANTHROPIC_API_KEY at the top.
    """
    env_text = " ".join(_env_keys(scope))
    if not env_text:
        return []
    found: list[Evidence] = []
    for agent in config.agents:
        pattern = agent.match("env", env_text)
        if pattern:
            found.append(
                Evidence(
                    agent=agent.key,
                    kind="env",
                    pattern=pattern,
                    job_id=job_id,
                    step_index=None,
                    step_name=None,
                    uses_ref=None,
                )
            )
    return found


def _scan_path(config: Config, file_path: str) -> list[Evidence]:
    """Evidence carried by the workflow file's own name."""
    found: list[Evidence] = []
    for agent in config.agents:
        pattern = agent.match("path", file_path)
        if pattern:
            found.append(
                Evidence(
                    agent=agent.key,
                    kind="path",
                    pattern=pattern,
                    job_id=None,
                    step_index=None,
                    step_name=None,
                    uses_ref=None,
                )
            )
    return found


def _scan_job_id(config: Config, job_id: str) -> list[Evidence]:
    """Evidence carried by a reserved job identifier."""
    found: list[Evidence] = []
    for agent in config.agents:
        pattern = agent.match("job", job_id)
        if pattern:
            found.append(
                Evidence(
                    agent=agent.key,
                    kind="job",
                    pattern=pattern,
                    job_id=job_id,
                    step_index=None,
                    step_name=None,
                    uses_ref=None,
                )
            )
    return found


def _scan_step(
    config: Config,
    job_id: str,
    index: int,
    step: dict,
) -> list[Evidence]:
    uses_ref = step.get("uses") if isinstance(step.get("uses"), str) else None
    run_text = step.get("run") if isinstance(step.get("run"), str) else ""
    step_name = step.get("name") if isinstance(step.get("name"), str) else None
    with_text = _as_text(step.get("with"))
    env_text = " ".join(_env_keys(step.get("env")))
    haystacks = {
        "uses": uses_ref or "",
        "run": run_text,
        "url": f"{run_text} {with_text}",
        "env": env_text,
    }

    found: list[Evidence] = []
    for agent in config.agents:
        for kind, haystack in haystacks.items():
            pattern = agent.match(kind, haystack)
            if pattern:
                found.append(
                    Evidence(
                        agent=agent.key,
                        kind=kind,
                        pattern=pattern,
                        job_id=job_id,
                        step_index=index,
                        step_name=step_name,
                        uses_ref=uses_ref,
                    )
                )
    return found


def _scan_text(config: Config, text: str) -> list[Evidence]:
    """Degraded scan for revisions that do not parse as YAML.

    Without structure we cannot tell which step an occurrence belongs to, and
    patterns overlap across kinds (the substring `claude-code` occurs inside
    `anthropics/claude-code-action`). We therefore keep only the single
    highest-confidence kind per agent, rather than every match.
    """
    found: list[Evidence] = []
    for agent in config.agents:
        for kind in ("uses", "run", "url", "env"):
            pattern = agent.match(kind, text)
            if pattern:
                found.append(
                    Evidence(
                        agent=agent.key,
                        kind=kind,
                        pattern=pattern,
                        job_id=None,
                        step_index=None,
                        step_name=None,
                        uses_ref=None,
                    )
                )
                break
    return found


def _ghaw_evidence(facts: GhawFacts) -> list[Evidence]:
    """One evidence item naming the engine the framework declares."""
    return [
        Evidence(
            agent=facts.agent_id or "unknown",
            kind="ghaw",
            pattern=f"gh-aw:{facts.kind}",
            job_id=None,
            step_index=None,
            step_name=None,
            uses_ref=None,
        )
    ]


def analyse(config: Config, content: str, file_path: str | None = None) -> WorkflowFacts:
    # Path evidence is independent of whether the body parses, so it is
    # collected first and survives the degraded branch below.
    path_evidence = _scan_path(config, file_path) if file_path else []

    # gh-aw stamps its own manifest, so this is exact rather than inferred.
    ghaw = analyse_ghaw(file_path, content) if file_path else None
    ghaw_evidence = _ghaw_evidence(ghaw) if ghaw else []

    if ghaw is not None and ghaw.kind == "source":
        # Authored Markdown: prose with frontmatter, no jobs or steps to walk.
        # Running it through the workflow parser would only mark it unparseable.
        triggers = _normalise_triggers(ghaw.extra.get("on"))
        return WorkflowFacts(
            parse_ok=ghaw.parse_ok,
            triggers=triggers,
            permissions=_as_text(ghaw.extra.get("permissions")) or None,
            evidence=[*path_evidence, *ghaw_evidence],
            ghaw=ghaw,
        )

    try:
        document = yaml.safe_load(content)
    # ValueError: an unquoted impossible date such as `2023-02-30` is resolved
    # as a timestamp and fails in the constructor, not the parser.
    except (yaml.YAMLError, RecursionError, ValueError):
        document = None

    if not isinstance(document, dict):
        return WorkflowFacts(
            parse_ok=False,
            evidence=[*path_evidence, *ghaw_evidence, *_scan_text(config, content)],
            ghaw=ghaw,
        )

    # `on:` is parsed as the boolean True by YAML 1.1 unless quoted.
    on_value = document.get("on", document.get(True))
    facts = WorkflowFacts(
        parse_ok=True,
        workflow_name=(
            document.get("name") if isinstance(document.get("name"), str) else None
        ),
        triggers=_normalise_triggers(on_value),
        permissions=_as_text(document.get("permissions")) or None,
    )

    facts.ghaw = ghaw
    facts.evidence += path_evidence
    facts.evidence += ghaw_evidence

    workflow_env = document.get("env") if isinstance(document.get("env"), dict) else None
    facts.evidence += _scan_scope_env(config, workflow_env, None)

    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return facts

    facts.n_jobs = len(jobs)
    for job_id, job in jobs.items():
        facts.evidence += _scan_job_id(config, str(job_id))
        if not isinstance(job, dict):
            continue
        job_env = job.get("env") if isinstance(job.get("env"), dict) else None
        facts.evidence += _scan_scope_env(config, job_env, str(job_id))

        # A job may call a reusable workflow instead of declaring steps.
        reusable = job.get("uses")
        if isinstance(reusable, str):
            facts.evidence += _scan_step(config, str(job_id), -1, {"uses": reusable})

        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            facts.n_steps += 1
            facts.evidence += _scan_step(config, str(job_id), index, step)

    return facts


# Evidence kinds ordered by how decisive they are, most first. `ghaw` leads
# because the framework stamps its own manifest -- it is not inferred at all.
KIND_RANK = ("ghaw", "uses", "run", "path", "job", "url", "env")


def _rank(kind: str) -> int:
    return KIND_RANK.index(kind) if kind in KIND_RANK else len(KIND_RANK)


def group_invocations(evidence: list[Evidence]) -> list[tuple[Evidence, list[Evidence]]]:
    """Collapse evidence items into one entry per agent invocation.

    A single step that both `uses:` an agent action and binds that agent's
    credential yields two evidence items for one invocation, and counting rows
    would double it. Items are therefore grouped by (agent, scope), where scope
    is the (job, step index) pair for step-level evidence and the file itself
    for evidence that is not attached to a step -- a reserved filename, a
    reserved job id, a workflow-level credential. Step indices restart in every
    job, so the job is part of the key: step 0 of `review` and step 0 of
    `triage` are two invocations. Each group is represented by its most
    decisive item, with the rest retained so nothing is discarded.
    """
    groups: dict[tuple[str, object], list[Evidence]] = {}
    for item in evidence:
        scope: object = (
            (item.job_id, item.step_index) if item.step_index is not None else "file"
        )
        groups.setdefault((item.agent, scope), []).append(item)

    collapsed: list[tuple[Evidence, list[Evidence]]] = []
    for members in groups.values():
        members.sort(key=lambda e: _rank(e.kind))
        collapsed.append((members[0], members))
    return collapsed


def classify_tasks(config: Config, facts: WorkflowFacts, evidence: Evidence) -> tuple[str, ...]:
    """Assign purpose labels to one agent step."""
    name_blob = " ".join(
        part
        for part in (facts.workflow_name, evidence.job_id, evidence.step_name)
        if part
    ).lower()
    labels: list[str] = []
    for task in config.tasks:
        if any(hint.lower() in name_blob for hint in task.name_hints):
            labels.append(task.key)
            continue
        if any(trigger in facts.triggers for trigger in task.triggers):
            labels.append(task.key)
    return tuple(dict.fromkeys(labels))


def is_gated(content: str) -> bool:
    """Whether the workflow conditions execution on who triggered it."""
    lowered = content.lower()
    return any(marker in lowered for marker in _GATE_MARKERS)
