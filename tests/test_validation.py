"""Tests for the validation sampling and scoring scripts (validation/PLAN.md)."""

from __future__ import annotations

import csv
import importlib.util
import random
from collections import Counter
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sample_validation = _load("sample_validation")
score_validation = _load("score_validation")


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- statistics -------------------------------------------------------------


def test_wilson_interval_matches_known_values():
    lo, hi = score_validation.wilson(10, 10)
    assert hi == pytest.approx(1.0)
    assert lo == pytest.approx(0.7225, abs=1e-4)
    lo, hi = score_validation.wilson(0, 10)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.2775, abs=1e-4)


def test_cohen_kappa_matches_hand_computation():
    # 10 units, 8 agreements, both coders say yes to exactly half:
    # po = 0.8, pe = 0.5, kappa = 0.6.
    pairs = [("yes", "yes")] * 4 + [("no", "no")] * 4 + [("yes", "no"), ("no", "yes")]
    assert score_validation.cohen_kappa(pairs) == pytest.approx(0.6)


def test_kappa_of_perfect_agreement_on_one_category_is_one():
    assert score_validation.cohen_kappa([("yes", "yes")] * 5) == 1.0


def test_weighted_precision_weights_strata_by_population():
    per_stratum = {"U": (10, 10), "R": (5, 10)}
    population = {"U": 900, "R": 100}
    p, lo, hi = score_validation.weighted_precision(per_stratum, population, ("U", "R"))
    assert p == pytest.approx(0.95)
    assert lo < p < hi


def test_weighted_interval_does_not_collapse_when_every_label_is_correct():
    p, lo, hi = score_validation.weighted_precision(
        {"U": (96, 96)}, {"U": 5000}, ("U",)
    )
    assert p == 1.0
    assert lo < 1.0


# --- allocation -------------------------------------------------------------


def test_allocation_floors_small_strata_and_spends_surplus_on_large_ones():
    population = {"X": 900, "P": 50, "G": 20, "U": 5000, "R": 800, "F": 30, "W": 400}
    alloc = sample_validation.allocate(population, dict(population), total=664, floor=96)
    # --total sizes the named-agent strata, which carry the overall figure.
    assert sum(alloc[s] for s in sample_validation.EXTRA_STRATA) == 664
    # Strata smaller than the floor are taken whole.
    assert (alloc["P"], alloc["G"], alloc["F"]) == (50, 20, 30)
    # Weak-only and generic get the floor on top, and never the surplus.
    assert alloc["W"] == 96 and alloc["X"] == 96
    assert sum(alloc.values()) == 664 + 2 * 96
    assert alloc["U"] > alloc["R"] >= 96


def test_allocation_stops_when_the_population_is_exhausted():
    population = {"U": 100, "R": 50}
    alloc = sample_validation.allocate(population, dict(population), total=664, floor=96)
    assert alloc == {"U": 100, "R": 50}


# --- precision sampling -----------------------------------------------------


def _steps(n_repos: int = 60, seed: int = 1) -> list[dict]:
    """Synthetic steps.csv: each unit repeated over several revisions."""
    rng = random.Random(seed)
    rows = []
    kinds = ["uses"] * 6 + ["run"] * 3 + ["path", "ghaw", "env", "url"]
    for r in range(n_repos):
        repo = f"owner{r}/repo"
        for f in range(rng.randint(1, 4)):
            kind = rng.choice(kinds)
            parse_ok = "False" if rng.random() < 0.05 else "True"
            agent = rng.choice(["claude", "codex", "copilot", "gemini"])
            sha = f"sha-{r}-{f}"
            for rev in range(rng.randint(1, 5)):  # same content in later revisions
                rows.append({
                    "repository": repo, "uid": f"f{f:08d}",
                    "commit_hash": f"c{r}{f}{rev}", "commit_date": f"2026-0{1 + rev}-01",
                    "file_path": f".github/workflows/w{f}.yml", "content_sha256": sha,
                    "parse_ok": parse_ok, "agent": agent,
                    "scope": "file" if kind in ("path", "ghaw", "env") else "step",
                    "evidence_kind": kind, "evidence_kinds": kind, "n_evidence": "1",
                    "pattern": "p", "job_id": "review", "step_index": "0",
                    "step_name": "Run agent", "action": "", "action_version": "",
                    "triggers": "pull_request", "tasks": "review",
                })
    return rows


def test_sample_has_no_duplicate_units_and_honours_the_repository_cap():
    sample, strata = sample_validation.sample_precision(
        _steps(), None, total=120, floor=10, cap=2, seed=7
    )
    keys = [sample_validation.unit_key(u) for u in sample]
    assert len(keys) == len(set(keys))
    per = Counter((u["stratum"], u["repository"]) for u in sample)
    assert max(per.values()) <= 2
    assert sum(int(s["n_sampled"]) for s in strata) == len(sample)
    assert {u["unit_id"] for u in sample} == {f"V{i:04d}" for i in range(1, len(sample) + 1)}


def test_generic_group_is_its_own_stratum_whatever_its_evidence():
    assert sample_validation.stratum_of(
        {"agent": "generic_ai", "parse_ok": "True", "evidence_kind": "uses"}
    ) == "X"
    assert sample_validation.stratum_of(
        {"agent": "generic_ai", "parse_ok": "False", "evidence_kind": "uses"}
    ) == "X"
    assert sample_validation.stratum_of(
        {"agent": "claude", "parse_ok": "True", "evidence_kind": "uses"}
    ) == "U"


def test_strata_follow_parse_status_before_evidence_kind():
    row = {"parse_ok": "False", "evidence_kind": "uses"}
    assert sample_validation.stratum_of(row) == "P"
    assert sample_validation.stratum_of({"parse_ok": "True", "evidence_kind": "env"}) == "W"
    assert sample_validation.stratum_of({"parse_ok": "True", "evidence_kind": "path"}) == "F"


def test_sampling_is_deterministic_for_a_seed():
    a, _ = sample_validation.sample_precision(_steps(), None, 80, 10, 2, seed=3)
    b, _ = sample_validation.sample_precision(_steps(), None, 80, 10, 2, seed=3)
    c, _ = sample_validation.sample_precision(_steps(), None, 80, 10, 2, seed=4)
    assert [u["unit_id"] + u["content_sha256"] for u in a] == [
        u["unit_id"] + u["content_sha256"] for u in b
    ]
    assert [u["content_sha256"] for u in a] != [u["content_sha256"] for u in c]


def test_coder_sheets_hide_the_detector_decision(tmp_path):
    steps = tmp_path / "steps.csv"
    _write(steps, _steps())
    assert sample_validation.main(
        ["precision", str(steps), "-o", str(tmp_path), "--total", "40", "--floor", "5"]
    ) == 0
    header = _read(tmp_path / "precision_sheet_A.csv")[0].keys()
    assert "q3_tasks" not in header and "q4_gated" not in header
    for hidden in ("agent", "evidence_kind", "pattern", "tasks", "stratum", "actor_gated"):
        assert hidden not in header
    assert "q1_invocation" in header
    assert _read(tmp_path / "precision_sheet_A.csv") == _read(tmp_path / "precision_sheet_B.csv")


def test_calibration_units_come_from_outside_the_sample_and_span_strata():
    steps = _steps(n_repos=120)
    sample, _ = sample_validation.sample_precision(steps, None, 60, 5, 2, seed=7)
    in_sample = {u["repository"] for u in sample}
    calib = sample_validation.sample_calibration(steps, in_sample, 12, seed=8)
    assert len(calib) == 12
    repos = [u["repository"] for u in calib]
    assert not set(repos) & in_sample
    assert len(repos) == len(set(repos))
    assert len({u["stratum"] for u in calib}) >= 4
    assert {u["unit_id"] for u in calib} == {f"C{i:03d}" for i in range(1, 13)}


# --- scoring ----------------------------------------------------------------


def _fill(path: Path, answers: dict[str, dict]) -> None:
    rows = _read(path)
    for row in rows:
        row.update(answers.get(row["unit_id"], {}))
    _write(path, rows)


def test_scoring_waits_for_adjudication_then_reports_precision(tmp_path):
    steps = tmp_path / "steps.csv"
    _write(steps, _steps())
    sample_validation.main(
        ["precision", str(steps), "-o", str(tmp_path), "--total", "40", "--floor", "5"]
    )
    sample = {r["unit_id"]: r for r in _read(tmp_path / "precision_sample.csv")}
    ids = sorted(sample)
    yes = {u: {"q1_invocation": "yes", "q2_agent": sample[u]["agent"]} for u in ids}
    _fill(tmp_path / "precision_sheet_A.csv", yes)
    disputed = ids[0]
    _fill(tmp_path / "precision_sheet_B.csv", {**yes, disputed: {"q1_invocation": "no"}})

    tex = tmp_path / "numbers-validation.tex"
    # One disagreement: agreement is reported, precision is withheld.
    assert score_validation.main(["-d", str(tmp_path), "--tex", str(tex)]) == 1
    written = tex.read_text()
    assert "\\def\\kappaOverall" in written
    assert "\\def\\precisionOverall" not in written
    adjudication = _read(tmp_path / "precision_adjudication.csv")
    assert [r["unit_id"] for r in adjudication] == [disputed]

    _fill(tmp_path / "precision_adjudication.csv", {disputed: {"q1_final": "yes"}})
    assert score_validation.main(["-d", str(tmp_path), "--tex", str(tex)]) == 0
    written = tex.read_text()
    assert "\\def\\precisionOverall{100.0\\%}" in written
    assert f"\\def\\nSampled{{{len(ids)}}}" in written
    assert "\\def\\accAgent{100.0}" in written
    # Coders answer Q1 and Q2 only: no task or gating figures.
    assert "accTasks" not in written and "accGated" not in written


def test_invalid_answers_are_rejected(tmp_path):
    steps = tmp_path / "steps.csv"
    _write(steps, _steps())
    sample_validation.main(
        ["precision", str(steps), "-o", str(tmp_path), "--total", "20", "--floor", "3"]
    )
    first = _read(tmp_path / "precision_sheet_A.csv")[0]["unit_id"]
    _fill(tmp_path / "precision_sheet_A.csv", {first: {"q1_invocation": "probably"}})
    _fill(tmp_path / "precision_sheet_B.csv", {first: {"q1_invocation": "yes"}})
    assert score_validation.main(["-d", str(tmp_path), "--tex", str(tmp_path / "n.tex")]) == 2


# --- recall -----------------------------------------------------------------


def test_recall_pool_takes_latest_revision_of_never_agentic_keyword_files(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    files = {
        "old": "steps:\n  - run: echo openai\n",
        "new": "steps:\n  - run: curl https://api.openai.com\n",
        "plain": "steps:\n  - run: make test\n",
        "agentic": "steps:\n  - uses: anthropics/claude-code-action@v1\n",
    }
    for sha, text in files.items():
        (content / f"{sha}.yml").write_text(text)
    base = {"repository": "o/r", "commit_hash": "c", "file_path": "w.yml",
            "agents": "", "parse_ok": "True"}
    revisions = [
        {**base, "uid": "f1", "commit_date": "2026-01-01", "content_sha256": "old", "agentic": "False"},
        {**base, "uid": "f1", "commit_date": "2026-02-01", "content_sha256": "new", "agentic": "False"},
        {**base, "uid": "f2", "commit_date": "2026-01-01", "content_sha256": "plain", "agentic": "False"},
        {**base, "uid": "f3", "commit_date": "2026-01-01", "content_sha256": "agentic", "agentic": "True"},
        {**base, "uid": "f3", "commit_date": "2026-03-01", "content_sha256": "plain", "agentic": "False"},
    ]
    picks, pool = sample_validation.sample_recall(
        revisions, content, n=10, seed=1, keywords=sample_validation.DEFAULT_KEYWORDS
    )
    assert pool == 1
    assert [(p["uid"], p["content_sha256"]) for p in picks] == [("f1", "new")]
    assert picks[0]["keyword_hits"] == "openai"


def test_generic_precision_is_reported_apart_with_the_fold_in_decision(tmp_path, capsys):
    rows = _steps()
    for i, row in enumerate(rows):
        if i % 5 == 0:
            row["agent"] = "generic_ai"
            row["evidence_kind"] = "uses"
            row["parse_ok"] = "True"
    steps = tmp_path / "steps.csv"
    _write(steps, rows)
    sample_validation.main(
        ["precision", str(steps), "-o", str(tmp_path), "--total", "40", "--floor", "8"]
    )
    sample = {r["unit_id"]: r for r in _read(tmp_path / "precision_sample.csv")}
    generic = [u for u, r in sample.items() if r["stratum"] == "X"]
    assert len(generic) == 8
    # Generic invocations are mostly wrong; everything else is right.
    answers = {u: {"q1_invocation": "no" if u in generic[:6] else "yes"} for u in sample}
    _fill(tmp_path / "precision_sheet_A.csv", answers)
    _fill(tmp_path / "precision_sheet_B.csv", answers)
    tex = tmp_path / "n.tex"
    assert score_validation.main(["-d", str(tmp_path), "--tex", str(tex)]) == 0
    written = tex.read_text()
    assert "\\def\\precGeneric{25.0\\%}" in written
    # The generic group's errors do not reach the overall figure.
    assert "\\def\\precisionOverall{100.0\\%}" in written
    assert "REPORT SEPARATELY" in capsys.readouterr().err


# --- subsampling an existing draw (option A, 2026-10-29) ----------------------


def test_proportional_allocation_respects_the_minimum_and_the_total():
    population = {"P": 750, "G": 31971, "U": 51266, "R": 46679, "F": 6077}
    capacity = {"P": 97, "G": 139, "U": 165, "R": 159, "F": 104}
    alloc = sample_validation.allocate_proportional(population, capacity, 384, 30)
    assert alloc == {"P": 30, "F": 30, "G": 80, "U": 128, "R": 116}
    assert sum(alloc.values()) == 384


def test_subsample_keeps_a_seeded_subset_and_leaves_weak_and_generic_whole():
    sample, strata = sample_validation.sample_precision(
        _steps(n_repos=3000, seed=11), None, total=664, floor=96, cap=2, seed=7
    )
    small, new_strata = sample_validation.subsample_precision(sample, strata, 200, 20, seed=1)
    again, _ = sample_validation.subsample_precision(sample, strata, 200, 20, seed=1)
    assert [u["unit_id"] for u in small] == [u["unit_id"] for u in again]
    ids = {u["unit_id"] for u in sample}
    assert {u["unit_id"] for u in small} <= ids
    # the global shuffled order survives: kept ids appear in their original order
    order = [u["unit_id"] for u in sample]
    assert [u["unit_id"] for u in small] == [i for i in order if i in {u["unit_id"] for u in small}]
    named = [u for u in small if u["stratum"] in sample_validation.EXTRA_STRATA]
    assert len(named) == 200
    for s in ("W", "X"):
        assert sum(u["stratum"] == s for u in small) == sum(u["stratum"] == s for u in sample)
    sampled = {r["stratum"]: r["n_sampled"] for r in new_strata}
    assert sum(sampled[s] for s in sample_validation.EXTRA_STRATA) == 200
    assert all(sampled[s] >= 20 for s in sample_validation.EXTRA_STRATA)
