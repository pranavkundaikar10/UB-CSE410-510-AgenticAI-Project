"""Read-only investigation tools and the gated export tool."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from triage.schemas import ToolResult
from triage.tools import logs, metrics, runbooks


@dataclass(frozen=True)
class ToolSpec:
    func: Callable[..., ToolResult]
    args_model: type[BaseModel]
    description: str


# The model-facing allowlist. export_report is deliberately NOT here: only the
# EXPORT state may call it, after a matching human approval.
READ_TOOLS: Mapping[str, ToolSpec] = MappingProxyType({
    logs.TOOL_NAME: ToolSpec(logs.query_logs, logs.QueryLogsArgs, logs.DESCRIPTION),
    metrics.TOOL_NAME: ToolSpec(metrics.query_metrics, metrics.QueryMetricsArgs, metrics.DESCRIPTION),
    runbooks.TOOL_NAME: ToolSpec(runbooks.search_runbooks, runbooks.SearchRunbooksArgs, runbooks.DESCRIPTION),
})


def tool_catalog() -> list[dict[str, Any]]:
    """Name, description and JSON schema of each read tool, for the system prompt."""
    return [
        {"name": name, "description": spec.description,
         "parameters": spec.args_model.model_json_schema()}
        for name, spec in READ_TOOLS.items()
    ]


__all__ = ["READ_TOOLS", "ToolSpec", "tool_catalog"]
