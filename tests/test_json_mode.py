"""JSON mode defaults and the json_mode field on model trace events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from triage import graph
from triage.config import Settings, load_settings
from triage.fake_llm import FakeLLM


def _clear(monkeypatch) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_S", "LLM_JSON_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_json_mode_is_on_by_default() -> None:
    assert Settings().LLM_JSON_MODE is True


def test_json_mode_on_when_variable_is_unset(monkeypatch, tmp_path: Path) -> None:
    _clear(monkeypatch)
    assert load_settings(env_file=tmp_path / "missing.env").LLM_JSON_MODE is True


@pytest.mark.parametrize("value", ["0", "false", "False"])
def test_json_mode_can_be_turned_off(monkeypatch, tmp_path: Path, value: str) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_JSON_MODE", value)
    assert load_settings(env_file=tmp_path / "missing.env").LLM_JSON_MODE is False


def test_model_events_record_json_mode(tmp_path: Path) -> None:
    class JsonModeLLM(FakeLLM):
        json_mode = True

    def model_events(llm):
        out = graph.investigate("INC-005", "What caused the restart?", llm=llm,
                                trace_dir=tmp_path, mode="offline-scripted")
        records = [json.loads(line) for line in out.trace_path.read_text().splitlines()]
        return [r for r in records if r["event"] == "model"]

    scripted = model_events(FakeLLM())
    assert scripted and all(r["json_mode"] is None for r in scripted)
    configured = model_events(JsonModeLLM())
    assert configured and all(r["json_mode"] is True for r in configured)
