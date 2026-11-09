"""Integrity tests for the generated development fixtures."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
SAMPLES_PER_METRIC = 60
REQUIRED_LOG_FIELDS = {"event_id", "ts", "level", "service", "host", "event", "message"}
FORBIDDEN_STRINGS = ("family", "expected_status", "key_evidence")


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def _incidents() -> list[dict[str, Any]]:
    return _manifest()["incidents"]


def _readme_metric_names() -> set[str]:
    """Metric names listed in the `metrics.csv` section of fixtures/README.md."""
    text = (FIXTURES / "README.md").read_text(encoding="utf-8")
    section = text.split("## `metrics.csv`", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"`([a-z0-9_]+)`", section)) - {"ts", "service", "metric", "value", "unit"}


INCIDENTS = _incidents()
IDS = [inc["incident_id"] for inc in INCIDENTS]


def test_manifest_lists_incidents() -> None:
    assert IDS, "manifest has no incidents"
    assert len(IDS) == len(set(IDS)), "duplicate incident IDs in manifest"


@pytest.mark.parametrize("incident", INCIDENTS, ids=IDS)
@pytest.mark.parametrize("kind", ["logs", "metrics"])
def test_manifest_sha256_matches(incident: dict[str, Any], kind: str) -> None:
    entry = incident[kind]
    path = FIXTURES / entry["path"]
    assert path.is_file(), f"missing {path}"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


def test_readme_lists_twelve_metrics() -> None:
    assert len(_readme_metric_names()) == 12


@pytest.mark.parametrize("incident", INCIDENTS, ids=IDS)
def test_metrics_have_expected_names_and_counts(incident: dict[str, Any]) -> None:
    expected = _readme_metric_names()
    with (FIXTURES / incident["metrics"]["path"]).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    counts = Counter(row["metric"] for row in rows)
    assert set(counts) == expected
    assert all(n == SAMPLES_PER_METRIC for n in counts.values()), counts


@pytest.mark.parametrize("incident", INCIDENTS, ids=IDS)
def test_log_lines_are_json_with_required_fields(incident: dict[str, Any]) -> None:
    path = FIXTURES / incident["logs"]["path"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines, f"{path} is empty"
    for lineno, line in enumerate(lines, start=1):
        record = json.loads(line)
        assert isinstance(record, dict), f"line {lineno} is not an object"
        missing = REQUIRED_LOG_FIELDS - record.keys()
        assert not missing, f"line {lineno} missing {sorted(missing)}"


def test_no_gold_label_strings_under_fixtures() -> None:
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_STRINGS:
            assert needle not in text, f"{needle!r} found in {path.relative_to(ROOT)}"
