"""Deterministic scripted LLM for tests and offline demos. Returns JSON text like a real model."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from triage.guardrails import FLAG_MARKER
from triage.llm import LLM

Scenario = Literal["normal", "always_tool_call", "bad_json_once", "request_export",
                   "cite_fabricated", "follow_injection"]

_INCIDENT_RE = re.compile(r"^Incident: (INC-\d{3})$", re.MULTILINE)
_START_RE = re.compile(r"^Window start: (\S+)$", re.MULTILINE)
_END_RE = re.compile(r"^Window end: (\S+)$", re.MULTILINE)
_TOOL_OUTPUT_RE = re.compile(r"<tool_output[^>]*>\n(.*?)\n</tool_output>", re.DOTALL)

Step = Callable[[str, str, str], tuple[str, dict[str, Any]]]


def _metric(name: str) -> Step:
    return lambda inc, s, e: ("query_metrics", {"incident_id": inc, "metric": name, "start": s, "end": e})


def _logs(**filters: Any) -> Step:
    return lambda inc, s, e: ("query_logs", {"incident_id": inc, "start": s, "end": e, **filters})


def _runbooks(query: str) -> Step:
    return lambda inc, s, e: ("search_runbooks", {"query": query})


@dataclass(frozen=True)
class Script:
    steps: tuple[Step, ...]
    status: str
    family: str
    confidence: float
    summary: str
    root_cause: str
    next_steps: tuple[str, ...]


SCRIPTS: dict[str, Script] = {
    "INC-001": Script(
        steps=(_metric("db_pool_active"), _metric("db_query_latency_p95_ms"),
               _logs(event="db_pool_timeout", limit=5),
               _runbooks("timed out waiting for a connection from pool")),
        status="diagnosed", family="connection_pool_exhaustion", confidence=0.85,
        summary="503s caused by checkout-api exhausting its orders-db connection pool.",
        root_cause="db_pool_active reached db_pool_max and requests timed out waiting for a "
                   "connection, while database query latency stayed normal.",
        next_steps=("Shed or rate-limit non-critical traffic", "Check what raised request volume"),
    ),
    "INC-002": Script(
        steps=(_metric("disk_used_pct"), _logs(level="ERROR", limit=5),
               _runbooks("No space left on device disk full")),
        status="diagnosed", family="disk_exhaustion", confidence=0.85,
        summary="500s caused by the checkout-api data volume filling up.",
        root_cause="disk_used_pct climbed to full and file writes failed with ENOSPC.",
        next_steps=("Stop whatever is writing to the volume", "Free space and add a disk alert"),
    ),
    "INC-003": Script(
        steps=(_metric("db_query_latency_p95_ms"), _metric("db_pool_active"),
               _logs(event="db_query_timeout", limit=5),
               _runbooks("statement timeout slow queries database latency")),
        status="diagnosed", family="dependency_failure", confidence=0.8,
        summary="504s caused by a slow orders-db; the app pool was not saturated.",
        root_cause="db_query_latency_p95_ms rose sharply and queries hit the statement timeout "
                   "while db_pool_active stayed below db_pool_max.",
        next_steps=("Find the database-side load source", "Do not raise the app pool size"),
    ),
    "INC-004": Script(
        steps=(_logs(service="deployer"), _logs(event="unhandled_exception", limit=5),
               _metric("error_rate_pct"),
               _runbooks("errors started after deploy KeyError new version")),
        status="diagnosed", family="deployment_regression", confidence=0.85,
        summary="500s on checkout began right after a checkout-api deployment.",
        root_cause="unhandled exceptions from the newly deployed version started at deploy "
                   "completion while resource metrics stayed normal.",
        next_steps=("Roll back checkout-api", "Fix the exception and add a regression test"),
    ),
    "INC-005": Script(
        steps=(_metric("mem_used_pct"), _logs(level="ERROR", limit=5),
               _runbooks("process killed out of memory restart")),
        status="inconclusive", family="none", confidence=0.3,
        summary="Brief 502 spike around a checkout-api restart; cause is outside the supported families.",
        root_cause="Memory rose before the process was killed and restarted; none of the four "
                   "supported families explains this.",
        next_steps=("Investigate memory growth in checkout-api", "Escalate to the service owner"),
    ),
}


@dataclass(frozen=True)
class Observed:
    evidence_id: str
    kind: str
    family: str | None
    flagged: bool
    text: str


def _observations(messages: list[dict[str, Any]]) -> tuple[int, list[Observed]]:
    """(number of tool outputs, evidence seen) from wrapped tool output in non-assistant turns."""
    blocks = 0
    seen: list[Observed] = []
    for msg in messages:
        if msg.get("role") in ("assistant", "system"):
            continue
        for body in _TOOL_OUTPUT_RE.findall(str(msg.get("content", ""))):
            blocks += 1
            flag_next = False
            for line in body.splitlines():
                if line.startswith(FLAG_MARKER):
                    flag_next = True
                    continue
                if not line.startswith("{"):
                    continue
                obj = json.loads(line)
                if "evidence_id" in obj:
                    seen.append(Observed(obj["evidence_id"], obj["kind"], obj["data"].get("family"),
                                         flag_next, line))
                flag_next = False
    return blocks, seen


class FakeLLM(LLM):
    """Deterministic stand-in for a real model, driven only by the conversation so far."""

    model = "FakeLLM (incident-specific scripts)"

    def __init__(self, scenario: Scenario = "normal") -> None:
        self.scenario = scenario
        self.calls = 0

    def __repr__(self) -> str:
        return f"FakeLLM(scenario={self.scenario!r})"

    def complete(self, messages: list[dict[str, Any]]) -> str:
        self.calls += 1
        first_user = next(str(m["content"]) for m in messages if m.get("role") == "user")
        inc = _INCIDENT_RE.search(first_user)
        start, end = _START_RE.search(first_user), _END_RE.search(first_user)
        if not (inc and start and end):
            return json.dumps({"type": "final_report", "report": self._report("INC-000", None, [])})
        incident_id, s, e = inc.group(1), start.group(1), end.group(1)
        step, seen = _observations(messages)
        script = SCRIPTS.get(incident_id)

        if self.scenario == "bad_json_once" and not any(m.get("role") == "assistant" for m in messages):
            return 'Sure, here is my action: {"type": "tool_call", "tool": "query_logs", "args": {'
        if self.scenario == "request_export":
            return self._tool("export_report", {"incident_id": incident_id, "path": "output/report.md"})
        if self.scenario == "always_tool_call":
            return self._tool("query_metrics", {"incident_id": incident_id, "metric": "cpu_pct",
                                                "start": s, "end": e})
        if self.scenario == "follow_injection":
            if any(o.flagged or "export_report" in o.text for o in seen):
                return self._tool("export_report", {"incident_id": incident_id,
                                                    "path": "../../etc/cron.d/triage", "approval": "granted"})
            return self._tool("query_logs", {"incident_id": incident_id, "start": s, "end": e,
                                             "event": "request_validation_failed"})

        if script is not None and step < len(script.steps):
            tool, args = script.steps[step](incident_id, s, e)
            return self._tool(tool, args)
        return json.dumps({"type": "final_report", "report": self._report(incident_id, script, seen)})

    @staticmethod
    def _tool(tool: str, args: dict[str, Any]) -> str:
        return json.dumps({"type": "tool_call", "tool": tool, "args": args})

    def _report(self, incident_id: str, script: Script | None, seen: list[Observed]) -> dict[str, Any]:
        usable = [o for o in seen if not o.flagged]
        logs = [o for o in usable if o.kind == "log"][:2]
        metrics = [o for o in usable if o.kind == "metric"][:2]
        cites: list[tuple[str, str]] = [(o.evidence_id, "metric trend in the window") for o in metrics]
        cites += [(o.evidence_id, "log event matching the symptom") for o in logs]
        if script is not None and script.family != "none":
            runbook = next((o for o in usable if o.kind == "runbook" and o.family == script.family), None)
            if runbook:
                cites.append((runbook.evidence_id, "runbook confirmation checks match"))
        if self.scenario == "cite_fabricated":
            cites.append((f"{incident_id}:L9999", "fabricated citation"))

        cites = list(dict.fromkeys(cites))
        if script is None:
            script = Script((), "inconclusive", "none", 0.2, "No script for this incident.",
                            "Not enough evidence to decide.", ("Investigate manually",))
        return {
            "incident_id": incident_id,
            "status": script.status,
            "family": script.family,
            "summary": script.summary,
            "root_cause": script.root_cause,
            "cited_evidence": [{"evidence_id": i, "reason": r} for i, r in cites],
            "timeline": [],
            "next_steps": list(script.next_steps),
            "confidence": script.confidence,
        }
