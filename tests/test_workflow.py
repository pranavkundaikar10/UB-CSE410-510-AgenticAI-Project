"""Production graph and CLI integration with real fixture tools; no network calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from triage import cli, graph
from triage.errors import ConfigError, TransientLLMError
from triage.fake_llm import FakeLLM
from triage.fixtures_store import get_incident
from triage.llm import GatewayLLM
from triage.config import Settings
from triage.schemas import ToolResult
from triage.trace import TraceWriter
from triage.validator import validate_report


def events(out):
    return [json.loads(line) for line in out.trace_path.read_text().splitlines()]


@pytest.fixture
def run(tmp_path):
    def investigate(llm=None, incident="INC-001", **kwargs):
        return graph.investigate(incident, "What caused the incident?", llm=llm or FakeLLM(),
                                 trace_dir=tmp_path, mode="offline-scripted", **kwargs)
    return investigate


class SequenceLLM:
    model = "test-sequence"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else response


def tool(name="query_metrics", incident="INC-001", **changes):
    entry = get_incident(incident)
    args = dict(incident_id=incident, metric="cpu_pct",
                start=entry.window_start.isoformat(), end=entry.window_end.isoformat())
    args.update(changes)
    return json.dumps(dict(type="tool_call", tool=name, args=args))


def partial():
    return json.dumps(dict(type="final_report", report=dict(incident_id="INC-001",
        status="inconclusive", family="none", summary="Evidence is insufficient.",
        root_cause="Not established.", confidence=0.2)))


def replace_tool(monkeypatch, name, func):
    tools = dict(graph.READ_TOOLS)
    tools[name] = replace(tools[name], func=func)
    monkeypatch.setattr(graph, "READ_TOOLS", tools)


@pytest.mark.parametrize("incident,family", [
    ("INC-001", "connection_pool_exhaustion"), ("INC-002", "disk_exhaustion"),
    ("INC-003", "dependency_failure"), ("INC-004", "deployment_regression"),
    ("INC-005", "none"),
])
def test_real_tools_and_validated_draft(run, incident, family):
    out = run(incident=incident)
    assert out.status == "draft_ready"
    assert out.report.family == family
    assert out.report.status == ("inconclusive" if family == "none" else "diagnosed")
    assert validate_report(out.report, out.state).ok
    assert {c.evidence_id for c in out.report.cited_evidence} <= out.state["evidence_ledger"].keys()
    calls = [e for e in events(out) if e["event"] == "tool"]
    assert {e["tool"] for e in calls} == {"query_metrics", "query_logs", "search_runbooks"}
    assert all(e["outcome"] == "ok" for e in calls)
    assert "DRAFT" in out.render() and out.state["approval"] is None
    assert events(out)[-1]["approved"] is False and events(out)[-1]["exported"] is False


@pytest.mark.parametrize("incident,start,end", [
    ("INC-999", None, None), ("../../etc/passwd", None, None),
    ("INC-001", "garbage", None), ("INC-001", "2026-10-06T14:00:00", None),
    ("INC-001", "2026-10-06T15:00:00Z", "2026-10-06T14:00:00Z"),
    ("INC-001", "2026-10-06T13:00:00Z", None), ("INC-001", "", None),
])
def test_input_rejected_before_model_or_tool(run, incident, start, end):
    llm = SequenceLLM()
    out = run(llm, incident, start=start, end=end)
    assert out.status == "invalid_input" and out.report is None
    assert llm.calls == out.state["model_turns"] == out.state["tool_calls"] == 0
    assert events(out)[-1]["outcome"] == "invalid_input"


@pytest.mark.parametrize("name", ["export_report", "run_shell", "fetch_url", "modify_infrastructure"])
def test_forbidden_request_never_dispatches(run, monkeypatch, name):
    dispatched = []
    replace_tool(monkeypatch, "query_metrics", lambda **kw: dispatched.append(kw))
    out = run(SequenceLLM(tool(name)))
    assert out.status == "halted" and out.report is None
    assert out.state["tool_calls"] == 0 and not dispatched
    assert any(e["event"] == "guard" and e["outcome"] == "blocked" for e in events(out))
    assert not any(e["event"] == "tool" for e in events(out))


def test_foreign_incident_halts_before_dispatch(run):
    out = run(SequenceLLM(tool(incident="INC-003")))
    assert out.status == "halted" and out.state["tool_calls"] == 0


def test_narrow_requested_window_enforced(run, monkeypatch):
    dispatched = []
    replace_tool(monkeypatch, "query_metrics", lambda **kw: dispatched.append(kw))
    out = run(SequenceLLM(tool(), partial()), start="2026-10-06T14:10:00Z",
              end="2026-10-06T14:20:00Z")
    assert out.status == "draft_ready" and not dispatched
    assert out.state["tool_calls"] == 1
    assert any("requested investigation window" in e.get("reason", "") for e in events(out))


def test_malformed_action_recovers_with_feedback(run):
    out = run(FakeLLM("bad_json_once"))
    assert out.status == "draft_ready" and validate_report(out.report, out.state).ok
    assert out.state["model_turns"] == 6 and out.state["tool_calls"] == 4
    assert sum(e["event"] == "parse" for e in events(out)) == 1


def test_invalid_citations_repaired_exactly_once(run):
    scripted = FakeLLM("cite_fabricated")

    def respond(messages):
        if any("Repair the report once" in m["content"] for m in messages):
            scripted.scenario = "normal"
        return scripted.complete(messages)

    out = run(SequenceLLM(*([respond] * 6)))
    assert out.status == "draft_ready" and validate_report(out.report, out.state).ok
    assert out.state["model_turns"] == 6 and out.state["repairs"] == 1
    assert [e["outcome"] for e in events(out) if e["event"] == "validate"] == ["invalid", "valid"]


def test_repeated_invalid_citations_cannot_escape_as_diagnosis(run):
    out = run(FakeLLM("cite_fabricated"))
    assert out.status == "validation_failed" and out.report is None
    assert out.state["repairs"] == 1 and out.state["model_turns"] == 6
    assert any("L9999" in e for e in out.state["validation_errors"])
    assert "diagnosed:" not in out.render() and events(out)[-1]["report"] is None


@pytest.mark.parametrize("second", ['{"type":"final_report","report":{}}', "bad json", tool()])
def test_schema_invalid_report_has_only_one_repair(run, second):
    out = run(SequenceLLM('{"type":"final_report","report":{}}', second))
    assert out.status == "validation_failed" and out.report is None
    assert out.state["model_turns"] == 2 and out.state["repairs"] == 1
    assert out.state["tool_calls"] == 0


@pytest.mark.parametrize("response", ["invalid JSON", tool()])
def test_model_budget_returns_validated_partial(run, response):
    out = run(SequenceLLM(*([response] * 6)))
    assert out.status == "budget_exceeded" and out.report.status == "budget_exceeded"
    assert out.report.family == "none" and out.report.confidence == 0
    assert out.state["model_turns"] == 6
    assert out.state["tool_calls"] == (6 if response == tool() else 0)
    assert validate_report(out.report, out.state).ok
    assert any(e["event"] == "validate_partial" and e["outcome"] == "valid" for e in events(out))


def test_tool_retry_attempts_can_exhaust_seven_call_budget(run, monkeypatch):
    real = graph.READ_TOOLS["query_metrics"].func
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) % 2:
            raise TimeoutError("transient")
        return real(**kwargs)

    replace_tool(monkeypatch, "query_metrics", flaky)
    out = run(FakeLLM("always_tool_call"))
    assert out.status == "budget_exceeded" and len(calls) == out.state["tool_calls"] == 7
    assert out.state["model_turns"] == 4 and out.report.family == "none"
    assert validate_report(out.report, out.state).ok


def test_model_transient_recovers_once(run):
    out = run(SequenceLLM(TransientLLMError("transient"), FakeLLM().complete,
                          *([FakeLLM().complete] * 4)))
    assert out.status == "draft_ready" and out.state["model_turns"] == 6
    assert sum(e["outcome"] == "transient_failure" for e in events(out)) == 1


@pytest.mark.parametrize("failure,status,attempts", [
    (TransientLLMError("private provider payload"), "model_failed", 2),
    (ConfigError("private configuration payload"), "config_error", 1),
    (RuntimeError("private unexpected payload"), "model_failed", 1),
])
def test_persistent_model_failure_is_safe_partial(run, failure, status, attempts):
    out = run(SequenceLLM(failure, failure, partial()))
    assert out.status == status and out.state["model_turns"] == attempts
    assert out.report.status == "inconclusive" and out.report.family == "none"
    assert validate_report(out.report, out.state).ok
    assert str(failure) not in out.trace_path.read_text() + out.render()


def test_tool_transient_recovers_once(run, monkeypatch):
    real = graph.READ_TOOLS["query_metrics"].func
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("transient")
        return real(**kwargs)

    replace_tool(monkeypatch, "query_metrics", flaky)
    out = run()
    assert out.status == "draft_ready" and out.state["tool_calls"] == 5
    assert calls[0] == calls[1] and len(calls) == 3


def test_persistent_tool_failure_stops_after_one_retry(run, monkeypatch):
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("provider-secret-payload")

    replace_tool(monkeypatch, "query_metrics", unavailable)
    out = run()
    assert out.status == "tool_failed" and out.state["tool_calls"] == len(calls) == 2
    assert out.report.family == "none" and validate_report(out.report, out.state).ok
    assert "provider-secret-payload" not in out.trace_path.read_text()


def test_invalid_arguments_do_not_dispatch_or_retry(run, monkeypatch):
    calls = []
    replace_tool(monkeypatch, "query_metrics", lambda **kw: calls.append(kw))
    out = run(SequenceLLM(tool(metric="unknown"), partial()))
    assert out.status == "draft_ready" and not calls
    assert out.state["tool_calls"] == 1 and out.state["model_turns"] == 2


def test_failed_tool_result_is_observed_without_transport_retry(run, monkeypatch):
    calls = []

    def empty(**kwargs):
        calls.append(kwargs)
        return ToolResult(tool="query_metrics", ok=False, error="No samples.")

    replace_tool(monkeypatch, "query_metrics", empty)

    def observe(messages):
        assert 'ok="false"' in messages[-1]["content"] and "No samples." in messages[-1]["content"]
        return partial()

    out = run(SequenceLLM(tool(), observe))
    assert len(calls) == 1 and out.status == "draft_ready"
    assert not out.state["evidence_ledger"]


def test_followed_injection_is_blocked_not_ignored(run):
    out = run(FakeLLM("follow_injection"), incident="INC-002")
    assert out.status == "halted" and out.report is None
    assert out.state["tool_calls"] == 1
    assert any(ev.untrusted_flags for ev in out.state["evidence_ledger"].values())
    assert any(e["outcome"] == "blocked" for e in events(out))


def test_injection_can_be_ignored_and_investigation_continues(run):
    normal = FakeLLM()
    entry = get_incident("INC-002")
    injection_query = json.dumps(dict(type="tool_call", tool="query_logs", args=dict(
        incident_id="INC-002", start=entry.window_start.isoformat(), end=entry.window_end.isoformat(),
        event="request_validation_failed")))

    def ignore_injection(messages):
        # Retain the injection in the real conversation and ledger; the stub ignores it.
        clean = [m for m in messages if "request_validation_failed" not in m["content"]]
        return normal.complete(clean)

    out = run(SequenceLLM(injection_query, *([ignore_injection] * 4)), incident="INC-002")
    assert out.status == "draft_ready" and out.report.family == "disk_exhaustion"
    assert any(ev.untrusted_flags for ev in out.state["evidence_ledger"].values())
    assert all(not out.state["evidence_ledger"][c.evidence_id].untrusted_flags for c in out.report.cited_evidence)
    assert not any(e["outcome"] == "blocked" for e in events(out))


def test_gateway_production_workflow_preserves_available_usage(run):
    fake = FakeLLM()

    def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=fake.complete(kwargs["messages"])))],
            usage=SimpleNamespace(prompt_tokens=21, completion_tokens=13))

    llm = GatewayLLM(Settings(LLM_BASE_URL="https://gateway.invalid/v1", LLM_API_KEY="test-private-key",
        LLM_MODEL="configured-gateway-model"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    out = run(llm)
    assert out.status == "draft_ready"
    model_events = [e for e in events(out) if e["event"] == "model"]
    assert all(e["model"] == "configured-gateway-model" and e["usage"] == {
        "prompt_tokens": 21, "completion_tokens": 13} and e["cost"] is None for e in model_events)


def test_trace_is_parseable_bounded_and_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "gateway-private-value")
    writer = TraceWriter(tmp_path / "trace.jsonl", "run", "test", None)
    writer.event("test", "ok", {}, args={"query": "gateway-private-value", "api_key": "hidden"},
                 error="Bearer opaque-token " + "z" * 10000, missing_usage=None)
    writer.close()
    raw = (tmp_path / "trace.jsonl").read_text()
    record = json.loads(raw)
    assert "gateway-private-value" not in raw and "opaque-token" not in raw and "hidden" not in raw
    assert len(record["error"]) <= 2000 and record["missing_usage"] is None


@pytest.mark.parametrize("incident,scenario,status,code", [
    ("INC-001", "normal", "draft_ready", 0), ("INC-005", "normal", "draft_ready", 0),
    ("INC-001", "request_export", "halted", 1),
    ("INC-001", "cite_fabricated", "validation_failed", 1),
    ("INC-999", "normal", "invalid_input", 2),
])
def test_cli_subprocess_and_jsonl_trace(tmp_path, incident, scenario, status, code):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(graph.__file__).resolve().parents[1])
    completed = subprocess.run([sys.executable, "-m", "triage", "investigate", incident,
        "--question", "Why?", "--mode", "offline-scripted", "--scenario", scenario],
        cwd=tmp_path, env=env, text=True, capture_output=True)
    assert completed.returncode == code, completed.stdout + completed.stderr
    assert f"Terminal status: {status}" in completed.stdout and "offline-scripted" in completed.stdout
    path = Path(completed.stdout.split("Trace: ")[1].splitlines()[0])
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["outcome"] == status and all(r["mode"] == "offline-scripted" for r in records)
    if status == "validation_failed":
        assert "diagnosed:" not in completed.stdout and records[-1]["report"] is None


def test_cli_missing_real_configuration_has_trace(monkeypatch, tmp_path, capsys):
    def unavailable():
        raise ConfigError("No credentials configured")
    monkeypatch.setattr(graph, "GatewayLLM", unavailable)
    assert cli.main(["investigate", "INC-001", "--question", "Why?"]) == 2
    output = capsys.readouterr().out
    path = Path(output.split("Trace: ")[1].splitlines()[0])
    assert json.loads(path.read_text().splitlines()[-1])["outcome"] == "config_error"


def test_real_mode_rejects_scripted_scenario():
    with pytest.raises(SystemExit) as info:
        cli.main(["investigate", "INC-001", "--question", "Why?", "--scenario", "request_export"])
    assert info.value.code == 2


def test_repair_without_remaining_budget_returns_safe_partial(run):
    invalid = json.loads(partial())
    invalid["report"]["cited_evidence"] = [{"evidence_id": "INC-001:L9999", "reason": "invented"}]
    out = run(SequenceLLM(*([tool()] * 5), json.dumps(invalid)))
    assert out.status == "budget_exceeded" and out.state["model_turns"] == 6
    assert out.state["repairs"] == 0 and out.report.family == "none"
    assert validate_report(out.report, out.state).ok and not out.report.cited_evidence


def test_transient_on_last_model_attempt_never_retries_over_budget(run):
    out = run(SequenceLLM(*([tool()] * 5), TransientLLMError("transient")))
    assert out.status == "budget_exceeded" and out.state["model_turns"] == 6
    assert events(out)[-3]["retry"] is False


def test_deterministic_tool_exception_is_not_retried(run, monkeypatch):
    calls = []

    def broken(**kwargs):
        calls.append(kwargs)
        raise ValueError("must-not-appear-in-trace")

    replace_tool(monkeypatch, "query_metrics", broken)
    out = run()
    assert out.status == "tool_failed" and len(calls) == out.state["tool_calls"] == 1
    assert "must-not-appear-in-trace" not in out.trace_path.read_text()


def test_model_cannot_assign_code_owned_budget_status(run):
    invalid = json.loads(partial())
    invalid["report"]["status"] = "budget_exceeded"
    out = run(SequenceLLM(json.dumps(invalid), partial()))
    assert out.status == "draft_ready" and out.report.status == "inconclusive"
    assert out.state["repairs"] == 1
