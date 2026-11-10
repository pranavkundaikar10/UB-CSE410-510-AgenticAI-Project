"""What the model is told on the single repair turn."""

from __future__ import annotations

import json

import pytest

from triage import graph
from triage.fake_llm import FakeLLM
from triage.graph import MAX_REPAIR_IDS_PER_KIND, repair_message
from triage.schemas import Evidence

INCONCLUSIVE = json.dumps({"type": "final_report", "report": {
    "incident_id": "INC-001", "status": "inconclusive", "family": "none",
    "summary": "A required kind of evidence is missing.", "root_cause": "Not established.",
    "confidence": 0.3, "next_steps": ["Query the missing metric."]}})


def _evidence(evidence_id: str, kind: str, incident="INC-001", flags=()) -> Evidence:
    return Evidence(evidence_id=evidence_id, kind=kind, incident_id=incident,
                    summary="x", untrusted_flags=list(flags))


def _state(*items: Evidence, errors=("something is wrong",)) -> dict:
    return {"evidence_ledger": {e.evidence_id: e for e in items}, "validation_errors": list(errors)}


def test_repair_message_lists_ledger_ids_by_kind() -> None:
    text = repair_message(_state(
        _evidence("INC-001:L0205", "log"),
        _evidence("INC-001:M:db_pool_active:1400-1500", "metric"),
        _evidence("RB:connection-pool-exhaustion@1.0", "runbook", incident=None)))
    assert "log: INC-001:L0205" in text
    assert "metric: INC-001:M:db_pool_active:1400-1500" in text
    assert "runbook: RB:connection-pool-exhaustion@1.0" in text
    assert "something is wrong" in text and "Return only a final_report" in text


def test_repair_message_says_tools_are_off_and_inconclusive_is_valid() -> None:
    text = repair_message(_state())
    assert "Tools cannot be called during repair" in text
    assert "status inconclusive, family none, confidence <= 0.5" in text
    assert "that is a valid outcome" in text
    assert "log: none" in text


def test_repair_message_skips_flagged_evidence_and_caps_the_list() -> None:
    items = [_evidence(f"INC-001:L{n:04d}", "log") for n in range(MAX_REPAIR_IDS_PER_KIND + 4)]
    items.append(_evidence("INC-001:L9000", "log", flags=["tool_name_mention"]))
    text = repair_message(_state(*items))
    assert "L9000" not in text
    assert "(+4 more)" in text


def test_repair_turn_reaches_the_model_and_inconclusive_ends_draft_ready(tmp_path) -> None:
    scripted = FakeLLM("cite_fabricated")
    seen: list[str] = []

    def respond(messages):
        if "Repair the report once" in messages[-1]["content"]:
            seen.append(messages[-1]["content"])
            return INCONCLUSIVE
        return scripted.complete(messages)

    class Llm:
        model = "test-repair"

        def complete(self, messages):
            return respond(messages)

    out = graph.investigate("INC-001", "What caused it?", llm=Llm(), trace_dir=tmp_path,
                            mode="offline-scripted")
    assert out.status == "draft_ready" and out.report.status == "inconclusive"
    assert out.state["repairs"] == 1
    assert len(seen) == 1
    assert "INC-001:M:db_pool_active:1400-1500" in seen[0] and "Tools cannot be called" in seen[0]
