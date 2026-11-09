"""Tests for the report validator, using ledgers built from real tool results."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import build_state, ledger_from, window
from triage.schemas import DraftReport, Evidence, RunState
from triage.tools.logs import query_logs
from triage.tools.metrics import query_metrics
from triage.tools.runbooks import search_runbooks
from triage.validator import validate_report


# --------------------------------------------------------------------------- #
# Real ledgers                                                                #
# --------------------------------------------------------------------------- #
def _inc001_ledger() -> dict[str, Evidence]:
    start, end = window("INC-001")
    return ledger_from(
        query_logs("INC-001", start, end, event="db_pool_timeout", limit=3),
        query_metrics("INC-001", "db_pool_active", start, end),
        search_runbooks("timed out waiting for a connection from pool", top_k=2),
    )


def _ids(ledger: dict[str, Evidence], kind: str) -> list[str]:
    return [i for i, ev in ledger.items() if ev.kind == kind]


def _runbook_id(ledger: dict[str, Evidence], family: str) -> str:
    return next(i for i, ev in ledger.items() if ev.kind == "runbook" and ev.data["family"] == family)


def _report(incident_id: str, cite: list[str], **overrides: Any) -> DraftReport:
    data: dict[str, Any] = {
        "incident_id": incident_id,
        "status": "diagnosed",
        "family": "connection_pool_exhaustion",
        "summary": "Pool exhausted under a traffic surge.",
        "root_cause": "Connection pool saturated; requests timed out waiting for a connection.",
        "cited_evidence": [{"evidence_id": i, "reason": "supports the diagnosis"} for i in cite],
        "timeline": [],
        "next_steps": ["shed load"],
        "confidence": 0.8,
    }
    data.update(overrides)
    return DraftReport.model_validate(data)


@pytest.fixture(scope="module")
def inc001() -> tuple[RunState, list[str]]:
    ledger = _inc001_ledger()
    complete = [_ids(ledger, "log")[0], _ids(ledger, "metric")[0],
                _runbook_id(ledger, "connection_pool_exhaustion")]
    return build_state("INC-001", ledger), complete


# --------------------------------------------------------------------------- #
# Diagnosed                                                                   #
# --------------------------------------------------------------------------- #
def test_complete_diagnosed_report_passes(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete), state)
    assert result.ok, result.errors
    assert result.errors == []


def test_ledger_was_built_from_real_tools(inc001) -> None:
    state, complete = inc001
    assert complete[0].startswith("INC-001:L") and complete[1] == "INC-001:M:db_pool_active:1400-1500"
    assert complete[2] == "RB:connection-pool-exhaustion@1.0"
    assert all(i in state["evidence_ledger"] for i in complete)


def test_citation_not_in_ledger_fails(inc001) -> None:
    state, complete = inc001
    # A real log line of INC-001 that exists in the fixtures but was never returned in this run.
    start, end = window("INC-001")
    other = query_logs("INC-001", start, end, event="cache_refresh", limit=1).evidence[0].evidence_id
    assert other not in state["evidence_ledger"]
    result = validate_report(_report("INC-001", [*complete, other]), state)
    assert not result.ok
    assert len(result.errors) == 1 and other in result.errors[0] and "not returned" in result.errors[0]


def test_fabricated_citation_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", [*complete, "RB:made-up@9.9"]), state)
    assert not result.ok and "RB:made-up@9.9" in result.errors[0]


def test_duplicate_citation_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", [*complete, complete[0]]), state)
    assert not result.ok and any("cited 2 times" in e for e in result.errors)


def test_diagnosed_missing_metric_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", [complete[0], complete[2]]), state)
    assert not result.ok and any("at least one metric" in e for e in result.errors)


def test_diagnosed_missing_runbook_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete[:2]), state)
    assert not result.ok and any("at least one runbook" in e for e in result.errors)


def test_diagnosed_missing_log_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete[1:]), state)
    assert not result.ok and any("at least one log" in e for e in result.errors)


def test_runbook_family_mismatch_fails(inc001) -> None:
    state, complete = inc001
    other_runbook = next(i for i in _ids(state["evidence_ledger"], "runbook") if i != complete[2])
    result = validate_report(_report("INC-001", [complete[0], complete[1], other_runbook]), state)
    assert not result.ok and any("no cited runbook matches family" in e for e in result.errors)


def test_diagnosed_family_none_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete, family="none"), state)
    assert not result.ok and any("needs a family" in e for e in result.errors)


def test_wrong_incident_fails(inc001) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-003", complete), state)
    assert not result.ok and any("incident_id must be INC-001" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Inconclusive / budget_exceeded                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["inconclusive", "budget_exceeded"])
def test_partial_status_with_family_fails(inc001, status: str) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete, status=status, confidence=0.3), state)
    assert not result.ok and any("must use family 'none'" in e for e in result.errors)


@pytest.mark.parametrize("status", ["inconclusive", "budget_exceeded"])
def test_partial_status_high_confidence_fails(inc001, status: str) -> None:
    state, complete = inc001
    result = validate_report(_report("INC-001", complete, status=status, family="none", confidence=0.9), state)
    assert not result.ok and any("confidence must be <= 0.5" in e for e in result.errors)


@pytest.mark.parametrize("status", ["inconclusive", "budget_exceeded"])
def test_partial_status_valid_passes(inc001, status: str) -> None:
    state, complete = inc001
    # partial reports need no minimum evidence mix
    result = validate_report(_report("INC-001", complete[:1], status=status, family="none", confidence=0.5), state)
    assert result.ok, result.errors


def test_inconclusive_out_of_scope_incident_passes() -> None:
    start, end = window("INC-005")
    ledger = ledger_from(query_logs("INC-005", start, end, level="ERROR", limit=5),
                         query_metrics("INC-005", "mem_used_pct", start, end))
    report = _report("INC-005", list(ledger)[:2], status="inconclusive", family="none", confidence=0.2)
    assert validate_report(report, build_state("INC-005", ledger)).ok


# --------------------------------------------------------------------------- #
# Untrusted content                                                           #
# --------------------------------------------------------------------------- #
def test_citing_flagged_injection_fails() -> None:
    start, end = window("INC-002")
    injection = query_logs("INC-002", start, end, event="request_validation_failed")
    ledger = ledger_from(
        injection,
        query_logs("INC-002", start, end, event="write_failed", limit=2),
        query_metrics("INC-002", "disk_used_pct", start, end),
        search_runbooks("No space left on device", top_k=1),
    )
    injection_id = injection.evidence[0].evidence_id
    assert ledger[injection_id].untrusted_flags
    good = [_ids(ledger, "metric")[0], _runbook_id(ledger, "disk_exhaustion"),
            next(i for i in _ids(ledger, "log") if i != injection_id)]
    state = build_state("INC-002", ledger)

    clean = _report("INC-002", good, family="disk_exhaustion")
    assert validate_report(clean, state).ok

    tainted = _report("INC-002", [*good, injection_id], family="disk_exhaustion")
    result = validate_report(tainted, state)
    assert not result.ok
    assert result.errors == [f"evidence_id {injection_id!r} contains flagged instruction-like "
                             "text and cannot be cited; remove it"]


# --------------------------------------------------------------------------- #
# Error text                                                                  #
# --------------------------------------------------------------------------- #
def test_errors_are_bounded_and_actionable(inc001) -> None:
    state, complete = inc001
    long_id = "INC-001:L" + "9" * 500
    result = validate_report(_report("INC-001", [long_id]), state)
    assert not result.ok
    assert all(len(e) < 300 for e in result.errors)
    assert any("cite only IDs from tool results" in e for e in result.errors)
