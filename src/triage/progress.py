"""One short status line per workflow step, for the optional --progress flag.

Lines are built only from trace-record fields that are safe to show: counters, tool
names, a few tool arguments, evidence counts, validation messages and timings. Model
text, evidence bodies and configuration values are never included.
"""

from __future__ import annotations

from typing import Any

from triage.config import MAX_MODEL_TURNS, MAX_TOOL_CALLS

MAX_ARG_CHARS = 60
# Tool arguments worth showing; incident_id, start and end are the same on every call.
_KEY_ARGS = ("metric", "service", "level", "event", "contains", "limit", "query")


def _seconds(record: dict[str, Any]) -> str:
    return f"{(record.get('duration_ms') or 0) / 1000:.1f}s"


def _key_args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    parts = []
    for name in _KEY_ARGS:
        if args.get(name) is not None:
            value = str(args[name])
            if len(value) > MAX_ARG_CHARS:
                value = value[:MAX_ARG_CHARS] + "..."
            parts.append(f"{name}={value}")
    return " ".join(parts) if parts else "full window, no filters"


def start_line(incident_id: str, mode: str, model: str | None) -> str:
    """Run-start line. The model name is shown; no other configuration is."""
    return f"[start] {incident_id} mode={mode} model={model or 'not configured'}"


def format_progress(record: dict[str, Any]) -> str | None:
    """Return the progress line for one cleaned trace record, or None to stay quiet."""
    event, outcome = record["event"], record["outcome"]
    model_n = f"{record['model_attempts']}/{MAX_MODEL_TURNS}"
    tool_n = f"{record['tool_attempts']}/{MAX_TOOL_CALLS}"
    # The start line is emitted by graph.investigate once the model name is known.
    if event == "model":
        if outcome == "completed":
            return f"[model {model_n}] {_seconds(record)}"
        return f"[model {model_n}] {outcome.replace('_', ' ')}, {_seconds(record)}"
    if event == "parse" and outcome == "empty_reply":
        return f"[model {model_n}] empty reply ({record.get('consecutive')} in a row)"
    if event == "parse":
        return f"[model {model_n}] not a valid action"
    if event == "guard":
        if outcome == "allowed":
            # The guard runs before the attempt is counted, so number the call about to start.
            upcoming = f"{record['tool_attempts'] + 1}/{MAX_TOOL_CALLS}"
            return f"[tool {upcoming}] {record.get('tool')} {_key_args(record.get('args'))}"
        if outcome == "invalid_arguments":
            return f"[tool {tool_n}] refused: invalid arguments"
        return "[guard] request blocked"
    if event == "tool":
        if outcome in ("ok", "error"):
            return (f"[tool {tool_n}] {outcome}, {record.get('evidence_count', 0)} evidence, "
                    f"{_seconds(record)}")
        return f"[tool {tool_n}] {outcome.replace('_', ' ')}"
    if event == "validate":
        if outcome == "valid":
            return "[validate] valid"
        return "[validate] invalid: " + "; ".join(record.get("errors") or [])
    if event == "repair":
        return "[repair] requested; one model turn, no tools"
    if event == "terminal":
        return (f"[done] status={outcome} model={model_n} tools={tool_n} "
                f"{record['elapsed_ms'] / 1000:.1f}s total")
    return None
