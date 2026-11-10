"""Empty model replies: still counted as attempts, and two in a row stop the run."""

from __future__ import annotations

import json

import pytest

from triage import graph
from triage.fake_llm import FakeLLM
from triage.validator import validate_report

REASON = ("The model returned empty message text twice. If your gateway returns reasoning "
          "without content, set LLM_JSON_MODE=1.")


class ReplyLLM:
    model = "test-replies"

    def __init__(self, *replies):
        self.replies = iter(replies)

    def complete(self, messages):
        reply = next(self.replies)
        return reply(messages) if callable(reply) else reply


@pytest.fixture
def run(tmp_path):
    def investigate(llm, incident="INC-001"):
        return graph.investigate(incident, "What caused the incident?", llm=llm,
                                 trace_dir=tmp_path, mode="offline-scripted")
    return investigate


def _events(out):
    return [json.loads(line) for line in out.trace_path.read_text().splitlines()]


@pytest.mark.parametrize("blank", ["", "  \n\t "])
def test_two_empty_replies_stop_with_model_failed(run, blank):
    out = run(ReplyLLM(blank, blank))
    assert out.status == "model_failed"
    assert out.state["halt_reason"] == REASON
    assert out.state["model_turns"] == 2 and out.state["tool_calls"] == 0
    assert out.report.status == "inconclusive" and validate_report(out.report, out.state).ok


def test_empty_replies_are_traced(run):
    out = run(ReplyLLM("", ""))
    empty = [e for e in _events(out) if e["event"] == "parse" and e["outcome"] == "empty_reply"]
    assert [e["consecutive"] for e in empty] == [1, 2]
    assert _events(out)[-1]["outcome"] == "model_failed"


def test_one_empty_reply_then_a_valid_run_proceeds(run):
    scripted = FakeLLM()
    out = run(ReplyLLM("", scripted.complete, scripted.complete, scripted.complete,
                       scripted.complete, scripted.complete))
    assert out.status == "draft_ready"
    assert out.state["model_turns"] == 6 and out.state["empty_replies"] == 0
    assert any(e["outcome"] == "empty_reply" for e in _events(out) if e["event"] == "parse")


def test_a_non_empty_reply_resets_the_count(run):
    scripted = FakeLLM()
    out = run(ReplyLLM("", "not json", "", scripted.complete, scripted.complete, scripted.complete))
    assert out.status != "model_failed"
    assert out.state["empty_replies"] <= 1
