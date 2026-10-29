"""scripts/labeling.py: the coders' workbooks, and importing them back."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "labeling.py"
spec = importlib.util.spec_from_file_location("labeling", SCRIPT)
labeling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labeling)

STEP_FIELDS = ["unit_id", "permalink", "scope", "job_id", "step_index", "step_name",
               "q1_invocation", "q2_agent", "notes"]
FILE_FIELDS = ["unit_id", "permalink", "q1_invocation", "q2_agent", "notes"]
LINK = "https://github.com/o/r/blob/{}/.github/workflows/w.yml"


def _write(path: Path, fields, rows) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def package(tmp_path, monkeypatch):
    """Blank sheets for 3 precision, 2 recall and 2 calibration units, built."""
    src = tmp_path / "validation"
    src.mkdir()
    step = [{"unit_id": f"V{i:04d}", "permalink": LINK.format(i), "scope": "step",
             "job_id": "review", "step_index": str(i), "step_name": "Run agent"} for i in range(1, 4)]
    files = [{"unit_id": f"R{i:04d}", "permalink": LINK.format(i)} for i in range(1, 3)]
    for coder in "AB":
        _write(src / f"precision_sheet_{coder}.csv", STEP_FIELDS, step)
        _write(src / f"recall_sheet_{coder}.csv", FILE_FIELDS, files)
    _write(src / "calibration_sheet.csv", STEP_FIELDS,
           [dict(step[0], unit_id="C0001"), dict(step[1], unit_id="C0002")])

    def fake_guide(out):            # the real one needs pdflatex
        pdf = out / labeling.GUIDE_PDF
        pdf.write_bytes(b"%PDF-1.4\n")
        return pdf

    monkeypatch.setattr(labeling, "build_guide", fake_guide)
    out = tmp_path / "labeling"
    labeling.build(out, src)
    return src, out


def _fill(path: Path, tab: str, answers: dict[str, tuple]) -> None:
    wb = openpyxl.load_workbook(path)
    ws = wb[tab]
    header = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        unit = row[header.index("unit_id")].value
        if unit in answers:
            q1, q2, note = answers[unit]
            row[header.index("Q1 invocation")].value = q1
            row[header.index("Q2 agent")].value = q2
            row[header.index("notes")].value = note
    wb.save(path)


def test_each_coder_gets_only_their_blind_workbook(package):
    _, out = package
    assert sorted(p.name for p in (out / "coder-A").iterdir()) == [
        "GHAgentCI-coding-guide.pdf", "GHAgentCI-labeling-coder-A.xlsx"]
    assert not list((out / "coder-A").glob("*coder-B*"))
    wb = openpyxl.load_workbook(out / "coder-A" / "GHAgentCI-labeling-coder-A.xlsx")
    assert wb.sheetnames == ["Start here", "Codebook", "Precision", "Recall"]
    header = [c.value for c in wb["Precision"][1]]
    for leaked in ("agent", "stratum", "evidence_kind", "pattern", "tasks"):
        assert leaked not in header
    assert wb["Precision"].max_row == 4 and wb["Recall"].max_row == 3
    assert wb["Precision"]["B2"].hyperlink.target == LINK.format(1)
    calibration = openpyxl.load_workbook(out / "shared" / "GHAgentCI-calibration.xlsx")
    assert calibration.sheetnames == ["Start here", "Codebook", "Calibration"]


def test_import_writes_normalised_answers_into_the_csv_sheets(package):
    src, out = package
    book = out / "coder-A" / "GHAgentCI-labeling-coder-A.xlsx"
    _fill(book, "Precision", {"V0001": ("Yes", "Claude", "runs on PRs"),
                              "V0002": ("no", None, None),
                              "V0003": ("maybe", "generic_ai", None)})
    _fill(book, "Recall", {"R0001": ("no", None, None), "R0002": ("yes", "codex", "missed!")})
    assert labeling.import_workbook(book, "A", False, src) == 0
    rows = {r["unit_id"]: r for r in csv.DictReader((src / "precision_sheet_A.csv").open())}
    assert (rows["V0001"]["q1_invocation"], rows["V0001"]["q2_agent"]) == ("yes", "claude")
    assert rows["V0001"]["notes"] == "runs on PRs"
    assert rows["V0001"]["permalink"] == LINK.format(1)        # other columns kept
    recall = {r["unit_id"]: r for r in csv.DictReader((src / "recall_sheet_A.csv").open())}
    assert recall["R0002"]["q2_agent"] == "codex"
    # coder B's sheets are untouched
    assert not any(r["q1_invocation"] for r in csv.DictReader((src / "precision_sheet_B.csv").open()))


def test_invalid_answers_or_missing_units_write_nothing(package):
    src, out = package
    book = out / "coder-B" / "GHAgentCI-labeling-coder-B.xlsx"
    _fill(book, "Precision", {"V0001": ("probably", None, None)})
    before = (src / "precision_sheet_B.csv").read_text()
    assert labeling.import_workbook(book, "B", False, src) == 2
    assert (src / "precision_sheet_B.csv").read_text() == before

    _fill(book, "Precision", {"V0001": ("yes", "Claude Code", None)})
    assert labeling.import_workbook(book, "B", False, src) == 2

    _fill(book, "Precision", {"V0001": ("yes", "claude", None)})
    wb = openpyxl.load_workbook(book)
    wb["Precision"].delete_rows(3)          # a coder deleted unit V0002
    wb.save(book)
    assert labeling.import_workbook(book, "B", False, src) == 2
    assert (src / "precision_sheet_B.csv").read_text() == before


def test_calibration_imports_into_its_own_sheet(package):
    src, out = package
    book = out / "shared" / "GHAgentCI-calibration.xlsx"
    _fill(book, "Calibration", {"C0001": ("yes", "copilot", "agreed"), "C0002": ("no", None, None)})
    assert labeling.import_workbook(book, None, True, src) == 0
    rows = {r["unit_id"]: r for r in csv.DictReader((src / "calibration_sheet.csv").open())}
    assert rows["C0001"]["q2_agent"] == "copilot"


def test_build_refuses_to_overwrite_coded_sheets(package):
    src, out = package
    book = out / "coder-A" / "GHAgentCI-labeling-coder-A.xlsx"
    _fill(book, "Precision", {u: ("no", None, None) for u in ("V0001", "V0002", "V0003")})
    _fill(book, "Recall", {u: ("no", None, None) for u in ("R0001", "R0002")})
    assert labeling.import_workbook(book, "A", False, src) == 0
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        labeling.build(out, src)


def test_link_text_names_repository_and_path():
    assert labeling.link_text(LINK.format("abc")) == "o/r: .github/workflows/w.yml"
