"""Tests for safe paths, config, schemas and the fixture store."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from triage import fixtures_store
from triage.config import require_llm_settings
from triage.errors import ChecksumMismatch, ConfigError, PathViolation, UnknownIncident
from triage.paths import FIXTURES_DIR, GOLD_DIR, REPO_ROOT, safe_join
from triage.schemas import (
    ApprovalRecord,
    DraftReport,
    Evidence,
    FinalReportAction,
    ModelAction,
    ToolCallAction,
    report_sha256,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    fixtures_store.clear_cache()
    yield
    fixtures_store.clear_cache()


def _report(**overrides) -> DraftReport:
    data = {
        "incident_id": "INC-003",
        "status": "diagnosed",
        "family": "dependency_failure",
        "summary": "orders-db degraded",
        "root_cause": "autovacuum and high I/O wait on orders-db",
        "cited_evidence": [{"evidence_id": "INC-003:L0205", "reason": "slow query"}],
        "timeline": ["18:27 autovacuum started"],
        "next_steps": ["check vacuum settings"],
        "confidence": 0.8,
    }
    data.update(overrides)
    return DraftReport.model_validate(data)


# --------------------------------------------------------------------------- #
# Incident IDs                                                                #
# --------------------------------------------------------------------------- #
def test_unregistered_incident_raises() -> None:
    with pytest.raises(UnknownIncident):
        fixtures_store.get_incident("INC-999")


@pytest.mark.parametrize(
    "bad_id",
    ["../evaluation/gold/dev_labels.json", "INC-01", "inc-001", "INC-001/../x", "", "INC-0011"],
)
def test_malformed_incident_raises(bad_id: str) -> None:
    with pytest.raises(UnknownIncident):
        fixtures_store.load_bundle(bad_id)


# --------------------------------------------------------------------------- #
# safe_join                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "relative",
    [
        "..\\..\\evaluation\\gold\\dev_labels.json",
        "../evaluation/gold/dev_labels.json",
        "incidents/../../scripts/generate_fixtures.py",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "C:relative.txt",
        "\\\\server\\share\\x",
        "/etc/passwd",
        "\\evaluation\\gold",
        "",
        "manifest.json\x00",
    ],
)
def test_safe_join_rejects_escapes(relative: str) -> None:
    with pytest.raises(PathViolation):
        safe_join(FIXTURES_DIR, relative)


def test_safe_join_rejects_absolute_path_object() -> None:
    with pytest.raises(PathViolation):
        safe_join(FIXTURES_DIR, (FIXTURES_DIR / "manifest.json").resolve())


@pytest.mark.parametrize(
    ("base", "relative"),
    [
        (REPO_ROOT, "evaluation/gold/dev_labels.json"),
        (REPO_ROOT, "evaluation\\gold"),
        (REPO_ROOT, "evaluation/./gold/dev_labels.json"),
        (REPO_ROOT / "evaluation", "gold/dev_labels.json"),
        (REPO_ROOT / "evaluation", "gold"),
        (GOLD_DIR, "dev_labels.json"),
        (GOLD_DIR, "anything/else.txt"),
    ],
)
def test_safe_join_refuses_gold_from_any_base(base: Path, relative: str) -> None:
    with pytest.raises(PathViolation):
        safe_join(base, relative)


def test_safe_join_allows_normal_fixture_path() -> None:
    path = safe_join(FIXTURES_DIR, "incidents/INC-001/logs.jsonl")
    assert path.is_file()
    assert path.is_relative_to(FIXTURES_DIR.resolve())


# --------------------------------------------------------------------------- #
# Bundles                                                                     #
# --------------------------------------------------------------------------- #
def test_valid_bundle_loads() -> None:
    entry, logs, metrics = fixtures_store.load_bundle("INC-003")
    assert entry.incident_id == "INC-003"
    assert len(metrics) == 60 * 12
    assert logs and all("event_id" in rec for rec in logs)
    assert isinstance(metrics[0]["value"], float)


def test_bundle_cache_returns_independent_copies() -> None:
    _, logs, _ = fixtures_store.load_bundle("INC-001")
    logs[0]["message"] = "mutated"
    logs.clear()
    _, logs_again, _ = fixtures_store.load_bundle("INC-001")
    assert logs_again and logs_again[0]["message"] != "mutated"


def test_tampered_bundle_raises_checksum_mismatch(tmp_path: Path, monkeypatch) -> None:
    copy = tmp_path / "fixtures"
    shutil.copytree(FIXTURES_DIR, copy)
    target = copy / "incidents" / "INC-001" / "logs.jsonl"
    data = bytearray(target.read_bytes())
    data[0] = ord("X")  # also makes the file invalid JSON: proves the hash is checked first
    target.write_bytes(bytes(data))

    monkeypatch.setattr(fixtures_store, "FIXTURES_DIR", copy)
    with pytest.raises(ChecksumMismatch):
        fixtures_store.load_bundle("INC-001")
    # untouched incidents in the copy still load
    assert fixtures_store.load_bundle("INC-002")[0].incident_id == "INC-002"


# --------------------------------------------------------------------------- #
# Schemas                                                                     #
# --------------------------------------------------------------------------- #
def test_report_sha256_stable_and_sensitive() -> None:
    base = _report()
    assert report_sha256(base) == report_sha256(_report())
    variants = {
        "status": "inconclusive",
        "family": "none",
        "summary": "different",
        "root_cause": "different",
        "cited_evidence": [],
        "timeline": [],
        "next_steps": ["other"],
        "confidence": 0.81,
        "incident_id": "INC-001",
    }
    for field, value in variants.items():
        assert report_sha256(_report(**{field: value})) != report_sha256(base), field


def test_draft_report_rejects_unknown_family() -> None:
    with pytest.raises(ValidationError):
        _report(family="memory_leak")


def test_draft_report_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        _report(severity="high")


def test_draft_report_rejects_bad_confidence() -> None:
    with pytest.raises(ValidationError):
        _report(confidence=1.5)


@pytest.mark.parametrize(
    ("evidence_id", "kind", "incident_id"),
    [
        ("INC-003:L0205", "log", "INC-003"),
        ("INC-003:M:db_pool_active:1800-1900", "metric", "INC-003"),
        ("RB:dependency-failure@1.0", "runbook", None),
    ],
)
def test_evidence_id_formats_accepted(evidence_id: str, kind: str, incident_id: str | None) -> None:
    Evidence(evidence_id=evidence_id, kind=kind, incident_id=incident_id, summary="s")


@pytest.mark.parametrize(
    ("evidence_id", "kind", "incident_id"),
    [
        ("INC-003:L0205", "log", "INC-001"),  # prefix mismatch
        ("INC-003:L0205", "metric", "INC-003"),  # wrong kind
        ("RB:dependency-failure@1.0", "runbook", "INC-003"),  # runbook with incident
        ("INC-003:M:db_pool_active:18-19", "metric", "INC-003"),
    ],
)
def test_evidence_id_formats_rejected(evidence_id: str, kind: str, incident_id: str | None) -> None:
    with pytest.raises(ValidationError):
        Evidence(evidence_id=evidence_id, kind=kind, incident_id=incident_id, summary="s")


def test_model_action_discriminates() -> None:
    adapter = TypeAdapter(ModelAction)
    call = adapter.validate_python({"type": "tool_call", "tool": "query_logs", "args": {}})
    assert isinstance(call, ToolCallAction)
    final = adapter.validate_python(
        {"type": "final_report", "report": _report().model_dump()}
    )
    assert isinstance(final, FinalReportAction)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "shell", "cmd": "rm -rf /"})


def test_approval_record_normalises_to_utc_and_rejects_naive() -> None:
    sha = report_sha256(_report())
    est = timezone(timedelta(hours=-5))
    rec = ApprovalRecord(decision="approve", report_sha256=sha, decided_at=datetime(2026, 1, 1, 7, tzinfo=est))
    assert rec.decided_at.utcoffset() == timedelta(0) and rec.decided_at.hour == 12
    with pytest.raises(ValidationError):
        ApprovalRecord(decision="approve", report_sha256=sha, decided_at=datetime(2026, 1, 1))


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
def test_require_llm_settings_names_missing_vars(monkeypatch, tmp_path: Path) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    with pytest.raises(ConfigError) as exc:
        require_llm_settings(env_file=tmp_path / "missing.env")
    message = str(exc.value)
    assert "LLM_BASE_URL" in message and "LLM_API_KEY" in message
    assert "LLM_MODEL" not in message.split(":", 1)[1].split("(")[0]


def test_require_llm_settings_ok(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_TIMEOUT_S", "30")
    settings = require_llm_settings(env_file=tmp_path / "missing.env")
    assert settings.LLM_TIMEOUT_S == 30.0
    assert "dummy" not in repr(settings)
