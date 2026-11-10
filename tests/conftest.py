"""Shared test helpers: run states and evidence ledgers built from real tool calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from triage import fixtures_store
from triage.guardrails import flag_evidence
from triage.schemas import Evidence, RunState, ToolResult


def window(incident_id: str) -> tuple[str, str]:
    entry = fixtures_store.get_incident(incident_id)
    return entry.window_start.isoformat(), entry.window_end.isoformat()


def ledger_from(*results: ToolResult) -> dict[str, Evidence]:
    """Evidence ledger as the graph will build it: flagged, keyed by evidence_id."""
    ledger: dict[str, Evidence] = {}
    for result in results:
        assert result.ok, result.error
        for ev in flag_evidence(result).evidence:
            ledger[ev.evidence_id] = ev
    return ledger


def build_state(incident_id: str, ledger: dict[str, Evidence] | None = None,
                **overrides: Any) -> RunState:
    start, end = window(incident_id)
    state: RunState = {
        "run_id": "test-run",
        "incident_id": incident_id,
        "question": "What caused the incident?",
        "window_start": start,
        "window_end": end,
        "messages": [],
        "evidence_ledger": ledger or {},
        "model_turns": 0,
        "tool_calls": 0,
        "draft": None,
        "status": "running",
        "halt_reason": None,
        "approval": None,
        "steps": [],
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


@pytest.fixture
def make_state() -> Callable[..., RunState]:
    return build_state
