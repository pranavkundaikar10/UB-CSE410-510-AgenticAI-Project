"""query_metrics tool: metric samples and statistics for one incident."""

from __future__ import annotations

from datetime import datetime
from statistics import fmean
from typing import Any, Literal, Self, get_args

from pydantic import model_validator

from triage.fixtures_store import load_bundle
from triage.schemas import Evidence, ToolResult
from triage.tools.common import WindowArgs, hhmm, parse_ts, validate_args

TOOL_NAME = "query_metrics"

MetricName = Literal[
    "request_rate_rps",
    "error_rate_pct",
    "latency_p95_ms",
    "cpu_pct",
    "mem_used_pct",
    "disk_used_pct",
    "db_pool_active",
    "db_pool_max",
    "db_pool_wait_ms",
    "db_query_latency_p95_ms",
    "db_cpu_pct",
    "db_active_connections",
]
KNOWN_METRICS: frozenset[str] = frozenset(get_args(MetricName))

DESCRIPTION = (
    "Return per-minute samples and min/max/mean/first/last/n for one metric of one incident "
    "inside [start, end). start and end must be whole minutes. Known metrics: "
    + ", ".join(get_args(MetricName)) + "."
)


class QueryMetricsArgs(WindowArgs):
    metric: MetricName

    @model_validator(mode="after")
    def _whole_minutes(self) -> Self:
        # Keeps "<INC>:M:<metric>:<HHMM>-<HHMM>" unambiguous: one ID, one sample set.
        for dt in (self.start, self.end):
            if dt.second or dt.microsecond:
                raise ValueError("start and end must be whole minutes (seconds = 0)")
        return self


def query_metrics(
    incident_id: str,
    metric: str | None = None,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    **extra: Any,
) -> ToolResult:
    """Samples and statistics for one metric. Bad arguments or no samples return ok=False.

    Raises UnknownIncident or ChecksumMismatch for unregistered or tampered bundles.
    """
    raw = {"incident_id": incident_id, "metric": metric, "start": start, "end": end, **extra}
    args, _entry, failure = validate_args(TOOL_NAME, QueryMetricsArgs, raw)
    if failure is not None:
        return failure
    assert args is not None

    _, _, rows = load_bundle(args.incident_id)
    selected = sorted(
        ((parse_ts(r["ts"]), r) for r in rows if r["metric"] == args.metric),
        key=lambda pair: pair[0],
    )
    selected = [(ts, r) for ts, r in selected if args.start <= ts < args.end]
    window = f"{hhmm(args.start)}-{hhmm(args.end)}"
    if not selected:
        return ToolResult(tool=TOOL_NAME, ok=False,
                          error=f"no samples for {args.metric} in {window} UTC")

    values = [r["value"] for _, r in selected]
    first_row = selected[0][1]
    data = {
        "service": first_row["service"],
        "unit": first_row["unit"],
        "samples": [[r["ts"], r["value"]] for _, r in selected],
        "min": min(values),
        "max": max(values),
        "mean": round(fmean(values), 2),
        "first": values[0],
        "last": values[-1],
        "n": len(values),
    }
    evidence = Evidence(
        evidence_id=f"{args.incident_id}:M:{args.metric}:{window}",
        kind="metric",
        incident_id=args.incident_id,
        summary=(f"{args.metric} ({data['service']}, {data['unit']}) {window} UTC: "
                 f"n={data['n']} min={data['min']} max={data['max']} mean={data['mean']} "
                 f"first={data['first']} last={data['last']}"),
        data=data,
    )
    return ToolResult(tool=TOOL_NAME, ok=True, evidence=[evidence], total_matched=len(values))
