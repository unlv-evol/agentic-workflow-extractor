"""scripts/frame_from_seart.py: the activity filter applied to a SEART export."""

from __future__ import annotations

import csv
import gzip
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "frame_from_seart.py"
spec = importlib.util.spec_from_file_location("frame_from_seart", SCRIPT)
frame_from_seart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frame_from_seart)


def _export(path: Path, rows: list[dict]) -> Path:
    fields = ["name", "createdAt", "lastCommit", "pushedAt", "commits", "stargazers",
              "mainLanguage", "isArchived"]
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _repo(name, created, last):
    return {"name": name, "createdAt": created, "lastCommit": last, "commits": "500"}


def test_frame_keeps_active_repositories_and_flags_the_comparable_ones(tmp_path):
    export = _export(tmp_path / "results.csv.gz", [
        _repo("old/active", "2020-01-01T00:00:00", "2026-09-01T00:00:00"),
        _repo("old/boundary", "2025-10-03T09:00:00", "2025-10-03T09:00:00"),
        _repo("new/active", "2026-02-01T00:00:00", "2026-09-30T00:00:00"),
        _repo("old/dormant", "2015-01-01T00:00:00", "2024-06-01T00:00:00"),
        _repo("clock/skew", "2020-01-01T00:00:00", "2099-01-01T12:00:00"),
        _repo("no/commit", "2020-01-01T00:00:00", ""),
    ])
    assert frame_from_seart.main([str(export), "--query-date", "2026-10-03"]) == 0

    with (tmp_path / "frame-2026-10-03.csv").open() as handle:
        frame = {r["name"]: r["ghagentfiles_comparable"] for r in csv.DictReader(handle)}
    assert frame == {"old/active": "true", "old/boundary": "true", "new/active": "false"}

    listed = [l.strip() for l in (tmp_path / "repos-2026-10-03.txt").open()
              if l.strip() and not l.startswith("#")]
    assert sorted(listed) == sorted(frame)


def test_frame_order_is_a_seeded_shuffle(tmp_path):
    rows = [_repo(f"o/r{i}", "2020-01-01T00:00:00", "2026-09-01T00:00:00") for i in range(50)]
    export = _export(tmp_path / "results.csv.gz", rows)
    lists = []
    for out in ("a", "b"):
        (tmp_path / out).mkdir()
        frame_from_seart.main([str(export), "--query-date", "2026-10-03", "-o", str(tmp_path / out)])
        lists.append((tmp_path / out / "repos-2026-10-03.txt").read_text())
    assert lists[0] == lists[1]
    names = [l for l in lists[0].splitlines() if not l.startswith("#")]
    assert names != sorted(names)
