"""--progress: status lines go to stderr only, and nothing else about a run changes."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from triage import cli, graph
from triage.progress import format_progress

SRC = Path(graph.__file__).resolve().parents[1]
FAKE_KEY = "sk-test-progress-key-8c1f"
FAKE_URL = "https://gateway-progress.invalid/v1"
FAKE_MODEL = "progress-test-model"


def _run(tmp_path, *extra, incident="INC-001", scenario="normal"):
    env = dict(os.environ, PYTHONPATH=str(SRC), LLM_API_KEY=FAKE_KEY,
               LLM_BASE_URL=FAKE_URL, LLM_MODEL=FAKE_MODEL)
    return subprocess.run([sys.executable, "-m", "triage", "investigate", incident,
        "--question", "Why?", "--mode", "offline-scripted", "--scenario", scenario, *extra],
        cwd=tmp_path, env=env, text=True, capture_output=True)


def _normalise(stdout: str) -> str:
    return re.sub(r"[0-9a-f]{32}", "<run>", re.sub(r"Trace: .*", "Trace: <path>", stdout))


def _without_verdict(stdout: str) -> str:
    return stdout.rstrip("\n").rsplit("\n", 1)[0]


def test_progress_lines_go_to_stderr(tmp_path) -> None:
    done = _run(tmp_path, "--progress")
    assert done.returncode == 0
    lines = done.stderr.splitlines()
    assert lines[0] == ("[start] INC-001 mode=offline-scripted "
                        "model=FakeLLM (incident-specific scripts)")
    assert "[model 1/6]" in lines[1] and lines[1].endswith("s")
    assert "[tool 1/7] query_metrics metric=db_pool_active" in lines
    assert any(re.fullmatch(r"\[tool 1/7\] ok, 1 evidence, \d+\.\ds", line) for line in lines)
    assert "[validate] valid" in lines
    assert re.fullmatch(r"\[done\] status=draft_ready model=5/6 tools=4/7 \d+\.\ds total", lines[-1])


def test_progress_reports_validation_errors_and_repair(tmp_path) -> None:
    done = _run(tmp_path, "--progress", scenario="cite_fabricated")
    assert "[validate] invalid: " in done.stderr and "L9999" in done.stderr
    assert "[repair] requested" in done.stderr
    assert "status=validation_failed" in done.stderr.splitlines()[-1]


def test_stdout_is_the_same_with_and_without_progress(tmp_path) -> None:
    plain, noisy = _run(tmp_path), _run(tmp_path, "--progress")
    assert plain.stderr == ""
    assert noisy.returncode == plain.returncode
    assert _normalise(noisy.stdout) == _normalise(plain.stdout)
    assert (_without_verdict(_normalise(noisy.stdout))
            == _without_verdict(_normalise(plain.stdout)))


@pytest.mark.parametrize("flag", [[], ["--progress"]])
def test_no_config_value_appears_in_either_stream(tmp_path, flag) -> None:
    done = _run(tmp_path, *flag)
    for secret in (FAKE_KEY, FAKE_URL, FAKE_MODEL):
        assert secret not in done.stdout and secret not in done.stderr


def test_progress_lines_never_carry_model_text_or_evidence() -> None:
    record = dict(event="guard", outcome="allowed", mode="m", model_attempts=2, tool_attempts=1,
                  tool="query_logs", args=dict(incident_id="INC-001", start="s", end="e",
                  contains="x" * 200, api_key="abc"), elapsed_ms=5)
    line = format_progress(record)
    assert line.startswith("[tool 2/7] query_logs contains=")
    assert "x" * 61 not in line and "abc" not in line and "INC-001" not in line
    # Events with no progress line stay quiet.
    assert format_progress(dict(record, event="input", outcome="valid")) is None


def test_progress_sink_can_be_a_plain_callable(tmp_path) -> None:
    from triage.fake_llm import FakeLLM
    lines: list[str] = []
    out = graph.investigate("INC-005", "Why?", llm=FakeLLM(), trace_dir=tmp_path,
                            mode="offline-scripted", progress=lines.append)
    assert out.status == "draft_ready" and lines[0].startswith("[start] INC-005")


# --------------------------------------------------------------------------- #
# When the lines appear: terminal detection, --progress and --quiet           #
# --------------------------------------------------------------------------- #
class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _in_process(monkeypatch, *flags, stderr_tty=False, stdout_tty=False, no_color=False):
    out, err = _Stream(stdout_tty), _Stream(stderr_tty)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    code = cli.main(["investigate", "INC-005", "--question", "Why?",
                     "--mode", "offline-scripted", *flags])
    return code, out.getvalue(), err.getvalue()


def test_progress_is_off_when_stderr_is_not_a_terminal(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch)
    assert err == ""


def test_progress_is_on_when_stderr_is_a_terminal(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch, stderr_tty=True)
    assert err.startswith("[start] INC-005") and "[done] status=draft_ready" in err


def test_progress_flag_forces_lines_on(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch, "--progress")
    assert "[done] status=draft_ready" in err


def test_quiet_turns_lines_off_even_on_a_terminal(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch, "--quiet", stderr_tty=True)
    assert err == ""


def test_quiet_wins_over_progress(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch, "--progress", "--quiet", stderr_tty=True)
    assert err == ""


def test_progress_lines_are_plain_ascii(monkeypatch) -> None:
    _, _, err = _in_process(monkeypatch, "--progress")
    assert err.isascii()


def test_progress_ends_with_total_time_before_the_verdict(monkeypatch) -> None:
    _, out, err = _in_process(monkeypatch, "--progress")
    assert re.fullmatch(r"\[done\] status=draft_ready model=\d/6 tools=\d/7 \d+\.\ds total",
                        err.splitlines()[-1])
    assert out.splitlines()[-1].startswith("SUCCESS - ")


# --------------------------------------------------------------------------- #
# Colour on the verdict keyword                                               #
# --------------------------------------------------------------------------- #
def test_no_colour_codes_when_stdout_is_not_a_terminal(monkeypatch) -> None:
    _, out, _ = _in_process(monkeypatch)
    assert "\033" not in out


def test_colour_codes_on_a_terminal(monkeypatch) -> None:
    _, out, _ = _in_process(monkeypatch, stdout_tty=True)
    assert out.splitlines()[-1].startswith("\033[32mSUCCESS\033[0m - ")


def test_no_color_variable_turns_colour_off(monkeypatch) -> None:
    _, out, _ = _in_process(monkeypatch, stdout_tty=True, no_color=True)
    assert "\033" not in out and out.splitlines()[-1].startswith("SUCCESS - ")


def test_null_arguments_are_left_out_of_progress_lines() -> None:
    record = dict(event="guard", outcome="allowed", mode="m", model_attempts=1, tool_attempts=0,
                  tool="query_logs", args=dict(service=None, level=None, contains="503", limit=20),
                  elapsed_ms=1)
    assert format_progress(record) == "[tool 1/7] query_logs contains=503 limit=20"
