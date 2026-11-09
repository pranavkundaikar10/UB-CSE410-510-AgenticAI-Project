"""Shared test helpers: run states and evidence ledgers built from real tool calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from triage import fixtures_store
from triage.config import MAX_MODEL_TURNS
from triage.errors import SchemaError
from triage.guardrails import check_tool_call, flag_evidence, wrap_untrusted
from triage.llm import LLM, build_system_prompt, build_user_message, parse_action
from triage.schemas import DraftReport, Evidence, FinalReportAction, RunState, ToolResult
from triage.tools import READ_TOOLS


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


# --------------------------------------------------------------------------- #
# Minimal turn loop (test stand-in until the graph exists)                    #
# --------------------------------------------------------------------------- #
@dataclass
class Outcome:
    state: RunState
    messages: list[dict[str, Any]]
    transcript: list[tuple[str, Any, Any]] = field(default_factory=list)
    report: DraftReport | None = None
    halt_reason: str | None = None
    budget_exhausted: bool = False


def drive(llm: LLM, incident_id: str, question: str = "What caused this incident?") -> Outcome:
    """Run model turns against REAL tools, wrapping each result with wrap_untrusted."""
    state = build_state(incident_id, question=question)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_message(incident_id, question,
                                                       state["window_start"], state["window_end"])},
    ]
    out = Outcome(state=state, messages=messages)
    for _ in range(MAX_MODEL_TURNS):
        state["model_turns"] += 1
        raw = llm.complete(messages)
        messages.append({"role": "assistant", "content": raw})
        try:
            action = parse_action(raw)
        except SchemaError as exc:
            out.transcript.append(("schema_error", raw, str(exc)))
            messages.append({"role": "user", "content": f"Invalid reply: {exc}"})
            continue
        if isinstance(action, FinalReportAction):
            out.transcript.append(("final_report", action, None))
            out.report = action.report
            return out
        decision = check_tool_call(action, state)
        if decision.halt:
            out.transcript.append(("halt", action, decision))
            out.halt_reason = decision.reason
            return out
        if not decision.allowed:
            out.transcript.append(("refused", action, decision))
            messages.append({"role": "user", "content": f"Tool call refused: {decision.reason}"})
            continue
        result = flag_evidence(READ_TOOLS[action.tool].func(**action.args))
        state["tool_calls"] += 1
        state["evidence_ledger"].update({ev.evidence_id: ev for ev in result.evidence})
        out.transcript.append(("tool", action, result))
        messages.append({"role": "user", "content": wrap_untrusted(result)})
    out.budget_exhausted = True
    return out
