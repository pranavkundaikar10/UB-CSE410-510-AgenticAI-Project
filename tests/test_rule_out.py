"""Rule-out metrics: connection_pool_exhaustion and dependency_failure need them cited."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import build_state, ledger_from, window
from triage.schemas import DraftReport
from triage.tools.logs import query_logs
from triage.tools.metrics import query_metrics
from triage.tools.runbooks import search_runbooks
from triage.validator import validate_report

LATENCY = "db_query_latency_p95_ms"
POOL = "db_pool_active"
FAMILY_RUNBOOK = {"connection_pool_exhaustion": "RB:connection-pool-exhaustion@1.0",
                  "dependency_failure": "RB:dependency-failure@1.0"}
INCIDENT = {"connection_pool_exhaustion": "INC-001", "dependency_failure": "INC-003"}


def _ledger(incident: str, family: str):
    start, end = window(incident)
    return ledger_from(
        query_logs(incident, start, end, limit=3),
        query_metrics(incident, POOL, start, end),
        query_metrics(incident, LATENCY, start, end),
        query_metrics(incident, "error_rate_pct", start, end),
        search_runbooks("connection pool timeout slow queries statement timeout", top_k=4))


def _report(incident: str, family: str, cite: list[str], **overrides: Any) -> DraftReport:
    data = dict(incident_id=incident, status="diagnosed", family=family,
                summary="s", root_cause="c", confidence=0.8, next_steps=["n"],
                cited_evidence=[{"evidence_id": i, "reason": "r"} for i in cite])
    data.update(overrides)
    return DraftReport.model_validate(data)


def _metric_id(ledger, name: str) -> str:
    return next(i for i, ev in ledger.items() if ev.kind == "metric" and f":M:{name}:" in i)


def _base(ledger, family: str) -> list[str]:
    """One log and the family runbook; metrics are added by each test."""
    log = next(i for i, ev in ledger.items() if ev.kind == "log")
    assert FAMILY_RUNBOOK[family] in ledger
    return [log, FAMILY_RUNBOOK[family]]


@pytest.mark.parametrize("family,metrics", [
    ("connection_pool_exhaustion", [LATENCY]),
    ("connection_pool_exhaustion", [POOL, LATENCY]),
    ("dependency_failure", [LATENCY, POOL]),
])
def test_diagnosis_with_rule_out_metrics_passes(family, metrics) -> None:
    incident = INCIDENT[family]
    ledger = _ledger(incident, family)
    cite = _base(ledger, family) + [_metric_id(ledger, m) for m in metrics]
    result = validate_report(_report(incident, family, cite), build_state(incident, ledger))
    assert result.ok, result.errors


def test_pool_diagnosis_without_latency_metric_fails() -> None:
    ledger = _ledger("INC-001", "connection_pool_exhaustion")
    cite = _base(ledger, "connection_pool_exhaustion") + [_metric_id(ledger, POOL)]
    result = validate_report(_report("INC-001", "connection_pool_exhaustion", cite),
                             build_state("INC-001", ledger))
    assert not result.ok
    assert len(result.errors) == 1 and LATENCY in result.errors[0]


def test_retrieved_but_uncited_metric_does_not_count() -> None:
    ledger = _ledger("INC-001", "connection_pool_exhaustion")
    assert _metric_id(ledger, LATENCY) in ledger
    cite = _base(ledger, "connection_pool_exhaustion") + [_metric_id(ledger, "error_rate_pct")]
    result = validate_report(_report("INC-001", "connection_pool_exhaustion", cite),
                             build_state("INC-001", ledger))
    assert not result.ok and any(LATENCY in e for e in result.errors)


@pytest.mark.parametrize("have,missing", [([LATENCY], POOL), ([POOL], LATENCY), ([], None)])
def test_dependency_failure_needs_both_metrics(have, missing) -> None:
    ledger = _ledger("INC-003", "dependency_failure")
    cite = _base(ledger, "dependency_failure") + [_metric_id(ledger, m) for m in have]
    if not have:
        cite.append(_metric_id(ledger, "error_rate_pct"))
    result = validate_report(_report("INC-003", "dependency_failure", cite),
                             build_state("INC-003", ledger))
    assert not result.ok
    for name in (LATENCY, POOL):
        mentioned = any(f"metric {name}" in e for e in result.errors)
        assert mentioned == (name not in have)


def test_other_families_and_statuses_are_not_checked() -> None:
    # Disk exhaustion cites no latency or pool metric and still passes.
    start, end = window("INC-002")
    ledger = ledger_from(query_logs("INC-002", start, end, level="ERROR", limit=2),
                         query_metrics("INC-002", "disk_used_pct", start, end),
                         search_runbooks("No space left on device disk full", top_k=2))
    cite = [next(i for i, e in ledger.items() if e.kind == k) for k in ("log", "metric")]
    cite.append("RB:disk-exhaustion@1.0")
    state = build_state("INC-002", ledger)
    assert validate_report(_report("INC-002", "disk_exhaustion", cite), state).ok
    # An inconclusive report with family none is not subject to the rule either.
    assert validate_report(_report("INC-002", "none", [], status="inconclusive", confidence=0.3),
                           state).ok


def test_only_metric_ids_with_the_exact_format_count() -> None:
    ledger = _ledger("INC-001", "connection_pool_exhaustion")
    odd = ledger[_metric_id(ledger, LATENCY)].model_copy(
        update={"evidence_id": f"INC-001:M:{LATENCY}_extra:1400-1500"})
    ledger = {k: v for k, v in ledger.items() if ":M:" + LATENCY + ":" not in k}
    ledger[odd.evidence_id] = odd
    cite = _base(ledger, "connection_pool_exhaustion") + [odd.evidence_id]
    result = validate_report(_report("INC-001", "connection_pool_exhaustion", cite),
                             build_state("INC-001", ledger))
    assert not result.ok and any(LATENCY in e for e in result.errors)
