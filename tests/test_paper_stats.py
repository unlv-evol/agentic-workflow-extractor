"""Definitions behind the paper's dataset figures (scripts/paper_stats.py)."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_stats.py"
spec = importlib.util.spec_from_file_location("paper_stats", SCRIPT)
paper_stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paper_stats)

from awe.cli import REVISION_FIELDS, STEP_FIELDS, SUMMARY_FIELDS  # noqa: E402


def _write(path: Path, fields, rows) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})


def _revision(repo, uid, commit, when, change="Modified", **extra):
    return {"repository": repo, "uid": uid, "commit_hash": commit,
            "commit_date": when, "git_change_type": change,
            "symbolic_link": "False", **extra}


def _step(repo, uid, commit, when, agent, kind="uses"):
    return {"repository": repo, "uid": uid, "commit_hash": commit,
            "commit_date": when, "agent": agent, "evidence_kind": kind}


@pytest.fixture
def run(tmp_path):
    """Four repositories: one Claude adopter that later deletes its workflow,
    one Copilot adopter, one generic-only, one with nothing agentic."""
    _write(tmp_path / "repositories.csv", SUMMARY_FIELDS, [
        {"repository": r, "status": "ok"} for r in ("a/claude", "b/copilot", "c/generic", "d/none")
    ] + [{"repository": "github/gh-aw", "status": "excluded"}])
    _write(tmp_path / "revisions.csv", REVISION_FIELDS, [
        _revision("a/claude", "f1", "c1", "2026-01-05T10:00:00", "Added"),
        _revision("a/claude", "f1", "c2", "2026-03-01T10:00:00"),
        _revision("a/claude", "f1", "c3", "2026-05-01T10:00:00", "Deleted"),
        _revision("b/copilot", "f1", "c4", "2026-02-01T10:00:00", "Added"),
        _revision("c/generic", "f1", "c5", "2026-02-01T10:00:00", "Added"),
    ])
    _write(tmp_path / "steps.csv", STEP_FIELDS, [
        _step("a/claude", "f1", "c1", "2026-01-05T10:00:00", "claude"),
        _step("a/claude", "f1", "c1", "2026-01-05T10:00:00", "claude", kind="env"),
        _step("a/claude", "f1", "c2", "2026-03-01T10:00:00", "claude"),
        _step("b/copilot", "f1", "c4", "2026-02-01T10:00:00", "copilot", kind="path"),
        _step("c/generic", "f1", "c5", "2026-02-01T10:00:00", "generic_ai"),
    ])
    return tmp_path


def test_headline_counts_named_agents_and_keeps_generic_apart(run):
    d = paper_stats.Dataset(run, include_generic=False)
    m = paper_stats.macros(d, run, trial=None)
    assert m["nMinedRepos"] == "4"
    assert m["nExcludedRepos"] == "1"
    assert m["nAgenticRepos"] == "2"
    assert m["pctAgenticRepos"] == "50.0"
    assert m["nGenericRepos"] == "1"
    assert m["nHistories"] == "2"
    assert m["nRevisions"] == "4"            # the deletion belongs to the history
    assert m["nInvocations"] == "4"          # the weak env row counts as an invocation...
    assert m["nInvWeak"] == "1"              # ...and is reported as weak
    assert m["nInvUses"] == "2" and m["nInvPath"] == "1"
    assert m["firstInvocationDate"] == "5 January 2026"


def test_last_snapshot_drops_deleted_histories(run):
    d = paper_stats.Dataset(run, include_generic=False)
    m = paper_stats.macros(d, run, trial=None)
    assert m["nHistoriesLast"] == "1"        # the Claude workflow was deleted
    assert m["nAgenticReposLast"] == "1"
    assert m["pctAgenticReposLast"] == "50.0"


def test_generic_can_be_folded_in(run):
    d = paper_stats.Dataset(run, include_generic=True)
    m = paper_stats.macros(d, run, trial=None)
    assert m["nAgenticRepos"] == "3" and m["nGenericRepos"] == "0"


def test_trial_marks_itself_and_stays_self_consistent(run):
    d = paper_stats.Dataset(run, include_generic=False)
    m = paper_stats.macros(d, run, trial="dry run")
    assert m["datasetTrial"] == "dry run"
    assert m["nCandidateRepos"] == "5"


def test_figures_are_written(run, tmp_path):
    pytest.importorskip("matplotlib")
    figures = tmp_path / "figs"
    assert paper_stats.main([str(run), "--tex", str(tmp_path / "n.tex"),
                             "--figures", str(figures)]) == 0
    assert {p.name for p in figures.glob("*.pdf")} == {"wiring.pdf", "constraints.pdf", "abandonment.pdf"}
    table = (tmp_path / "table-agents.tex").read_text()
    assert "Copilot &" in table and "\\midrule" in table
    # The closing rule is generated too: \bottomrule after \input fails in TeX.
    assert table.rstrip().endswith("\\bottomrule")


def test_pre_chatgpt_matches_are_dropped_and_counted(run):
    """gh-aw source heuristics matched ordinary 2019-2022 Markdown files."""
    with (run / "revisions.csv").open("a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=REVISION_FIELDS, extrasaction="ignore").writerow(
            {f: "" for f in REVISION_FIELDS}
            | _revision("d/none", "f1", "c9", "2021-05-01T10:00:00", "Added"))
    with (run / "steps.csv").open("a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=STEP_FIELDS, extrasaction="ignore").writerow(
            {f: "" for f in STEP_FIELDS}
            | _step("d/none", "f1", "c9", "2021-05-01T10:00:00", "unknown", kind="ghaw"))
    d = paper_stats.Dataset(run, include_generic=False)
    m = paper_stats.macros(d, run, trial=None)
    assert m["nAgenticRepos"] == "2"          # d/none is not agentic
    assert m["nPreRelease"] == "1"
    assert m["nPreReleaseHistories"] == "1"


def test_latest_revision_uses_true_instants_and_git_order(tmp_path):
    """Same-day commits and commits in different time zones are ordered by
    instant; git's newest-first listing breaks exact ties."""
    _write(tmp_path / "repositories.csv", SUMMARY_FIELDS, [{"repository": "a/b", "status": "ok"}])
    _write(tmp_path / "revisions.csv", REVISION_FIELDS, [
        # newest first, as git log writes them: the removal came last that day
        _revision("a/b", "f1", "c3", "2026-03-01T09:00:00-07:00"),   # 16:00 UTC: plain
        _revision("a/b", "f1", "c2", "2026-03-01T17:00:00+02:00"),   # 15:00 UTC: agentic
        _revision("a/b", "f1", "c1", "2026-02-01T10:00:00+00:00", "Added"),
    ])
    _write(tmp_path / "steps.csv", STEP_FIELDS, [
        _step("a/b", "f1", "c2", "2026-03-01T17:00:00+02:00", "claude"),
        _step("a/b", "f1", "c1", "2026-02-01T10:00:00+00:00", "claude"),
    ])
    d = paper_stats.Dataset(tmp_path, include_generic=False)
    m = paper_stats.macros(d, tmp_path, trial=None)
    # The string "17:00+02:00" sorts after "09:00-07:00", but it is earlier.
    assert m["nAgenticReposLast"] == "0"
    assert m["nHistoriesLast"] == "1"


def _lifetime_run(tmp_path, revisions, steps):
    _write(tmp_path / "repositories.csv", SUMMARY_FIELDS, [{"repository": "a/b", "status": "ok"}])
    _write(tmp_path / "revisions.csv", REVISION_FIELDS, revisions)
    _write(tmp_path / "steps.csv", STEP_FIELDS, steps)
    return paper_stats.Dataset(tmp_path, include_generic=False)


def test_a_replaced_workflow_is_not_an_abandonment(tmp_path):
    """claude.yml deleted in the commit that adds claude-review.yml: still agentic."""
    from datetime import date
    d = _lifetime_run(tmp_path, [
        _revision("a/b", "f2", "c2", "2026-03-01T10:00:00", "Added"),
        _revision("a/b", "f1", "c2", "2026-03-01T10:00:00", "Deleted"),
        _revision("a/b", "f1", "c1", "2026-01-01T10:00:00", "Added"),
    ], [
        _step("a/b", "f1", "c1", "2026-01-01T10:00:00", "claude"),
        _step("a/b", "f2", "c2", "2026-03-01T10:00:00", "claude"),
    ])
    [(days, gone, first)] = d.lifetimes(date(2026, 6, 1))
    assert not gone and days == 151 and first == {"claude"}


def test_removing_the_last_agentic_workflow_is_an_abandonment(tmp_path):
    from datetime import date
    d = _lifetime_run(tmp_path, [
        _revision("a/b", "f1", "c2", "2026-01-31T10:00:00"),          # agent removed
        _revision("a/b", "f1", "c1", "2026-01-01T10:00:00", "Added"),
    ], [_step("a/b", "f1", "c1", "2026-01-01T10:00:00", "codex", kind="run")])
    assert d.lifetimes(date(2026, 6, 1)) == [(30, True, {"codex"})]


def test_wiring_prefers_ghaw_and_reserved_names_over_steps(tmp_path):
    d = _lifetime_run(tmp_path, [
        _revision("a/b", "f1", "c1", "2026-01-01T10:00:00", "Added"),
    ], [
        _step("a/b", "f1", "c1", "2026-01-01T10:00:00", "copilot", kind="uses"),
        _step("a/b", "f1", "c1", "2026-01-01T10:00:00", "copilot", kind="ghaw"),
    ])
    assert d.wiring({"repository": "a/b", "uid": "f1", "commit_hash": "c1"}) == "ghaw"


def test_wiring_quarters_stop_before_cloning(tmp_path):
    """A quarter still under way when cloning started is only partly observed."""
    from datetime import date
    d = _lifetime_run(tmp_path, [
        _revision("a/b", "f2", "c2", "2026-07-15T10:00:00", "Added"),
        _revision("a/b", "f1", "c1", "2026-05-01T10:00:00", "Added"),
    ], [
        _step("a/b", "f1", "c1", "2026-05-01T10:00:00", "claude"),
        _step("a/b", "f2", "c2", "2026-07-15T10:00:00", "codex", kind="run"),
    ])
    quarters = paper_stats.wiring_by_quarter(d, date(2026, 8, 1))
    assert [(q, dict(c)) for q, c in quarters] == [("2026 Q2", {"uses": 1})]


def test_kaplan_meier_matches_hand_computation():
    curve = paper_stats.kaplan_meier([(1, True), (2, False), (3, True), (3, False)])
    # t=1: 1 of 4 at risk drop -> 0.75; t=3: 1 of 2 -> 0.375
    assert paper_stats.survival_at(curve, 2) == 0.75
    assert paper_stats.survival_at(curve, 3) == 0.375


def test_kaplan_meier_ci_brackets_the_estimate_and_widens_as_risk_shrinks():
    import math
    obs = [(t, True) for t in range(1, 6)] + [(10, False)] * 5
    curve = paper_stats.kaplan_meier_ci(obs)
    for t, s, lo, hi, _ in curve:
        assert 0 < lo <= s <= hi <= 1
    # Greenwood at t=1: d=1, n=10 -> var term 1/(10*9); S = 0.9.
    t, s, lo, hi, n = curve[0]
    assert n == 10 and s == 0.9
    assert math.isclose(hi, min(1.0, 0.9 * math.exp(1.96 * math.sqrt(1 / 90))))
    widths = [math.log(hi / lo) for _, _, lo, hi, _ in curve[:5]]
    assert widths == sorted(widths)
