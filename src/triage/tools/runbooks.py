"""search_runbooks tool: ranked runbook passages."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from triage import paths
from triage.errors import RunbookError
from triage.paths import safe_join
from triage.schemas import Evidence, StrictModel, ToolResult
from triage.tools.common import format_validation_error

TOOL_NAME = "search_runbooks"
DESCRIPTION = (
    "Search the versioned runbooks by keywords (symptoms, metric names, log event types, "
    "error text). Returns up to top_k runbooks ranked by term overlap, each with its best-"
    "matching section excerpt. Cite results as RB:<id>@<version>."
)

# Read at call time so tests can point the loader at a temporary corpus.
RUNBOOKS_DIR: Path = paths.RUNBOOKS_DIR

REQUIRED_SECTIONS = ("Symptoms", "How to confirm", "Distinguish from", "Mitigation", "Escalation")
TITLE_FIELD = "title"
WEIGHTS: dict[str, float] = {
    TITLE_FIELD: 3.0,
    "How to confirm": 3.0,
    "Symptoms": 2.0,
    "Mitigation": 1.0,
    "Distinguish from": 0.5,  # mentions look-alike signals, so it must not dominate
    "Escalation": 0.5,
}
MAX_EXCERPT_CHARS = 600

STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its no not of on or "
    "so than that the then there these this to was were when where which while with".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_FRONT_MATTER_RE = re.compile(r"^([a-z_]+):\s*(.+?)\s*$")

RunbookFamily = Literal[
    "connection_pool_exhaustion", "disk_exhaustion", "dependency_failure", "deployment_regression"
]


class Runbook(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+$")
    family: RunbookFamily
    updated: date
    sections: dict[str, str]

    @model_validator(mode="after")
    def _sections_in_order(self) -> Self:
        if tuple(self.sections) != REQUIRED_SECTIONS:
            raise ValueError(
                f"sections must be exactly {list(REQUIRED_SECTIONS)} in order, got {list(self.sections)}"
            )
        empty = [name for name, body in self.sections.items() if not body.strip()]
        if empty:
            raise ValueError(f"empty sections: {empty}")
        return self

    @property
    def evidence_id(self) -> str:
        return f"RB:{self.id}@{self.version}"


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #
def parse_runbook(text: str) -> dict[str, Any]:
    """Split front matter ("key: value" lines between ---) and "## " sections."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing front matter: first line must be ---")
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("front matter is not closed with ---") from None

    meta: dict[str, Any] = {}
    for lineno, line in enumerate(lines[1:close], start=2):
        if not line.strip():
            continue
        match = _FRONT_MATTER_RE.match(line)
        if not match:
            raise ValueError(f"line {lineno}: front matter must be 'key: value'")
        key, value = match.groups()
        if key in meta:
            raise ValueError(f"line {lineno}: duplicate key {key!r}")
        meta[key] = value

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for lineno, line in enumerate(lines[close + 1:], start=close + 2):
        if line.startswith("## "):
            current = line[3:].strip()
            if current in sections:
                raise ValueError(f"line {lineno}: duplicate section {current!r}")
            sections[current] = []
        elif current is None:
            if line.strip():
                raise ValueError(f"line {lineno}: text before the first ## section")
        else:
            sections[current].append(line)

    if "sections" in meta:
        raise ValueError("'sections' is not a valid front matter key")
    meta["sections"] = {name: "\n".join(body).strip() for name, body in sections.items()}
    return meta


def load_runbooks() -> list[Runbook]:
    """Load and validate every runbooks/*.md, sorted by id. Raises RunbookError naming the file."""
    base = Path(RUNBOOKS_DIR)
    names = sorted(p.name for p in base.glob("*.md") if p.is_file())
    if not names:
        raise RunbookError(f"no runbooks found in {base}")
    books: list[Runbook] = []
    for name in names:
        path = safe_join(base, name)
        try:
            book = Runbook.model_validate(parse_runbook(path.read_text(encoding="utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:  # ValidationError is a ValueError
            raise RunbookError(f"malformed runbook {name}: {exc}") from exc
        if book.id != path.stem:
            raise RunbookError(f"malformed runbook {name}: id {book.id!r} must match the file name")
        books.append(book)
    for field in ("id", "family"):
        values = [getattr(b, field) for b in books]
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise RunbookError(f"duplicate runbook {field}: {dupes}")
    return sorted(books, key=lambda b: b.id)


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; snake_case names also yield their parts, minus stopwords."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        parts = tok.split("_")
        out.extend([tok, *parts] if len(parts) > 1 else [tok])
    return [t for t in out if t not in STOPWORDS]


def _fields(book: Runbook) -> dict[str, str]:
    return {TITLE_FIELD: book.title, **book.sections}


def _score(terms: set[str], counts: Counter[str], idf: dict[str, float]) -> float:
    return sum(idf[t] * (1.0 + math.log(counts[t])) for t in terms if counts[t])


def rank(query: str, books: list[Runbook]) -> list[tuple[float, str, Runbook]]:
    """Return (score, best_section, runbook) with score > 0, best first, ties by id."""
    terms = set(tokenize(query))
    token_counts = {b.id: {f: Counter(tokenize(t)) for f, t in _fields(b).items()} for b in books}
    n = len(books)
    idf = {}
    for t in terms:
        df = sum(1 for b in books if any(c[t] for c in token_counts[b.id].values()))
        idf[t] = math.log(1.0 + n / df) if df else 0.0

    ranked = []
    for book in books:
        per_field = {f: WEIGHTS[f] * _score(terms, c, idf) for f, c in token_counts[book.id].items()}
        total = sum(per_field.values())
        if total <= 0:
            continue
        best = max(REQUIRED_SECTIONS, key=lambda s: (per_field[s], -REQUIRED_SECTIONS.index(s)))
        ranked.append((total, best, book))
    ranked.sort(key=lambda r: (-round(r[0], 9), r[2].id))
    return ranked


# --------------------------------------------------------------------------- #
# Tool                                                                        #
# --------------------------------------------------------------------------- #
class SearchRunbooksArgs(StrictModel):
    query: str = Field(min_length=3, max_length=200, description="Keywords or error text")
    top_k: StrictInt = Field(default=2, ge=1, le=4)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("query must contain at least 3 non-space characters")
        return value


def _excerpt(text: str) -> str:
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[: MAX_EXCERPT_CHARS - 3].rstrip() + "..."


def search_runbooks(query: str | None = None, top_k: int = 2, **extra: Any) -> ToolResult:
    """Rank runbooks for a query. Bad arguments return ok=False; no matches is ok=True."""
    try:
        args = SearchRunbooksArgs.model_validate({"query": query, "top_k": top_k, **extra})
    except ValidationError as exc:
        return ToolResult(tool=TOOL_NAME, ok=False, error=format_validation_error(exc))

    ranked = rank(args.query, load_runbooks())
    kept = ranked[: args.top_k]
    evidence = [
        Evidence(
            evidence_id=book.evidence_id,
            kind="runbook",
            incident_id=None,
            summary=f"{book.title} v{book.version}",
            data={
                "title": book.title,
                "version": book.version,
                "family": book.family,
                "section": section,
                "excerpt": _excerpt(book.sections[section]),
                # Always the full section, so look-alike families are separated even when
                # the best-matching excerpt comes from another section.
                "distinguish_from": book.sections["Distinguish from"],
                "score": round(score, 3),
            },
        )
        for score, section, book in kept
    ]
    return ToolResult(tool=TOOL_NAME, ok=True, evidence=evidence,
                      truncated=len(ranked) > len(kept), total_matched=len(ranked))
