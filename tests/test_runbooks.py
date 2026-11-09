"""Tests for the runbook corpus and search_runbooks."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from triage.errors import PathViolation, RunbookError
from triage.paths import RUNBOOKS_DIR, safe_join
from triage.tools import READ_TOOLS, tool_catalog
from triage.tools import runbooks as rb
from triage.tools.runbooks import REQUIRED_SECTIONS, load_runbooks, search_runbooks

FAMILIES = {"connection_pool_exhaustion", "disk_exhaustion", "dependency_failure", "deployment_regression"}
RUNBOOK_FILES = sorted(RUNBOOKS_DIR.glob("*.md"))


# --------------------------------------------------------------------------- #
# Corpus                                                                      #
# --------------------------------------------------------------------------- #
def test_all_runbooks_load_with_unique_families() -> None:
    books = load_runbooks()
    assert len(books) == 4
    families = [b.family for b in books]
    assert sorted(families) == sorted(FAMILIES)
    assert len(set(families)) == len(families)
    for book in books:
        assert tuple(book.sections) == REQUIRED_SECTIONS
        assert re.fullmatch(r"\d+\.\d+", book.version)


@pytest.mark.parametrize("path", RUNBOOK_FILES, ids=lambda p: p.name)
def test_runbooks_do_not_leak_fixture_answers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "INC-" not in text
    assert not re.search(r"L\d{4}", text)
    assert not re.search(r"20\d\d-\d\d-\d\dT", text), "no fixture timestamps"
    assert len(text.splitlines()) < 60


@pytest.mark.parametrize(
    ("family", "signals"),
    [
        ("connection_pool_exhaustion", ["db_pool_active", "db_pool_max", "db_pool_wait_ms", "db_pool_timeout"]),
        ("disk_exhaustion", ["disk_used_pct", "disk_usage_high", "write_failed"]),
        ("dependency_failure", ["db_query_latency_p95_ms", "db_cpu_pct", "slow_query", "db_query_timeout"]),
        ("deployment_regression", ["deploy_completed", "unhandled_exception", "error_rate_pct"]),
    ],
)
def test_how_to_confirm_names_concrete_signals(family: str, signals: list[str]) -> None:
    book = next(b for b in load_runbooks() if b.family == family)
    for signal in signals:
        assert signal in book.sections["How to confirm"], signal


# --------------------------------------------------------------------------- #
# Ranking                                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("timed out waiting for a connection from pool", "connection-pool-exhaustion"),
        ("No space left on device", "disk-exhaustion"),
        ("statement timeout slow queries database latency", "dependency-failure"),
        ("errors started after deploy KeyError new version", "deployment-regression"),
    ],
)
def test_query_ranks_expected_runbook_first(query: str, expected: str) -> None:
    result = search_runbooks(query)
    assert result.ok and result.evidence
    assert result.evidence[0].evidence_id.startswith(f"RB:{expected}@")


def test_evidence_shape() -> None:
    result = search_runbooks("db_pool_wait_ms high latency", top_k=4)
    assert result.ok and result.evidence
    scores = [ev.data["score"] for ev in result.evidence]
    assert scores == sorted(scores, reverse=True)
    for ev in result.evidence:
        assert re.fullmatch(r"RB:[a-z0-9-]+@\d+\.\d+", ev.evidence_id)
        assert ev.kind == "runbook" and ev.incident_id is None
        assert ev.summary == f"{ev.data['title']} v{ev.data['version']}"
        assert set(ev.data) == {"title", "version", "family", "section", "excerpt",
                                "distinguish_from", "score"}
        assert ev.data["section"] in REQUIRED_SECTIONS
        assert 0 < len(ev.data["excerpt"]) <= 600
        assert ev.data["score"] > 0


def test_lookalike_query_returns_both_families_in_top_2() -> None:
    result = search_runbooks("db query timeout with normal connection pool", top_k=2)
    assert result.ok
    assert {ev.evidence_id for ev in result.evidence} == {
        "RB:connection-pool-exhaustion@1.0",
        "RB:dependency-failure@1.0",
    }


@pytest.mark.parametrize(
    "query",
    ["db query timeout with normal connection pool", "No space left on device",
     "errors started after deploy KeyError new version", "checkout-api errors latency"],
)
def test_every_evidence_has_distinguish_from(query: str) -> None:
    result = search_runbooks(query, top_k=4)
    assert result.evidence
    books = {b.evidence_id: b for b in load_runbooks()}
    for ev in result.evidence:
        text = ev.data["distinguish_from"]
        assert isinstance(text, str) and text.strip()
        assert text == books[ev.evidence_id].sections["Distinguish from"]  # full, not clipped


def test_distinguish_from_names_the_lookalike() -> None:
    result = search_runbooks("db query timeout with normal connection pool", top_k=2)
    by_id = {ev.evidence_id: ev.data["distinguish_from"].lower() for ev in result.evidence}
    assert "dependency failure" in by_id["RB:connection-pool-exhaustion@1.0"]
    assert "pool saturation" in by_id["RB:dependency-failure@1.0"]


def test_top_k_and_truncation() -> None:
    one = search_runbooks("checkout-api errors latency", top_k=1)
    assert len(one.evidence) == 1 and one.truncated and one.total_matched > 1
    four = search_runbooks("checkout-api errors latency", top_k=4)
    assert len(four.evidence) == four.total_matched and not four.truncated


def test_results_are_deterministic() -> None:
    query = "db query timeout with normal connection pool"
    first = search_runbooks(query, top_k=4).model_dump_json()
    second = search_runbooks(query, top_k=4).model_dump_json()
    assert first == second


def test_ties_break_by_id() -> None:
    books = load_runbooks()
    ranked = rb.rank("checkout-api", books)
    scores = [round(s, 9) for s, _, _ in ranked]
    for (s1, _, b1), (s2, _, b2) in zip(ranked, ranked[1:]):
        if round(s1, 9) == round(s2, 9):
            assert b1.id < b2.id
    assert scores == sorted(scores, reverse=True)


def test_nonsense_query_returns_empty_ok() -> None:
    result = search_runbooks("qwzx flurb zzyzx")
    assert result.ok is True and result.evidence == [] and result.total_matched == 0


def test_stopword_only_query_returns_empty_ok() -> None:
    result = search_runbooks("the and of with")
    assert result.ok is True and result.evidence == []


def test_tokenize_splits_snake_case() -> None:
    assert rb.tokenize("The db_pool_wait_ms is HIGH") == ["db_pool_wait_ms", "db", "pool", "wait", "ms", "high"]


# --------------------------------------------------------------------------- #
# Bad arguments                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "ab"},
        {"query": "     "},
        {"query": None},
        {"query": "x" * 201},
        {"query": "disk full", "top_k": 10},
        {"query": "disk full", "top_k": 0},
        {"query": "disk full", "top_k": "2"},
        {"query": "disk full", "path": "../evaluation/gold"},
        {"query": "disk full", "version": "1.0"},
    ],
)
def test_bad_args_return_error(kwargs: dict) -> None:
    result = search_runbooks(**kwargs)
    assert result.ok is False and result.error and result.evidence == []


# --------------------------------------------------------------------------- #
# Paths and malformed files                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "relative",
    ["../evaluation/gold/dev_labels.json", "..\\fixtures\\manifest.json", "../src/triage/config.py",
     "C:\\Windows\\win.ini", "/etc/passwd"],
)
def test_safe_join_refuses_escape_from_runbooks(relative: str) -> None:
    with pytest.raises(PathViolation):
        safe_join(RUNBOOKS_DIR, relative)


def _corpus_copy(tmp_path: Path, monkeypatch) -> Path:
    copy = tmp_path / "runbooks"
    shutil.copytree(RUNBOOKS_DIR, copy)
    monkeypatch.setattr(rb, "RUNBOOKS_DIR", copy)
    return copy


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda t: t.replace("version: 1.0", "version: one"), "version"),
        (lambda t: t.replace("family: disk_exhaustion", "family: memory_leak"), "family"),
        (lambda t: t.replace("## Escalation", "## Contacts"), "sections"),
        (lambda t: t.replace("## Mitigation", "## Escalation", 1), "duplicate section"),
        (lambda t: t.replace("---\n", "", 1), "front matter"),
        (lambda t: t.replace("title: Disk", "title Disk"), "key: value"),
        (lambda t: t.replace("updated: 2026-11-01", "updated: 2026-11-01\nowner: ops"), "owner"),
        (lambda t: t.replace("id: disk-exhaustion", "id: disk-full"), "file name"),
    ],
)
def test_malformed_runbook_raises_naming_file(tmp_path: Path, monkeypatch, mutate, needle: str) -> None:
    copy = _corpus_copy(tmp_path, monkeypatch)
    target = copy / "disk-exhaustion.md"
    target.write_text(mutate(target.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(RunbookError) as exc:
        load_runbooks()
    assert "disk-exhaustion.md" in str(exc.value)
    assert needle in str(exc.value)


def test_duplicate_family_rejected(tmp_path: Path, monkeypatch) -> None:
    copy = _corpus_copy(tmp_path, monkeypatch)
    text = (copy / "disk-exhaustion.md").read_text(encoding="utf-8")
    (copy / "disk-exhaustion-v2.md").write_text(text.replace("id: disk-exhaustion", "id: disk-exhaustion-v2"),
                                                encoding="utf-8")
    with pytest.raises(RunbookError, match="duplicate runbook family"):
        load_runbooks()


def test_empty_corpus_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rb, "RUNBOOKS_DIR", tmp_path)
    with pytest.raises(RunbookError, match="no runbooks"):
        load_runbooks()


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #
def test_registered_as_read_tool() -> None:
    assert set(READ_TOOLS) == {"query_logs", "query_metrics", "search_runbooks"}
    assert "export_report" not in READ_TOOLS
    entry = next(t for t in tool_catalog() if t["name"] == "search_runbooks")
    assert entry["parameters"]["required"] == ["query"]
    assert entry["parameters"]["additionalProperties"] is False
    assert READ_TOOLS["search_runbooks"].func(query="disk full").ok
