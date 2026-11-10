"""The final stdout line: one verdict per outcome class, with exit codes unchanged."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from triage import graph
from triage.cli import verdict_line

SRC = Path(graph.__file__).resolve().parents[1]


def _run(tmp_path, incident, scenario="normal"):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.pop("NO_COLOR", None)
    return subprocess.run([sys.executable, "-m", "triage", "investigate", incident,
        "--question", "Why?", "--mode", "offline-scripted", "--scenario", scenario],
        cwd=tmp_path, env=env, text=True, capture_output=True)


@pytest.mark.parametrize("incident,scenario,code,verdict", [
    ("INC-001", "normal", 0,
     "SUCCESS - validated draft ready for human review (diagnosed: connection_pool_exhaustion)"),
    ("INC-005", "normal", 0, "SUCCESS - validated inconclusive draft ready for human review"),
    ("INC-001", "request_export", 1, "BLOCKED - an unsafe action was stopped before it ran"),
    ("INC-001", "cite_fabricated", 1, "FAILED - validation_failed: Report validation failed after one repair."),
    ("INC-001", "always_tool_call", 1, "FAILED - budget_exceeded: "),
    ("INC-999", "normal", 2, "FAILED - invalid_input: "),
])
def test_last_stdout_line_is_the_verdict(tmp_path, incident, scenario, code, verdict) -> None:
    done = _run(tmp_path, incident, scenario)
    assert done.returncode == code, done.stdout + done.stderr
    last = done.stdout.rstrip("\n").splitlines()[-1]
    assert last.startswith(verdict) and last.isascii()
    assert "\033" not in done.stdout  # captured output is never coloured


def _outcome(status, report_status=None, family="none", reason=None):
    report = SimpleNamespace(status=report_status, family=family) if report_status else None
    return SimpleNamespace(status=status, report=report, state={"halt_reason": reason})


def test_failed_verdict_reuses_the_halt_reason() -> None:
    line = verdict_line(_outcome("model_failed", reason="Model unavailable after one retry."))
    assert line == "FAILED - model_failed: Model unavailable after one retry."


def test_failed_verdict_without_a_reason() -> None:
    assert verdict_line(_outcome("tool_failed")) == "FAILED - tool_failed"


def test_success_does_not_claim_the_diagnosis_is_correct() -> None:
    line = verdict_line(_outcome("draft_ready", "diagnosed", "disk_exhaustion"))
    assert "human review" in line and "correct" not in line and "approved" not in line


@pytest.mark.parametrize("outcome,keyword,code", [
    (_outcome("draft_ready", "inconclusive"), "SUCCESS", "32"),
    (_outcome("halted"), "BLOCKED", "33"),
    (_outcome("validation_failed"), "FAILED", "31"),
])
def test_keyword_is_coloured_only_when_asked(outcome, keyword, code) -> None:
    assert verdict_line(outcome, color=True).startswith(f"\033[{code}m{keyword}\033[0m - ")
    assert verdict_line(outcome) .startswith(f"{keyword} - ") and "\033" not in verdict_line(outcome)
