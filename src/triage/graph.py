"""Bounded single-agent LangGraph investigation; one tool per model action."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from triage.config import MAX_MODEL_TURNS, MAX_TOOL_CALLS
from triage.errors import ConfigError, SchemaError, TransientLLMError, UnknownIncident
from triage.fixtures_store import get_incident
from triage.guardrails import check_tool_call, flag_evidence, wrap_untrusted
from triage.llm import (GatewayLLM, LLM, build_system_prompt, build_user_message,
                        extract_json_objects, parse_action)
from triage.paths import LOGS_DIR
from triage.progress import start_line
from triage.schemas import DraftReport, FinalReportAction, RunState, ToolCallAction
from triage.tools import READ_TOOLS
from triage.tools.common import WindowArgs, format_validation_error
from triage.trace import TraceWriter
from triage.validator import validate_report


class WorkflowState(RunState):
    action: ToolCallAction | FinalReportAction | None
    route: str
    model_retry: int
    tool_retry: int
    repairs: int
    empty_replies: int  # consecutive empty model replies
    validation_errors: list[str]


@dataclass
class Outcome:
    state: WorkflowState
    trace_path: Path

    @property
    def report(self) -> DraftReport | None:
        """Only a validated draft is ever exposed as the result."""
        return self.state["draft"]

    @property
    def status(self) -> str:
        return self.state["status"]

    def render(self) -> str:
        lines = [f"Terminal status: {self.status}"]
        if self.state["halt_reason"]:
            lines.append(self.state["halt_reason"])
        if self.report:
            r = self.report
            lines += ["DRAFT — requires human review; unapproved", f"{r.status}: {r.family}",
                      r.summary, f"Cause: {r.root_cause}", f"Confidence: {r.confidence:g}"]
            lines += [f"Evidence: {c.evidence_id} — {c.reason}" for c in r.cited_evidence]
            lines += [f"Timeline: {item}" for item in r.timeline]
            lines += [f"Next: {item}" for item in r.next_steps]
        lines += [f"Attempts: model={self.state['model_turns']}/6, tools={self.state['tool_calls']}/7",
                  f"Trace: {self.trace_path}"]
        return "\n".join(lines)


EMPTY_REPLY_REASON = ("The model returned empty message text twice. If your gateway returns "
                      "reasoning without content, set LLM_JSON_MODE=1.")
MAX_REPAIR_IDS_PER_KIND = 15


def repair_message(state: WorkflowState) -> str:
    """The one-shot repair request: the errors, the usable evidence IDs and the ground rules."""
    by_kind: dict[str, list[str]] = {"log": [], "metric": [], "runbook": []}
    for evidence_id, ev in state["evidence_ledger"].items():
        if not ev.untrusted_flags:  # flagged items can't be cited anyway
            by_kind[ev.kind].append(evidence_id)
    groups = []
    for kind, ids in by_kind.items():
        shown = ", ".join(ids[:MAX_REPAIR_IDS_PER_KIND]) or "none"
        if len(ids) > MAX_REPAIR_IDS_PER_KIND:
            shown += f" (+{len(ids) - MAX_REPAIR_IDS_PER_KIND} more)"
        groups.append(f"{kind}: {shown}")
    return ("Repair the report once. Return only a final_report using evidence already returned. "
            "Validation errors: " + "; ".join(state["validation_errors"]) + "\n"
            "Evidence IDs you can cite - " + "; ".join(groups) + "\n"
            "Tools cannot be called during repair; a tool call ends the run. If a required kind of "
            "evidence is missing, return status inconclusive, family none, confidence <= 0.5, and say "
            "what is missing; that is a valid outcome.")


def build_graph(llm: LLM, trace: TraceWriter):
    """Compile explicit model → guard → execute → model / validate → repair edges."""
    def stop(s: WorkflowState, status: str, reason: str) -> dict[str, Any]:
        return {"status": status, "halt_reason": reason, "route": "finish"}

    def model(s: WorkflowState) -> dict[str, Any]:
        if s["model_turns"] >= MAX_MODEL_TURNS:
            return stop(s, "budget_exceeded", "Model attempt budget exhausted.")
        s["model_turns"] += 1
        started = time.perf_counter()
        json_mode = getattr(llm, "json_mode", None)  # None for models without the setting
        try:
            raw = llm.complete(s["messages"])
        except TransientLLMError:
            retry = s["model_retry"] == 0 and s["model_turns"] < MAX_MODEL_TURNS
            trace.event("model", "transient_failure", s, retry=retry, usage=None, cost=None, json_mode=json_mode,
                        duration_ms=(time.perf_counter() - started) * 1000)
            if s["model_turns"] >= MAX_MODEL_TURNS:
                return stop(s, "budget_exceeded", "Model attempt budget exhausted during retry.")
            if retry:
                return {"model_retry": 1, "route": "model"}
            return stop(s, "model_failed", "Model unavailable after one retry.")
        except ConfigError:
            trace.event("model", "configuration_failure", s, usage=None, cost=None, json_mode=json_mode,
                        duration_ms=(time.perf_counter() - started) * 1000)
            return stop(s, "config_error", "Gateway rejected configuration; check LLM settings and access.")
        except Exception:
            trace.event("model", "unexpected_failure", s, usage=None, cost=None, json_mode=json_mode,
                        duration_ms=(time.perf_counter() - started) * 1000)
            return stop(s, "model_failed", "Unexpected model failure (details withheld to protect credentials).")
        trace.event("model", "completed", s, usage=getattr(llm, "last_usage", None), cost=None,
                    json_mode=json_mode, response_chars=len(raw),
                    duration_ms=(time.perf_counter() - started) * 1000)
        s["messages"].append({"role": "assistant", "content": raw})
        if raw.strip():
            s["empty_replies"] = 0
        else:
            # Empty text still used a model attempt. Twice in a row means the gateway is not
            # returning message content, and more turns would only burn the budget.
            s["empty_replies"] += 1
            trace.event("parse", "empty_reply", s, consecutive=s["empty_replies"])
            if s["empty_replies"] >= 2:
                return stop(s, "model_failed", EMPTY_REPLY_REASON)
        try:
            action = parse_action(raw)
        except SchemaError:
            trace.event("parse", "invalid_action", s)
            if s["repairs"]:
                return stop(s, "validation_failed", "Report repair did not produce a valid report action.")
            objects = extract_json_objects(raw)
            if len(objects) == 1 and objects[0].get("type") == "final_report":
                if s["model_turns"] >= MAX_MODEL_TURNS:
                    return stop(s, "budget_exceeded", "No model budget remains for report repair.")
                return {"validation_errors": ["Report does not match the final_report JSON schema."],
                        "model_retry": 0, "route": "repair"}
            s["messages"].append({"role": "user", "content":
                "Invalid action. Reply with exactly one JSON object matching the action schema."})
            return {"model_retry": 0, "route": "model"}
        return {"action": action, "model_retry": 0,
                "route": "validate" if isinstance(action, FinalReportAction) else "guard"}

    def guard(s: WorkflowState) -> dict[str, Any]:
        action = s["action"]
        assert isinstance(action, ToolCallAction)
        decision = check_tool_call(action, s)
        if decision.halt:
            trace.event("guard", "blocked", s, reason=decision.reason)
            return stop(s, "halted", decision.reason)
        if s["repairs"]:
            return stop(s, "validation_failed", "Report repair must return a final_report action.")
        if s["tool_calls"] >= MAX_TOOL_CALLS:
            return stop(s, "budget_exceeded", "Read-tool attempt budget exhausted.")
        if not decision.allowed:
            # Invalid requests count, but deterministic errors never dispatch or retry.
            s["tool_calls"] += 1
            trace.event("guard", "invalid_arguments", s, tool=action.tool, reason=decision.reason)
            s["messages"].append({"role": "user", "content": f"Tool refused: {decision.reason}"})
            return {"route": "model"}
        trace.event("guard", "allowed", s, tool=action.tool, args=action.args)
        return {"tool_retry": 0, "route": "execute"}

    def execute(s: WorkflowState) -> dict[str, Any]:
        action = s["action"]
        assert isinstance(action, ToolCallAction)
        if s["tool_calls"] >= MAX_TOOL_CALLS:
            return stop(s, "budget_exceeded", "Read-tool attempt budget exhausted during retry.")
        s["tool_calls"] += 1
        started = time.perf_counter()
        try:
            result = flag_evidence(READ_TOOLS[action.tool].func(**action.args))
        except (TimeoutError, ConnectionError):
            retry = s["tool_retry"] == 0 and s["tool_calls"] < MAX_TOOL_CALLS
            trace.event("tool", "transient_failure", s, tool=action.tool, args=action.args,
                        retry=retry, duration_ms=(time.perf_counter() - started) * 1000)
            if s["tool_calls"] >= MAX_TOOL_CALLS:
                return stop(s, "budget_exceeded", "Read-tool attempt budget exhausted during retry.")
            if retry:
                return {"tool_retry": 1, "route": "execute"}
            return stop(s, "tool_failed", "Read tool unavailable after one retry.")
        except Exception:
            trace.event("tool", "failure", s, tool=action.tool, args=action.args,
                        error="Tool failed; raw exception withheld.",
                        duration_ms=(time.perf_counter() - started) * 1000)
            return stop(s, "tool_failed", "Read tool failed; inspect fixture integrity and configuration.")
        if result.ok:
            s["evidence_ledger"].update({ev.evidence_id: ev for ev in result.evidence})
        s["messages"].append({"role": "user", "content": wrap_untrusted(result)})
        trace.event("tool", "ok" if result.ok else "error", s, tool=action.tool,
                    args=action.args, result=result.model_dump(mode="json"),
                    evidence_count=len(result.evidence),
                    flagged_count=sum(bool(ev.untrusted_flags) for ev in result.evidence),
                    duration_ms=(time.perf_counter() - started) * 1000)
        return {"route": "model"}

    def validate(s: WorkflowState) -> dict[str, Any]:
        action = s["action"]
        assert isinstance(action, FinalReportAction)
        result = validate_report(action.report, s)
        if action.report.status == "budget_exceeded":
            result.ok = False
            result.errors.append("Model reports must use diagnosed or inconclusive.")
        trace.event("validate", "valid" if result.ok else "invalid", s,
                    errors=result.errors, repair_attempt=s["repairs"])
        if result.ok:
            return {"draft": action.report, "status": "draft_ready", "route": "finish"}
        if s["repairs"]:
            return {**stop(s, "validation_failed", "Report validation failed after one repair."),
                    "validation_errors": result.errors}
        if s["model_turns"] >= MAX_MODEL_TURNS:
            return stop(s, "budget_exceeded", "No model budget remains for report repair.")
        return {"validation_errors": result.errors, "route": "repair"}

    def repair(s: WorkflowState) -> dict[str, Any]:
        s["messages"].append({"role": "user", "content": repair_message(s)})
        trace.event("repair", "requested", s)
        return {"repairs": 1, "route": "model"}

    def finish(s: WorkflowState) -> dict[str, Any]:
        report = s["draft"]
        if report is None and s["status"] in {"budget_exceeded", "model_failed", "tool_failed", "config_error"}:
            report = DraftReport(incident_id=s["incident_id"],
                status="budget_exceeded" if s["status"] == "budget_exceeded" else "inconclusive",
                family="none", summary="Investigation incomplete: " + (s["halt_reason"] or "failure"),
                root_cause="Not established from the available evidence.", confidence=0,
                next_steps=["Resolve the reported limitation and start a new investigation."])
            checked = validate_report(report, s)
            trace.event("validate_partial", "valid" if checked.ok else "invalid", s, errors=checked.errors)
            if not checked.ok:
                report = None
        trace.event("terminal", s["status"], s, reason=s["halt_reason"],
                    report=report.model_dump(mode="json") if report else None,
                    evidence_count=len(s["evidence_ledger"]), approved=False, exported=False)
        return {"draft": report}

    graph = StateGraph(WorkflowState)
    nodes = {"model": model, "guard": guard, "execute": execute, "validate": validate,
             "repair": repair, "finish": finish}
    def tracked(name, node):
        def transition(s: WorkflowState) -> dict[str, Any]:
            s["steps"].append(name)
            s.update(node(s))
            return dict(s)
        return transition

    for name, node in nodes.items():
        graph.add_node(name, tracked(name, node))
    graph.add_edge(START, "model")
    routes = {"model": ["model", "guard", "validate", "repair", "finish"],
              "guard": ["model", "execute", "finish"],
              "execute": ["execute", "model", "finish"],
              "validate": ["repair", "finish"], "repair": ["model"]}
    for name, destinations in routes.items():
        graph.add_conditional_edges(name, lambda s: s["route"],
                                    {dest: dest for dest in destinations})
    graph.add_edge("finish", END)
    return graph.compile()


def investigate(incident_id: str, question: str, *, llm: LLM | None = None,
                start: str | None = None, end: str | None = None,
                mode: str = "real-provider", trace_dir: Path = LOGS_DIR,
                progress: Callable[[str], None] | None = None) -> Outcome:
    """Validate input before model construction or dispatch. Failures also have traces.

    `progress`, if given, receives one short status line per step (see triage.progress).
    """
    run_id = uuid4().hex
    state: WorkflowState = dict(run_id=run_id, incident_id=incident_id, question=question,
        window_start="", window_end="", messages=[], evidence_ledger={}, model_turns=0,
        tool_calls=0, draft=None, status="running", halt_reason=None, approval=None,
        steps=[], action=None, route="model", model_retry=0, tool_retry=0, repairs=0,
        empty_replies=0, validation_errors=[])
    settings = getattr(llm, "_settings", None)
    trace = TraceWriter(trace_dir / f"{run_id}.jsonl", run_id, mode,
                        getattr(llm, "model", None), (getattr(settings, "LLM_API_KEY", None),),
                        progress=progress)
    try:
        trace.event("start", "running", state, incident_id=incident_id,
                    trace_limits={"string_chars": 2000, "list_items": 10, "object_keys": 60})
        try:
            entry = get_incident(incident_id)
            window = WindowArgs.model_validate(dict(incident_id=incident_id,
                start=start if start is not None else entry.window_start,
                end=end if end is not None else entry.window_end), context={"entry": entry})
        except (UnknownIncident, ValidationError) as exc:
            reason = format_validation_error(exc) if isinstance(exc, ValidationError) else "Unknown incident ID."
            state.update(status="invalid_input", halt_reason=reason)
            trace.note(start_line(incident_id, mode, None))
            trace.event("terminal", "invalid_input", state, reason=reason, report=None)
            return Outcome(state, trace.path)
        state.update(window_start=window.start.isoformat(), window_end=window.end.isoformat())
        trace.event("input", "valid", state, start=state["window_start"], end=state["window_end"])
        if llm is None:
            try:
                llm = GatewayLLM()
            except ConfigError:
                trace.note(start_line(incident_id, mode, None))
                state.update(status="config_error", halt_reason=
                    "Gateway configuration unavailable; set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (see .env.example).")
                trace.event("terminal", "config_error", state, reason=state["halt_reason"], report=None)
                return Outcome(state, trace.path)
            trace.model = llm.model
            trace.secrets += (llm._settings.LLM_API_KEY,)
        trace.note(start_line(incident_id, mode, getattr(llm, "model", None)))
        state["messages"] = [dict(role="system", content=build_system_prompt()),
            dict(role="user", content=build_user_message(incident_id, question,
                 state["window_start"], state["window_end"]))]
        state = build_graph(llm, trace).invoke(state, config={"recursion_limit": 100})
        return Outcome(state, trace.path)
    except Exception:
        state.update(status="internal_error", draft=None,
                     halt_reason="Unexpected workflow failure; raw exception withheld.")
        trace.event("terminal", "internal_error", state, reason=state["halt_reason"], report=None)
        return Outcome(state, trace.path)
    finally:
        trace.close()
