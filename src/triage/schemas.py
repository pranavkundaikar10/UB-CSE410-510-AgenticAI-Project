"""Pydantic v2 data contracts shared across the package."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from triage.config import MAX_MESSAGE_CHARS

INCIDENT_ID_PATTERN = r"^INC-\d{3}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

LOG_EVIDENCE_RE = re.compile(r"^(INC-\d{3}):L\d{4,}$")
METRIC_EVIDENCE_RE = re.compile(r"^(INC-\d{3}):M:[a-z0-9_]+:\d{4}-\d{4}$")
RUNBOOK_EVIDENCE_RE = re.compile(r"^RB:[a-z0-9][a-z0-9-]*@\d+(\.\d+)*$")

IncidentId = Annotated[str, Field(pattern=INCIDENT_ID_PATTERN)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]

EvidenceKind = Literal["log", "metric", "runbook"]
ReportStatus = Literal["diagnosed", "inconclusive", "budget_exceeded"]
Family = Literal[
    "connection_pool_exhaustion",
    "disk_exhaustion",
    "dependency_failure",
    "deployment_regression",
    "none",
]


class StrictModel(BaseModel):
    """Base for every contract: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Manifest (mirrors fixtures/manifest.json)                                   #
# --------------------------------------------------------------------------- #
class BundleFile(StrictModel):
    path: str
    sha256: Sha256


class IncidentEntry(StrictModel):
    incident_id: IncidentId
    title: str
    topology_id: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    logs: BundleFile
    metrics: BundleFile

    @model_validator(mode="after")
    def _window_order(self) -> IncidentEntry:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class Manifest(StrictModel):
    manifest_version: str
    topology_id: str
    incidents: list[IncidentEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> Manifest:
        ids = [i.incident_id for i in self.incidents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate incident_id in manifest")
        return self


# --------------------------------------------------------------------------- #
# Topology (mirrors fixtures/topology.json)                                   #
# --------------------------------------------------------------------------- #
class DbPool(StrictModel):
    name: str
    max: int = Field(gt=0)


class ServiceSpec(StrictModel):
    name: str
    host: str
    role: str
    depends_on: list[str] | None = None
    db_pool: DbPool | None = None
    data_volume: str | None = None


class Topology(StrictModel):
    topology_id: str
    services: list[ServiceSpec]

    def service_names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.services)


# --------------------------------------------------------------------------- #
# Evidence and tool results                                                   #
# --------------------------------------------------------------------------- #
class Evidence(StrictModel):
    """One citable item. The ID format depends on `kind`:

    log      "<INC>:<event_id>"                e.g. INC-003:L0205
    metric   "<INC>:M:<metric>:<HHMM>-<HHMM>"  e.g. INC-003:M:db_pool_active:1800-1900
    runbook  "RB:<id>@<version>"               e.g. RB:dependency-failure@1.0
    """

    evidence_id: str
    kind: EvidenceKind
    incident_id: IncidentId | None
    summary: str = Field(max_length=MAX_MESSAGE_CHARS)
    data: dict[str, Any] = Field(default_factory=dict)
    # Names of instruction-like patterns found in data (set by guardrails.flag_evidence).
    untrusted_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _id_matches_kind(self) -> Evidence:
        if self.kind == "runbook":
            if self.incident_id is not None:
                raise ValueError("runbook evidence must have incident_id=None")
            if not RUNBOOK_EVIDENCE_RE.match(self.evidence_id):
                raise ValueError(f"bad runbook evidence_id: {self.evidence_id!r}")
            return self
        pattern = LOG_EVIDENCE_RE if self.kind == "log" else METRIC_EVIDENCE_RE
        match = pattern.match(self.evidence_id)
        if not match:
            raise ValueError(f"bad {self.kind} evidence_id: {self.evidence_id!r}")
        if self.incident_id is None or match.group(1) != self.incident_id:
            raise ValueError("evidence_id prefix must match incident_id")
        return self


class ToolResult(StrictModel):
    tool: str
    ok: bool
    evidence: list[Evidence] = Field(default_factory=list)
    truncated: bool = False
    total_matched: int = Field(default=0, ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def _error_iff_not_ok(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("ok results must not carry an error")
        if not self.ok and not self.error:
            raise ValueError("failed results must carry an error message")
        return self


# --------------------------------------------------------------------------- #
# Report                                                                      #
# --------------------------------------------------------------------------- #
class CitedEvidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DraftReport(StrictModel):
    incident_id: IncidentId
    status: ReportStatus
    family: Family
    summary: str = Field(min_length=1)
    root_cause: str
    cited_evidence: list[CitedEvidence] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


def report_sha256(report: DraftReport) -> str:
    """SHA-256 of the report's canonical JSON (sorted keys, compact separators)."""
    canonical = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Model actions (discriminated on "type")                                     #
# --------------------------------------------------------------------------- #
class ToolCallAction(StrictModel):
    type: Literal["tool_call"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class FinalReportAction(StrictModel):
    type: Literal["final_report"]
    report: DraftReport


ModelAction = Annotated[ToolCallAction | FinalReportAction, Field(discriminator="type")]


# --------------------------------------------------------------------------- #
# Approval and run state                                                      #
# --------------------------------------------------------------------------- #
class ApprovalRecord(StrictModel):
    decision: Literal["approve", "reject"]
    report_sha256: Sha256
    decided_at: AwareDatetime

    @field_validator("decided_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class RunState(TypedDict):
    """LangGraph state for one investigation."""

    run_id: str
    incident_id: str
    question: str
    window_start: str
    window_end: str
    messages: list[dict[str, Any]]
    evidence_ledger: dict[str, Evidence]
    model_turns: int
    tool_calls: int
    draft: DraftReport | None
    status: str
    halt_reason: str | None
    approval: ApprovalRecord | None
    steps: list[str]
