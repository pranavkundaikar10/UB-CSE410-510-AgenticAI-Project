"""Shared argument validation for the read tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Self, TypeVar

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from triage.config import MAX_MESSAGE_CHARS
from triage.fixtures_store import get_incident
from triage.schemas import IncidentEntry, IncidentId, StrictModel, ToolResult


def _iso_only(value: Any) -> Any:
    """Accept ISO-8601 strings or datetime objects; refuse numbers and other types."""
    if isinstance(value, (str, datetime)):
        return value
    raise ValueError("must be an ISO-8601 timestamp string with a time zone")


IsoTime = Annotated[
    AwareDatetime,
    BeforeValidator(_iso_only),
    Field(description="ISO-8601 timestamp with time zone, e.g. 2026-01-01T10:00:00Z"),
]


class WindowArgs(StrictModel):
    """Incident ID and a [start, end) window that must lie inside the manifest window.

    The window check needs the incident entry, passed as validation context
    {"entry": IncidentEntry}. The tool functions always pass it.
    """

    incident_id: IncidentId
    start: IsoTime
    end: IsoTime

    @model_validator(mode="after")
    def _check_window(self, info: ValidationInfo) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        entry = (info.context or {}).get("entry")
        if entry is not None:
            if self.start < entry.window_start or self.end > entry.window_end:
                raise ValueError(
                    "window must lie inside the incident window "
                    f"{entry.window_start.isoformat()} to {entry.window_end.isoformat()}"
                )
        return self


ArgsT = TypeVar("ArgsT", bound=WindowArgs)


def format_validation_error(exc: ValidationError) -> str:
    """Compact error text for the model. Never echoes offending input values.

    Unexpected argument names are caller-controlled text too, so they are not echoed.
    """
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        if err["type"] == "extra_forbidden":
            parts.append("unexpected argument (not in the tool schema)")
            continue
        loc = ".".join(str(p) for p in err["loc"]) or "args"
        parts.append(f"{loc}: {err['msg']}")
    return ("invalid arguments: " + "; ".join(parts))[:MAX_MESSAGE_CHARS]


def validate_args(
    tool: str, model: type[ArgsT], raw: dict[str, Any]
) -> tuple[ArgsT | None, IncidentEntry, ToolResult | None]:
    """Resolve the incident (raises UnknownIncident) and validate the arguments.

    Returns (args, entry, None) on success or (None, entry, failed ToolResult).
    """
    entry = get_incident(raw.get("incident_id"))  # type: ignore[arg-type]
    try:
        args = model.model_validate(raw, context={"entry": entry})
    except ValidationError as exc:
        return None, entry, ToolResult(tool=tool, ok=False, error=format_validation_error(exc))
    return args, entry, None


def hhmm(dt: datetime) -> str:
    """UTC HHMM for evidence IDs."""
    return dt.astimezone(timezone.utc).strftime("%H%M")


def parse_ts(value: str) -> datetime:
    """Parse a fixture timestamp (ISO-8601, 'Z' suffix)."""
    return datetime.fromisoformat(value)
