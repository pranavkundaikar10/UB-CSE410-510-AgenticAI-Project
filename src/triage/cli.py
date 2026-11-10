"""Command-line interface for the Incident Triage Agent."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from triage import __version__
from triage.errors import ConfigError, TransientLLMError

if TYPE_CHECKING:  # graph pulls in langgraph; keep it out of the llm-check path
    from triage.graph import Outcome

LLM_CHECK_MESSAGES = [
    {"role": "system", "content": "You are a connectivity check. Reply with one JSON object only."},
    {"role": "user", "content": 'Reply with exactly: {"ok": true}'},
]


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Investigate a registered synthetic incident and draft a cited report "
        "for human review.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser(
        "llm-check",
        help="send one tiny prompt to the configured LLM gateway and report latency",
        description="Send one tiny prompt through the configured gateway (.env or environment) "
        "and print the model name, latency and whether the reply parsed as JSON.",
    )
    investigation = sub.add_parser("investigate", help="investigate an incident and produce a validated draft")
    investigation.add_argument("incident_id", help="registered incident, e.g. INC-001")
    investigation.add_argument("--question", required=True)
    investigation.add_argument("--start", help="ISO-8601 with zone; defaults to incident start")
    investigation.add_argument("--end", help="ISO-8601 with zone; defaults to incident end")
    investigation.add_argument("--mode", choices=["real-provider", "offline-scripted"],
                               default="real-provider")
    investigation.add_argument("--scenario", choices=["normal", "always_tool_call", "bad_json_once",
        "request_export", "cite_fabricated", "follow_injection"], default="normal",
        help="scripted test scenario; only valid with --mode offline-scripted")
    investigation.add_argument("--progress", action="store_true",
        help="force the per-step status lines on stderr (they are on by default when stderr is a terminal)")
    investigation.add_argument("--quiet", action="store_true",
        help="turn the status lines off; wins over --progress")
    return parser


def _print_progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _show_progress(args: argparse.Namespace) -> bool:
    """On for a terminal, off when piped or captured; --progress forces on, --quiet wins."""
    if args.quiet:
        return False
    return args.progress or sys.stderr.isatty()


def _use_color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


_COLORS = {"SUCCESS": "32", "BLOCKED": "33", "FAILED": "31"}


def verdict_line(outcome: Outcome, *, color: bool = False) -> str:
    """The last stdout line. SUCCESS only means a validated, unapproved draft exists."""
    report = outcome.report
    if outcome.status == "draft_ready" and report is not None:
        keyword = "SUCCESS"
        if report.status == "diagnosed":
            rest = f"validated draft ready for human review (diagnosed: {report.family})"
        else:
            rest = "validated inconclusive draft ready for human review"
    elif outcome.status == "halted":
        keyword, rest = "BLOCKED", "an unsafe action was stopped before it ran"
    else:
        keyword = "FAILED"
        reason = outcome.state["halt_reason"]
        rest = f"{outcome.status}: {reason}" if reason else outcome.status
    shown = f"\033[{_COLORS[keyword]}m{keyword}\033[0m" if color else keyword
    return f"{shown} - {rest}"


def llm_check() -> int:
    """Connectivity check against the configured gateway. Never prints the API key."""
    from triage.llm import GatewayLLM, extract_json_objects

    try:
        llm = GatewayLLM()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        raw = llm.complete(LLM_CHECK_MESSAGES)
    except (ConfigError, TransientLLMError) as exc:
        print(f"model:   {llm.model}", file=sys.stderr)
        print(f"error:   {exc}", file=sys.stderr)
        return 1
    latency_ms = (time.perf_counter() - started) * 1000
    parsed = len(extract_json_objects(raw)) == 1
    print(f"model:   {llm.model}")
    print(f"latency: {latency_ms:.0f} ms")
    print(f"json:    {'yes' if parsed else 'no'}")
    return 0 if parsed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "llm-check":
        return llm_check()
    if args.command == "investigate":
        if args.mode != "offline-scripted" and args.scenario != "normal":
            parser.error("--scenario requires --mode offline-scripted")
        from triage.graph import investigate
        from triage.fake_llm import FakeLLM

        print(f"Mode: {args.mode}")
        if args.mode == "offline-scripted":
            print("FakeLLM uses incident-specific scripted answers; this does not measure diagnostic accuracy.")
        try:
            outcome = investigate(args.incident_id, args.question,
                start=args.start, end=args.end, mode=args.mode,
                llm=FakeLLM(args.scenario) if args.mode == "offline-scripted" else None,
                progress=_print_progress if _show_progress(args) else None)
        except OSError:
            print("error: could not create or write the run trace in logs/", file=sys.stderr)
            return 1
        print(outcome.render())
        print(verdict_line(outcome, color=_use_color()))
        if outcome.status == "draft_ready":
            return 0
        return 2 if outcome.status in ("invalid_input", "config_error") else 1
    parser.print_help()
    return 0
