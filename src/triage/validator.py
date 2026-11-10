"""Report schema and citation validation."""

from __future__ import annotations

import re
from collections import Counter

from triage.schemas import DraftReport, Evidence, RunState, StrictModel

MAX_ID_ECHO = 80
MAX_PARTIAL_CONFIDENCE = 0.5

# Metric evidence IDs look like INC-003:M:db_pool_active:1800-1900.
_METRIC_ID_RE = re.compile(r"^INC-\d{3}:M:([a-z0-9_]+):\d{4}-\d{4}$")

# Metrics a diagnosis has to cite to rule out the look-alike family, with what each one shows.
RULE_OUT_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "connection_pool_exhaustion": (("db_query_latency_p95_ms", "shows the database itself is healthy"),),
    "dependency_failure": (("db_query_latency_p95_ms", "shows the database is slow"),
                           ("db_pool_active", "shows the app pool is not saturated")),
}


def _cited_metric_names(cited: list[Evidence]) -> set[str]:
    names = set()
    for ev in cited:
        match = _METRIC_ID_RE.match(ev.evidence_id) if ev.kind == "metric" else None
        if match:
            names.add(match.group(1))
    return names


class ValidationResult(StrictModel):
    ok: bool
    errors: list[str]


def _show(evidence_id: str) -> str:
    """Cited IDs come from model output: quote them, bounded in length."""
    text = evidence_id if len(evidence_id) <= MAX_ID_ECHO else evidence_id[:MAX_ID_ECHO] + "..."
    return repr(text)


def validate_report(report: DraftReport, state: RunState) -> ValidationResult:
    """Check a schema-valid draft against this run's evidence ledger and status rules.

    Error strings are short and actionable; they are fed back to the model on repair.

    The rule-out check for connection_pool_exhaustion and dependency_failure only confirms
    that the required metrics were retrieved and cited. It does not check that their values
    support the claim.
    """
    errors: list[str] = []
    ledger: dict[str, Evidence] = state["evidence_ledger"]

    # a. incident scope
    if report.incident_id != state["incident_id"]:
        errors.append(f"incident_id must be {state['incident_id']} (the incident of this run)")

    # b. citations resolve in THIS run; c. no duplicates
    cited_ids = [c.evidence_id for c in report.cited_evidence]
    for evidence_id in dict.fromkeys(cited_ids):
        if evidence_id not in ledger:
            errors.append(f"cited evidence_id {_show(evidence_id)} was not returned by a tool "
                          "in this run; cite only IDs from tool results")
    for evidence_id, count in Counter(cited_ids).items():
        if count > 1:
            errors.append(f"evidence_id {_show(evidence_id)} is cited {count} times; cite each ID once")

    cited = [ledger[i] for i in dict.fromkeys(cited_ids) if i in ledger]
    kinds = {ev.kind for ev in cited}

    # d-f. status rules
    if report.status == "diagnosed":
        if report.family == "none":
            errors.append("a diagnosed report needs a family; use status 'inconclusive' if the "
                          "evidence does not identify one")
        for kind in ("log", "metric", "runbook"):
            if kind not in kinds:
                errors.append(f"a diagnosed report must cite at least one {kind} evidence item; "
                              f"query it and cite the returned ID")
        runbook_families = {ev.data.get("family") for ev in cited if ev.kind == "runbook"}
        if "runbook" in kinds and report.family != "none" and report.family not in runbook_families:
            errors.append(f"no cited runbook matches family {report.family!r}; cite that family's "
                          "runbook or change the family")
        needed = RULE_OUT_METRICS.get(report.family, ())
        have = _cited_metric_names(cited)
        for metric, why in needed:
            if metric not in have:
                errors.append(f"a {report.family} diagnosis must cite metric {metric} ({why}); "
                              "query it and cite the returned ID")
    elif report.family != "none":
        errors.append(f"status {report.status!r} must use family 'none'; only 'diagnosed' "
                      "reports name a family")

    # g. flagged content cannot support a conclusion
    for ev in cited:
        if ev.untrusted_flags:
            errors.append(f"evidence_id {_show(ev.evidence_id)} contains flagged instruction-like "
                          "text and cannot be cited; remove it")

    # h. partial reports stay low-confidence
    if report.status in ("inconclusive", "budget_exceeded") and report.confidence > MAX_PARTIAL_CONFIDENCE:
        errors.append(f"confidence must be <= {MAX_PARTIAL_CONFIDENCE} for status {report.status!r}")

    return ValidationResult(ok=not errors, errors=errors)
