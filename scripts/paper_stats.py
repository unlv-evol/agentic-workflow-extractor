#!/usr/bin/env python3
"""Compute the paper's dataset figures and draw its plots from a mining run.

    python3 scripts/paper_stats.py out/full
    python3 scripts/paper_stats.py out/trial-500 --trial "500-repository dry run"

Reads the merged CSVs that scripts/mine.sh writes and produces:

* manuscripts/shared/numbers-dataset.tex -- one \\def per dataset macro declared
  in numbers-defaults.tex (counts, last-snapshot shares, dates);
* manuscripts/shared/figures/{wiring,constraints,abandonment}.pdf -- Figs. 1-3;
* manuscripts/shared/table-agents.tex -- the body of the per-agent table.

Headline figures count **named agents only**. The generic_ai group of
unattributed Marketplace actions is counted apart (\\nGenericRepos) unless
validation folds it in (validation/PLAN.md, C7); pass --include-generic then.

With --trial (a dry run) or --provisional (the real run, retries still
pending), the macro file also defines \\datasetTrial, and the paper prints a red
banner saying the figures are provisional, so they cannot be submitted by
accident. Only a plain run removes it.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "manuscripts" / "shared"

STRONG = ("uses", "run", "path", "ghaw")
WEAK = ("job", "url", "env")
# Precedence when one revision carries several strong kinds (Dataset.wiring),
# and the names the paper gives each way of wiring an agent into CI.
WIRING = ("ghaw", "path", "uses", "run")
WIRING_LABEL = {"uses": "dedicated action", "run": "CLI in a shell step",
                "path": "reserved file name", "ghaw": "gh-aw"}
WIRING_SHORT = {"uses": "action", "run": "CLI", "path": "reserved", "ghaw": "gh-aw"}
GENERIC = "generic_ai"
# Nothing before ChatGPT's public release can be an LLM agent invocation; the
# same proxy GHAgentFiles uses. On the full run it removes only gh-aw source
# false positives: ordinary Markdown in .github/workflows/ whose frontmatter
# happens to carry a key such as `tools`.
CUTOFF = "2022-11-30"
DISPLAY = {
    "claude": "Claude", "codex": "Codex", "copilot": "Copilot", "gemini": "Gemini",
    "cursor": "Cursor", "aider": "Aider", "devin": "Devin", "pr_agent": "PR-Agent",
    "coderabbit": "CodeRabbit", "goose": "Goose", "opencode": "OpenCode",
    "openhands": "OpenHands", GENERIC: "generic",
    "unknown": "unspecified",   # gh-aw files whose header names no engine
}
CHANGE_TYPES = ("Added", "Modified", "Deleted", "Renamed")

# Reference palette (dataviz skill, references/palette.md), light mode: the
# first four categorical slots in fixed order, validated adjacent-safe; aqua
# and yellow sit below 3:1 on white, so every chart that uses them carries a
# legend or direct labels.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
OTHER = "#8a8984"
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    csv.field_size_limit(sys.maxsize)
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def day(stamp: str) -> date:
    return datetime.fromisoformat(stamp[:19]).date()


def instant(stamp: str) -> datetime:
    """A commit date as a comparable instant. git writes the committer's own
    offset (`...T10:00:00-07:00`), so comparing the strings misorders commits
    made in different time zones."""
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def count(n: int) -> str:
    return f"{n:,}".replace(",", "{,}")


def kplus(n: int) -> str:
    """142K+ style for the abstract; exact below a thousand."""
    return f"{n // 1000}K+" if n >= 1000 else str(n)


def ipct(part: int, whole: int) -> str:
    """A whole percentage, for table cells."""
    return f"{round(100 * part / whole)}" if whole else "0"


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}" if whole else "0.0"


def run_dates(out: Path) -> tuple[date, date]:
    """First and last day of mining: RUN.txt if present, else file times."""
    stamps = []
    run = out / "RUN.txt"
    if run.exists():
        stamps = [day(m) for m in re.findall(r"^== (\S+)", run.read_text(), re.M)]
    parts = list(out.glob("parts/*/repositories.csv")) or [out / "repositories.csv"]
    mtimes = [date.fromtimestamp(p.stat().st_mtime) for p in parts if p.exists()]
    first = min(stamps) if stamps else min(mtimes)
    return first, max(mtimes + stamps)


# ---------------------------------------------------------------------------


class Dataset:
    """The run's tables, reduced to the named-agent view the paper reports."""

    def __init__(self, out: Path, include_generic: bool):
        self.repos = read(out / "repositories.csv")
        self.revisions = read(out / "revisions.csv")
        self.steps = read(out / "steps.csv")
        self.manifests = read(out / "ghaw_manifests.csv")
        if not self.repos:
            raise SystemExit(f"{out}: no repositories.csv; has the run merged its shards?")

        def counted(agent: str) -> bool:
            return include_generic or agent != GENERIC

        # Named agents with strong evidence, per revision, and the strong
        # evidence kinds behind them (how the agent is wired into the file).
        self.agents_at: dict[tuple, set[str]] = defaultdict(set)
        self.kinds_at: dict[tuple, set[str]] = defaultdict(set)
        self.generic_repos: set[str] = set()
        early = [s for s in self.steps if s["commit_date"][:10] < CUTOFF]
        self.steps = [s for s in self.steps if s["commit_date"][:10] >= CUTOFF]
        for s in self.steps:
            if s["evidence_kind"] not in STRONG:
                continue
            if counted(s["agent"]):
                key = (s["repository"], s["uid"], s["commit_hash"])
                self.agents_at[key].add(s["agent"])
                self.kinds_at[key].add(s["evidence_kind"])
            else:
                self.generic_repos.add(s["repository"])
        self.invocations = [s for s in self.steps if counted(s["agent"])]

        # Each history in chronological order. git log lists commits newest
        # first, so among revisions with the same instant, a later position in
        # the file is the older commit.
        self.histories: dict[tuple, list[dict]] = defaultdict(list)
        for position, r in enumerate(self.revisions):
            r["_order"] = (instant(r["commit_date"]), -position)
            self.histories[(r["repository"], r["uid"])].append(r)
        for revs in self.histories.values():
            revs.sort(key=lambda r: r["_order"])
        self.agentic_histories = {
            key for key, revs in self.histories.items()
            if any((key[0], key[1], r["commit_hash"]) in self.agents_at for r in revs)
        }
        self.generic_repos -= {key[0] for key in self.agentic_histories}
        # What the cutoff removed from the reported figures: invocations of the
        # counted agents, and histories that were agentic only through them.
        early = [s for s in early if counted(s["agent"])]
        self.pre_cutoff = len(early)
        self.pre_cutoff_histories = len(
            {(s["repository"], s["uid"]) for s in early if s["evidence_kind"] in STRONG}
            - self.agentic_histories
        )

        # Last snapshot: the latest revision of each history, unless deleted.
        self.latest: dict[tuple, dict] = {}
        for key in self.agentic_histories:
            last = self.histories[key][-1]
            if last["git_change_type"] != "Deleted":
                self.latest[key] = last

    def agents(self, row: dict) -> set[str]:
        return self.agents_at.get((row["repository"], row["uid"], row["commit_hash"]), set())

    def wiring(self, row: dict) -> str | None:
        """How the agent is wired into an agentic revision, by precedence.

        A gh-aw file and a reserved file name say more about the deployment
        than the steps they contain, so they win over `uses` and `run`.
        """
        kinds = self.kinds_at.get((row["repository"], row["uid"], row["commit_hash"]), set())
        return next((k for k in WIRING if k in kinds), None)

    def adoptions(self) -> list[tuple[date, str, set[str]]]:
        """For each agentic history: when it first invoked an agent, how, and which."""
        found = []
        for key in self.agentic_histories:
            for r in self.histories[key]:
                if self.agents(r):
                    found.append((day(r["commit_date"]), self.wiring(r), self.agents(r)))
                    break
        return found

    def lifetimes(self, end: date) -> list[tuple[int, bool, set[str]]]:
        """Per agentic repository: days from adoption to abandonment, whether it
        was abandoned, and the agents it adopted first.

        A repository is agentic while at least one of its workflow files invokes
        an agent. It has abandoned agentic CI only if none does at its last
        commit; the abandonment date is when the last one stopped. Counting
        repositories rather than files keeps replacements out: a workflow
        deleted in favour of a new one (454 in the same commit, 876 more within
        a week, on the full run) is not an abandonment. Repositories still
        agentic are censored at `end`.
        """
        events: dict[str, list] = defaultdict(list)
        for key in self.agentic_histories:
            state = False
            for r in self.histories[key]:
                on = bool(self.agents(r)) and r["git_change_type"] != "Deleted"
                if on != state:
                    events[key[0]].append((r["_order"][0], 1 if on else -1, self.agents(r)))
                    state = on
        result = []
        for repo, evs in events.items():
            # At equal instants, switch on before off: a file replaced in one
            # commit must not count as a moment without any agent.
            evs.sort(key=lambda e: (e[0], -e[1]))
            alive, start, stopped = 0, None, None
            for when, delta, _ in evs:
                alive += delta
                if start is None and alive > 0:
                    start = when
                if alive == 0:
                    stopped = when
            if start is None:
                continue
            # Every agent switched on at the starting instant, not whichever
            # file the sort happened to put first: several files are often
            # added in one commit.
            first = set().union(*(a for when, delta, a in evs if when == start and delta > 0))
            # Adoptions on the clone days themselves get zero follow-up, never less.
            if alive > 0:
                result.append((max(0, (end - start.date()).days), False, first))
            else:
                result.append((max(0, (stopped.date() - start.date()).days), True, first))
        return result

    def rows_in_histories(self) -> list[dict]:
        return [r for key in self.agentic_histories for r in self.histories[key]]


def macros(d: Dataset, out: Path, trial: str | None) -> dict[str, str]:
    m: dict[str, str] = {}
    status = Counter(r["status"] for r in d.repos)
    mined = status["ok"] + status["incomplete"]
    m["nMinedRepos"] = count(mined)
    m["nIncompleteRepos"] = count(status["incomplete"])
    m["nErrorRepos"] = count(sum(n for s, n in status.items() if s not in ("ok", "incomplete", "excluded")))
    m["nExcludedRepos"] = count(status["excluded"])

    first_run, last_run = run_dates(out)
    m["cloneStartDate"] = long_date(first_run)
    m["cloneEndDate"] = long_date(last_run)
    m["snapshotDate"] = long_date(last_run)

    rows = d.rows_in_histories()
    agentic_rows = [r for r in rows if d.agents(r)]
    repos = {key[0] for key in d.agentic_histories}
    ghaw = {m_["repository"] for m_ in d.manifests}
    m["nAgenticRepos"] = count(len(repos))
    m["pctAgenticRepos"] = pct(len(repos), mined)
    m["nGhawRepos"] = count(len(ghaw & repos))
    m["pctGhawRepos"] = pct(len(ghaw & repos), mined)
    m["nGenericRepos"] = count(len(d.generic_repos))
    m["nHistories"] = count(len(d.agentic_histories))
    m["nRevisions"] = count(len(rows))
    m["nCommits"] = count(len({(r["repository"], r["commit_hash"]) for r in rows}))
    m["nAgenticRevisions"] = count(len(agentic_rows))
    m["nSymlinks"] = count(sum(r["symbolic_link"] == "True" for r in rows))
    m["nGhawManifests"] = count(len(d.manifests))
    m["nPreRelease"] = count(d.pre_cutoff)
    m["nPreReleaseHistories"] = count(d.pre_cutoff_histories)
    if agentic_rows:
        dates = sorted(day(r["commit_date"]) for r in agentic_rows)
        m["firstInvocationDate"] = long_date(dates[0])
        m["lastInvocationDate"] = long_date(dates[-1])

    kinds = Counter(s["evidence_kind"] for s in d.invocations)
    m["nInvocations"] = count(len(d.invocations))
    m["nInvUses"], m["nInvRun"] = count(kinds["uses"]), count(kinds["run"])
    m["nInvPath"], m["nInvGhaw"] = count(kinds["path"]), count(kinds["ghaw"])
    m["nInvWeak"] = count(sum(kinds[k] for k in WEAK))
    # Table I's count column: every kind on its own.
    m["nInvJob"], m["nInvUrl"], m["nInvEnv"] = (count(kinds[k]) for k in WEAK)

    # Agentic workflows keep being revised once introduced.
    if d.agentic_histories:
        sizes = sorted(len(d.histories[key]) for key in d.agentic_histories)
        m["meanRevisionsPerHistory"] = f"{sum(sizes) / len(sizes):.1f}"
        m["medianRevisionsPerHistory"] = f"{sizes[len(sizes) // 2]}"
        changes = Counter(r["git_change_type"] for r in rows)
        m["nRevModified"] = count(changes["Modified"])
        m["nRevAdded"] = count(changes["Added"])

    # gh-aw lock headers name their engine only from some compiler release on;
    # older lock files make up the "unnamed" row of Table III.
    named = [x["compiler_version"] for x in d.manifests
             if x["artifact_kind"] != "source" and x["agent_id"] and x["compiler_version"]]
    if named:
        oldest = min(named, key=lambda v: tuple(int(p) for p in re.findall(r"\d+", v)))
        m["ghawEngineSince"] = oldest

    for name, n in (("nAgenticRepos", len(repos)), ("nHistories", len(d.agentic_histories)),
                    ("nRevisions", len(rows)), ("nInvocations", len(d.invocations))):
        m[f"{name}K"] = kplus(n)

    # Last-snapshot column, each as a share of its all-snapshots figure.
    latest = list(d.latest.values())
    latest_keys = {(r["repository"], r["uid"], r["commit_hash"]) for r in latest}
    inv_last = [s for s in d.invocations
                if (s["repository"], s["uid"], s["commit_hash"]) in latest_keys]
    kinds_last = Counter(s["evidence_kind"] for s in inv_last)
    repos_last = {r["repository"] for r in latest if d.agents(r)}
    pairs = {
        "AgenticRepos": (len(repos_last), len(repos)),
        "GhawRepos": (len(repos_last & ghaw), len(ghaw & repos)),
        "Histories": (len(latest), len(d.agentic_histories)),
        "Commits": (len({(r["repository"], r["commit_hash"]) for r in latest}),
                    len({(r["repository"], r["commit_hash"]) for r in rows})),
        "Revisions": (len(latest), len(rows)),
        "AgenticRevisions": (sum(1 for r in latest if d.agents(r)), len(agentic_rows)),
        "Invocations": (len(inv_last), len(d.invocations)),
        "InvUses": (kinds_last["uses"], kinds["uses"]),
        "InvRun": (kinds_last["run"], kinds["run"]),
        "InvPath": (kinds_last["path"], kinds["path"]),
        "InvGhaw": (kinds_last["ghaw"], kinds["ghaw"]),
    }
    for name, (last, total) in pairs.items():
        m[f"n{name}Last"] = count(last)
        m[f"pct{name}Last"] = pct(last, total)

    # Adoption in the GHAgentFiles-comparable subset (created >= a year before
    # the query) against repositories created since: the frame's flag column.
    frame = ROOT / "data" / "seart" / "frame-2026-10-03.csv"
    if frame.exists():
        comparable = {r["name"]: r["ghagentfiles_comparable"] == "true" for r in read(frame)}
        ok = {r["repository"] for r in d.repos if r["status"] in ("ok", "incomplete")}
        for label, flag in (("Comparable", True), ("Recent", False)):
            group = {r for r in ok if comparable.get(r) is flag}
            if not group:
                continue  # e.g. a dry run drawn from one subset: leave it [??]
            adopters = group & repos
            m[f"nMined{label}"] = count(len(group))
            m[f"nAgenticRepos{label}"] = count(len(adopters))
            m[f"pctAgenticRepos{label}"] = pct(len(adopters), len(group))

    m["plotStart"] = plot_start(d, last_run).strftime("%B %Y")
    m.update(analysis(d, first_run))

    if trial:
        m["datasetTrial"] = trial
        # The frame's size (numbers.tex) beside a dry run's counts would read
        # as "13 of 172K+"; a placeholder should at least be self-consistent.
        m["nCandidateRepos"] = count(len(d.repos))
        m["nCandidateReposK"] = kplus(len(d.repos))
    return m


def _names(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`, in sans serif as the paper sets names."""
    items = [f"\\code{{{i.replace('_', '\\_')}}}" for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def analysis(d: Dataset, first_run: date) -> dict[str, str]:
    """Section V figures, all measured on the last snapshot: what the
    workflows that invoke an agent today look like."""
    m: dict[str, str] = {}
    latest = [r for r in d.latest.values() if d.agents(r)]
    n = len(latest)
    m["nLatestAgentic"] = count(n)
    if not n:
        return m

    def share(pred) -> str:
        return pct(sum(1 for r in latest if pred(r)), n)

    def triggered(r, names) -> bool:
        return bool(set(names) & set((r.get("triggers") or "").split("|")))

    m["pctTrigDispatch"] = share(lambda r: triggered(r, ["workflow_dispatch"]))
    m["pctTrigPR"] = share(lambda r: triggered(r, PR_EVENTS))
    m["pctTrigIssues"] = share(lambda r: triggered(r, ISSUE_EVENTS))
    m["pctTrigSchedule"] = share(lambda r: triggered(r, ["schedule"]))
    m["pctPermissions"] = share(lambda r: (r.get("permissions") or "").strip())
    m["pctGated"] = share(lambda r: r.get("actor_gated") == "True")

    keys = {(r["repository"], r["uid"], r["commit_hash"]) for r in latest}
    invocations = [s for s in d.invocations
                   if (s["repository"], s["uid"], s["commit_hash"]) in keys]
    uses = [s for s in invocations if s["evidence_kind"] == "uses" and s["action"]]
    sha = sum(1 for s in uses if re.fullmatch(r"[0-9a-f]{40}", s.get("action_version") or ""))
    m["nUsesLatest"] = count(len(uses))
    m["pctShaPinned"] = pct(sha, len(uses))
    tasks = Counter(t for s in invocations
                    for t in set(filter(None, (s.get("tasks") or "").split("|"))))
    for label, task in (("Review", "review"), ("Triage", "triage"), ("Implement", "implement")):
        m[f"pctTask{label}"] = pct(tasks[task], len(invocations))

    # Agents by repositories invoking them at the last snapshot.
    repos_by_agent: dict[str, set[str]] = defaultdict(set)
    for r in latest:
        for agent in d.agents(r):
            repos_by_agent[agent].add(r["repository"])
    # gh-aw files that name no engine are certain invocations of an unknown
    # agent: counted everywhere, but not ranked as an agent in the prose.
    ranked = sorted((a for a in repos_by_agent if a != "unknown"),
                    key=lambda a: (-len(repos_by_agent[a]), a))
    for slot, agent in zip("ABC", ranked):
        m[f"agent{slot}"] = DISPLAY.get(agent, agent)
        m[f"nAgentRepos{slot}"] = count(len(repos_by_agent[agent]))

    # gh-aw sources at the last snapshot: the constraints maintainers declare.
    sources = [x for x in d.manifests
               if x["artifact_kind"] == "source"
               and (x["repository"], x["uid"], x["commit_hash"]) in keys]
    m["nGhawSourcesLast"] = count(len(sources))
    if sources:
        k = len(sources)
        m["pctGhawSandbox"] = pct(sum(x["sandboxed"] == "True" for x in sources), k)
        m["pctGhawNetwork"] = pct(sum(x["network"] == "custom" for x in sources), k)
        m["pctGhawStrict"] = pct(sum(x["strict"] == "True" for x in sources), k)
        # `noop` is gh-aw's explicit "do nothing" output, not a capability.
        outputs = Counter(o for x in sources for o in filter(None, x["safe_outputs"].split("|"))
                          if o != "noop")
        m["ghawTopOutputs"] = _names([o for o, _ in outputs.most_common(3)])
        m["pctGhawTopOutput"] = pct(outputs.most_common(1)[0][1], k) if outputs else "0.0"

    # Fig. 1 in numbers: how new agentic workflows were wired in, first and
    # last complete quarter.
    quarters = wiring_by_quarter(d, first_run)
    if quarters:
        (q0, c0), (q1, c1) = quarters[0], quarters[-1]
        m["wiringFirstQuarter"], m["wiringLastQuarter"] = q0, q1
        for kind, label in (("uses", "Uses"), ("run", "Run"), ("path", "Path"), ("ghaw", "Ghaw")):
            m[f"pctWire{label}First"] = pct(c0[kind], sum(c0.values()))
            m[f"pctWire{label}Last"] = pct(c1[kind], sum(c1.values()))

    # Fig. 3 in numbers: abandonment of agentic CI by repositories.
    lives = d.lifetimes(first_run)
    if lives:
        m["nAbandonedRepos"] = count(sum(1 for _, gone, _ in lives if gone))
        m["pctAbandonedRepos"] = pct(sum(1 for _, gone, _ in lives if gone), len(lives))
        m["nSameDayAbandon"] = count(sum(1 for t, gone, _ in lives if gone and t == 0))
        curve = kaplan_meier([(t, gone) for t, gone, _ in lives])
        # TeX control sequences cannot contain digits: name the horizons.
        # Codex's curve in Fig. 3 stops early: how far it reaches, and how few
        # Codex repositories have a year of follow-up.
        codex = [(t, gone) for t, gone, agents in lives if "codex" in agents]
        if codex:
            curve = kaplan_meier(codex)
            reach = max((t for t, _, n in curve if n >= 50), default=0)
            m["codexReachMonths"] = f"{round(reach / (365.25 / 12))}"
            m["nCodexYear"] = count(sum(1 for t, _ in codex if t >= 365))
        # One, three and twelve months (of 30.44 days), as ticked in Fig. 3.
        for horizon, name in ((30, "Month"), (91, "Quarter"), (365, "Year")):
            m[f"pctAbandon{name}"] = f"{100 * (1 - survival_at(curve, horizon)):.0f}"
    m["pctTrigPRTarget"] = share(lambda r: triggered(r, ["pull_request_target"]))
    m["nTrigPRTarget"] = count(sum(1 for r in latest if triggered(r, ["pull_request_target"])))

    # Fig. 3 in numbers, from the same rows the figure draws.
    rows = {row["label"]: row for row in trigger_constraints(d)}
    for label, name in (("issue or comment", "Comment"), ("schedule", "Schedule"),
                        ("manual dispatch", "Dispatch"), ("pull_request_target", "PRTarget"),
                        ("pull request", "PR")):
        if label in rows:
            row = rows[label]
            m[f"pctPerm{name}"] = f"{row['permissions']:.0f}"
            m[f"pctGated{name}"] = f"{row['gated']:.0f}"
            if row["pinned"] is not None:
                m[f"pctPin{name}"] = f"{row['pinned']:.0f}"
    return m


# ---------------------------------------------------------------------------
# analyses behind the figures and the per-agent table

PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review",
             "pull_request_review_comment")
ISSUE_EVENTS = ("issues", "issue_comment")


def _quarter(x: date) -> tuple[int, int]:
    return x.year, (x.month - 1) // 3 + 1


def wiring_by_quarter(d: Dataset, observed_until: date) -> list[tuple[str, Counter]]:
    """New agentic workflows per complete quarter, by how the agent is wired in.

    Starts at the quarter of `plot_start` and stops at the last quarter that
    ended before cloning began: later quarters are only partly observed.
    """
    start = _quarter(plot_start(d, observed_until))
    per: dict[tuple, Counter] = defaultdict(Counter)
    for when, kind, _ in d.adoptions():
        q = _quarter(when)
        year, number = q
        quarter_end = date(year + (number == 4), number % 4 * 3 + 1, 1)
        if q >= start and quarter_end <= observed_until and kind:
            per[q][kind] += 1
    return [(f"{y} Q{n}", per[(y, n)]) for y, n in sorted(per)]


def kaplan_meier(observations: list[tuple[int, bool]]) -> list[tuple[int, float, int]]:
    """Kaplan-Meier estimate: (time, survival after it, number at risk before it)."""
    curve, survival, at_risk = [], 1.0, len(observations)
    by_time: dict[int, list[bool]] = defaultdict(list)
    for t, event in observations:
        by_time[t].append(event)
    for t in sorted(by_time):
        events = sum(by_time[t])
        if events and at_risk:
            survival *= 1 - events / at_risk
        curve.append((t, survival, at_risk))
        at_risk -= len(by_time[t])
    return curve


def kaplan_meier_ci(observations: list[tuple[int, bool]],
                    z: float = 1.96) -> list[tuple[int, float, float, float, int]]:
    """Kaplan-Meier with a pointwise confidence interval: (time, survival,
    lower, upper, number at risk before it).

    Greenwood's variance on the log scale, S * exp(+-z * sqrt(sum d/(n(n-d)))),
    which keeps the bounds positive; the upper bound is capped at 1.
    """
    curve, survival, greenwood, at_risk = [], 1.0, 0.0, len(observations)
    by_time: dict[int, list[bool]] = defaultdict(list)
    for t, event in observations:
        by_time[t].append(event)
    for t in sorted(by_time):
        events = sum(by_time[t])
        if events and at_risk:
            survival *= 1 - events / at_risk
            if at_risk > events:
                greenwood += events / (at_risk * (at_risk - events))
        spread = math.exp(z * math.sqrt(greenwood))
        curve.append((t, survival, survival / spread, min(1.0, survival * spread), at_risk))
        at_risk -= len(by_time[t])
    return curve


def survival_at(curve: list[tuple[int, float, int]], t: int) -> float:
    s = 1.0
    for time, value, _ in curve:
        if time > t:
            break
        s = value
    return s


# Fig. 3 rows: trigger events, those that let outsiders start the agent first.
TRIGGER_ROWS = (
    ("issue or comment", ISSUE_EVENTS),
    ("pull request", PR_EVENTS),
    ("pull_request_target", ("pull_request_target",)),
    ("push", ("push",)),
    ("schedule", ("schedule",)),
    ("manual dispatch", ("workflow_dispatch",)),
    ("called by a workflow", ("workflow_call",)),
)


def trigger_constraints(d: Dataset) -> list[dict]:
    """Per trigger event, the last-snapshot agentic workflows it starts and the
    share of them that declare token permissions, restrict the triggering
    actor, and (among dedicated-action invocations) pin to a commit SHA.

    A workflow with several triggers counts in each of their rows.
    """
    latest = [r for r in d.latest.values() if d.agents(r)]
    keys = {(r["repository"], r["uid"], r["commit_hash"]) for r in latest}
    pins: dict[tuple, list[bool]] = defaultdict(list)
    for s in d.invocations:
        k = (s["repository"], s["uid"], s["commit_hash"])
        if k in keys and s["evidence_kind"] == "uses" and s["action"]:
            pins[k].append(bool(re.fullmatch(r"[0-9a-f]{40}", s.get("action_version") or "")))
    rows = []
    for label, events in TRIGGER_ROWS:
        group = [r for r in latest if set((r.get("triggers") or "").split("|")) & set(events)]
        if not group:
            continue
        pinned = [p for r in group for p in pins.get((r["repository"], r["uid"], r["commit_hash"]), [])]
        n = len(group)
        rows.append({
            "label": label, "n": n,
            "permissions": 100 * sum(1 for r in group if (r.get("permissions") or "").strip()) / n,
            "gated": 100 * sum(1 for r in group if r.get("actor_gated") == "True") / n,
            "pinned": 100 * sum(pinned) / len(pinned) if pinned else None,
        })
    return rows


def agent_profiles(d: Dataset, rows_cap: int = 4) -> list[dict]:
    """The per-agent table: last-snapshot agentic workflows, per agent group.

    The largest named agents get a row each, gh-aw files naming no engine get
    their own, and the remaining agents fold into one row, as do totals.
    """
    latest = [r for r in d.latest.values() if d.agents(r)]
    keys = {(r["repository"], r["uid"], r["commit_hash"]) for r in latest}
    pinned: dict[tuple, list[bool]] = defaultdict(list)
    for s in d.invocations:
        k = (s["repository"], s["uid"], s["commit_hash"])
        if k in keys and s["evidence_kind"] == "uses" and s["action"]:
            pinned[(k, s["agent"])].append(
                bool(re.fullmatch(r"[0-9a-f]{40}", s.get("action_version") or "")))
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in latest:
        for agent in d.agents(r):
            by_agent[agent].append(r)
    ranked = sorted((a for a in by_agent if a != "unknown"),
                    key=lambda a: (-len({r["repository"] for r in by_agent[a]}), a))
    groups = [(DISPLAY.get(a, a), [a]) for a in ranked[:rows_cap]]
    rest = ranked[rows_cap:]
    if rest:
        groups.append(("other", rest))
    if "unknown" in by_agent:
        groups.append(("unnamed", ["unknown"]))

    def profile(label: str, agents: list[str], rows: list[dict]) -> dict:
        n = len(rows)
        wiring = Counter(d.wiring(r) for r in rows)
        top, top_n = wiring.most_common(1)[0]
        pins = [p for r in rows for a in agents
                for p in pinned.get(((r["repository"], r["uid"], r["commit_hash"]), a), [])]

        def share(pred) -> str:
            return ipct(sum(1 for r in rows if pred(r)), n)

        events = lambda r: set((r.get("triggers") or "").split("|"))
        return {
            "label": label,
            "repos": count(len({r["repository"] for r in rows})),
            "workflows": count(n),
            "wiring": WIRING_SHORT.get(top, top),
            "wiring_pct": ipct(top_n, n),
            "pr": share(lambda r: events(r) & set(PR_EVENTS)),
            "issues": share(lambda r: events(r) & set(ISSUE_EVENTS)),
            "permissions": share(lambda r: (r.get("permissions") or "").strip()),
            "gated": share(lambda r: r.get("actor_gated") == "True"),
            "pinned": ipct(sum(pins), len(pins)) if pins else "--",
        }

    profiles = []
    for label, agents in groups:
        rows = list({id(r): r for a in agents for r in by_agent[a]}.values())
        profiles.append(profile(label, agents, rows))
    profiles.append(profile("all", list(by_agent), latest))
    return profiles


def agent_table(profiles: list[dict]) -> str:
    """Body rows of the per-agent table, for \\input in the paper."""
    lines = ["% Generated by scripts/paper_stats.py. Do not edit."]
    for i, p in enumerate(profiles):
        if p["label"] == "all":
            lines.append("\\midrule")
        lines.append(
            f"{p['label']} & {p['repos']} & {p['workflows']} & "
            f"{p['wiring']} ({p['wiring_pct']}) & {p['pr']} & {p['issues']} & "
            f"{p['permissions']} & {p['gated']} & {p['pinned']} \\\\"
        )
    # The closing rule lives here: \bottomrule right after \input in the
    # paper fails ("Misplaced \noalign"), as \input is not expandable.
    lines.append("\\bottomrule")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# figures


def _style():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "STIXGeneral", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 8,
        "axes.titlesize": 8,
        "axes.labelsize": 8,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
        "axes.edgecolor": INK_2,
        "axes.labelcolor": INK,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.linewidth": 0.6,
        "grid.color": GRID,
        "grid.linewidth": 0.5,
        "pdf.fonttype": 42,   # embed TrueType: IEEE PDF eXpress rejects Type 3
        "ps.fonttype": 42,
    })
    return plt



def plot_start(d: Dataset, end: date, share: float = 0.01) -> date:
    """First month whose agentic activity reaches `share` of the busiest month.

    A handful of early adopters (PR-Agent in 2023) would otherwise stretch the
    time axis over years of near-zero activity and squeeze the period that
    matters into its right edge. The text states where the plots start.
    """
    monthly = Counter(
        day(r["commit_date"]).replace(day=1)
        for r in d.rows_in_histories() if d.agents(r)
    )
    if not monthly:
        return end.replace(day=1)
    peak = max(monthly.values())
    return min(m for m, n in monthly.items() if n >= share * peak)



def wiring_figure(d: Dataset, path: Path, observed_until: date) -> None:
    """Fig. 1: how new agentic workflows wire the agent in, per quarter.

    A 100% stacked bar per complete quarter (part-to-whole over a short,
    discrete series), segments in the fixed categorical order, with the number
    of new workflows above each bar so that small early quarters are read as
    such. Four series: a legend plus a direct label on the last bar.
    """
    plt = _style()
    quarters = wiring_by_quarter(d, observed_until)
    if not quarters:
        quarters = [("", Counter())]
    order = ("uses", "run", "path", "ghaw")
    fig, ax = plt.subplots(figsize=(3.5, 1.65))
    xs = range(len(quarters))
    bottoms = [0.0] * len(quarters)
    for kind, colour in zip(order, SERIES):
        heights = [100 * c[kind] / (sum(c.values()) or 1) for _, c in quarters]
        # White edges give the 2px surface gap between stacked segments.
        ax.bar(xs, heights, bottom=bottoms, width=0.72, color=colour,
               edgecolor="white", linewidth=0.8, label=WIRING_LABEL[kind])
        bottoms = [b + h for b, h in zip(bottoms, heights)]
    for x, (_, c) in zip(xs, quarters):
        ax.text(x, 102, f"{sum(c.values()):,}", ha="center", va="bottom",
                fontsize=6, color=INK_2)
    # Direct labels beside the last bar, at each segment's middle.
    last = quarters[-1][1]
    total = sum(last.values()) or 1
    level = 0.0
    for kind in order:
        h = 100 * last[kind] / total
        if h >= 4:
            ax.text(len(quarters) - 1 + 0.42, level + h / 2, WIRING_SHORT[kind],
                    va="center", fontsize=6.5, color=INK)
        level += h
    ax.set_xticks(list(xs))
    ax.set_xticklabels([q.replace(" ", "\n") for q, _ in quarters])
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_ylabel("% of new agentic workflows")
    ax.set_xlim(-0.6, len(quarters) - 0.4 + 0.75)
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", length=0)
    ax.legend(frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(-0.02, 1.2),
              handlelength=1.0, columnspacing=0.8, handletextpad=0.4, fontsize=6.5)
    fig.tight_layout(pad=0.2)
    fig.savefig(path)
    plt.close(fig)


def constraints_figure(d: Dataset, path: Path) -> None:
    """Fig. 2: when agents run, and under which constraints.

    One row per trigger event, labelled with the number of agentic workflows
    it starts (when); three dots per row on one percentage axis (under which
    constraints). A faint connector spans each row's dots, so the gap between
    constraints reads at a glance. Marker shapes double the colour coding for
    grayscale print and colour-vision deficiencies.
    """
    plt = _style()
    rows = trigger_constraints(d)
    series = (("permissions", "permissions declared", "o"),
              ("gated", "actor restricted", "s"),
              ("pinned", "action pinned to a SHA", "^"))
    fig, ax = plt.subplots(figsize=(3.5, 0.62 + 0.17 * len(rows)))
    ys = list(range(len(rows)))[::-1]          # first row at the top
    for y, row in zip(ys, rows):
        values = [row[key] for key, _, _ in series if row[key] is not None]
        ax.plot([min(values), max(values)], [y, y], color=GRID, linewidth=1.6,
                solid_capstyle="round", zorder=1)
    for (key, label, marker), colour in zip(series, SERIES):
        points = [(row[key], y) for y, row in zip(ys, rows) if row[key] is not None]
        ax.scatter([x for x, _ in points], [y for _, y in points], s=16, marker=marker,
                   color=colour, edgecolors="white", linewidths=0.6, zorder=3, label=label)
    ax.set_yticks(ys)
    ax.set_yticklabels([f"{row['label']} ({row['n']:,})" for row in rows])
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("% of agentic workflows started by the event")
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    fig.tight_layout(pad=0.2, rect=(0, 0, 1, 0.9))
    # Centred over the whole figure, not the axes: the row labels take a third
    # of the width, and an axes-anchored legend would run off the right edge.
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, ncol=3, loc="upper center",
               bbox_to_anchor=(0.5, 1.0), handletextpad=0.2, columnspacing=1.0,
               fontsize=6.5, markerscale=1.1)
    fig.savefig(path)
    plt.close(fig)


def abandonment_figure(d: Dataset, path: Path, observed_until: date,
                       min_at_risk: int = 50, horizon_months: int = 18) -> None:
    """Fig. 3: share of repositories still invoking an agent, by months since
    adoption (Kaplan-Meier with 95% bands), per agent adopted first.

    Repositories adopting several agents at once count for each. The three
    largest agents get a line and the rest fold into "other". Every curve is
    drawn on one common window of `horizon_months`, ticked every three months
    so that the 1-, 3- and 12-month figures in the text sit on gridlines, and
    stops earlier once fewer than `min_at_risk` repositories remain under
    observation, where one abandonment would move it by 2 points or more.
    """
    plt = _style()
    lives = d.lifetimes(observed_until)
    by_agent: dict[str, list] = defaultdict(list)
    for t, gone, agents in lives:
        for a in agents:
            by_agent[a].append((t, gone))
    ranked = sorted((a for a in by_agent if a != "unknown"), key=lambda a: -len(by_agent[a]))
    top = ranked[:3]
    groups = [(DISPLAY.get(a, a), by_agent[a], c) for a, c in zip(top, SERIES)]
    rest = [obs for a in by_agent if a not in top for obs in by_agent[a]]
    if rest:
        groups.append(("other", rest, OTHER))

    month = 365.25 / 12
    horizon = horizon_months * month
    fig, ax = plt.subplots(figsize=(3.5, 1.65))
    ends, x_max = [], 0.0
    for label, obs, colour in groups:
        curve = [(0, 1.0, 1.0, 1.0, len(obs))] + kaplan_meier_ci(obs)
        curve = [p for p in curve if p[4] >= min_at_risk and p[0] <= horizon] or curve[:1]
        # A curve still observed at the window's edge runs to it, flat.
        last_t = curve[-1][0]
        reaches_edge = any(t > horizon for t, _, _, _, n in kaplan_meier_ci(obs)
                           if n >= min_at_risk)
        if reaches_edge and last_t < horizon:
            curve.append((horizon, *curve[-1][1:]))
        xs = [t / month for t, *_ in curve]
        ss = [100 * p[1] for p in curve]
        lo = [100 * p[2] for p in curve]
        hi = [100 * p[3] for p in curve]
        ax.fill_between(xs, lo, hi, step="post", color=colour, alpha=0.15, linewidth=0)
        name = f"{label} ({len(obs):,} repositories)"
        (line,) = ax.step(xs, ss, where="post", color=colour, linewidth=1.2)
        ends.append((ss[-1], name, line))
        x_max = max(x_max, xs[-1])
    # Legend in the empty lower-left corner, ordered as the curves end, top to
    # bottom. Curves stop at different ages, so labels at their ends would land
    # on top of other curves.
    ends.sort(key=lambda e: -e[0])
    ax.legend([e[2] for e in ends], [e[1] for e in ends], frameon=False,
              loc="lower left", fontsize=6.5, handlelength=1.2, borderaxespad=0.2)
    # The axis ends with the longest curve, so no labelled stretch is empty
    # and no curve runs past the last tick by more than a few weeks.
    ax.set_xticks(range(0, int(x_max) + 1, 3))
    ax.set_xlim(0, max(x_max, 1))
    ax.set_xlabel("months since the repository first invoked an agent in CI")
    ax.set_ylabel("% still agentic")
    ax.set_ylim(0, 100)
    ax.grid(axis="both")
    ax.set_axisbelow(True)
    fig.tight_layout(pad=0.2)

    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", help="mining output directory (merged CSVs)")
    parser.add_argument("--tex", default=str(SHARED / "numbers-dataset.tex"))
    parser.add_argument("--figures", default=str(SHARED / "figures"))
    parser.add_argument("--trial", metavar="DESCRIPTION",
                        help="a dry run: mark the figures as placeholders, and report the "
                             "run's own size as the candidate list so the abstract stays consistent")
    parser.add_argument("--provisional", metavar="DESCRIPTION",
                        help="the real run, not yet final (e.g. retries pending): keep the "
                             "banner, change no figure")
    parser.add_argument("--include-generic", action="store_true",
                        help="count generic_ai as agentic (only if validation folds it in)")
    args = parser.parse_args(argv)

    out = Path(args.out)
    d = Dataset(out, args.include_generic)
    values = macros(d, out, args.trial)
    if args.provisional:
        values["datasetTrial"] = args.provisional

    lines = [
        f"% Generated by scripts/paper_stats.py from {out}. Do not edit.",
        "% Macros absent here fall back to numbers-defaults.tex and render as [??].",
    ]
    lines += [f"\\def\\{k}{{{v}}}" for k, v in sorted(values.items())]
    Path(args.tex).write_text("\n".join(lines) + "\n", encoding="utf-8")

    figures = Path(args.figures)
    figures.mkdir(parents=True, exist_ok=True)
    first, end = run_dates(out)
    wiring_figure(d, figures / "wiring.pdf", first)
    constraints_figure(d, figures / "constraints.pdf")
    abandonment_figure(d, figures / "abandonment.pdf", first)
    table = Path(args.tex).with_name("table-agents.tex")
    table.write_text(agent_table(agent_profiles(d)), encoding="utf-8")

    print(f"wrote {len(values)} macros to {args.tex}, 3 figures to {figures} "
          f"and the per-agent table to {table}", file=sys.stderr)
    for key in ("nMinedRepos", "nAgenticRepos", "pctAgenticRepos", "nHistories",
                "nRevisions", "nInvocations", "nGenericRepos"):
        print(f"  {key:<18} {values.get(key)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
