"""Command-line interface for the Incident Triage Agent."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from triage import __version__
from triage.errors import ConfigError, TransientLLMError

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
    return parser


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
                llm=FakeLLM(args.scenario) if args.mode == "offline-scripted" else None)
        except OSError:
            print("error: could not create or write the run trace in logs/", file=sys.stderr)
            return 1
        print(outcome.render())
        if outcome.status == "draft_ready":
            return 0
        return 2 if outcome.status in ("invalid_input", "config_error") else 1
    parser.print_help()
    return 0
