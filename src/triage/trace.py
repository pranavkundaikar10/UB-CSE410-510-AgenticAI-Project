"""Bounded, secret-scrubbed JSONL events, flushed after every transition."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from triage.progress import format_progress


class TraceWriter:
    def __init__(self, path: Path, run_id: str, mode: str, model: str | None,
                 secrets: tuple[str, ...] = (),
                 progress: Callable[[str], None] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.run_id, self.mode, self.model = path, run_id, mode, model
        self.secrets = tuple(s for s in (*secrets, os.getenv("LLM_API_KEY"),
                                        os.getenv("OPENAI_API_KEY")) if s)
        self.progress = progress  # optional sink for one short line per event
        self.started = time.perf_counter()
        self.stream = path.open("x", encoding="utf-8")

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)Bearer\s+\S+|\bsk-[a-zA-Z0-9_-]+", "[REDACTED]", value)
            return value[:2000]
        if isinstance(value, dict):
            return {self.clean(str(k))[:80]: ("[REDACTED]" if re.search(
                r"(?i)api.?key|authorization|password|secret", str(k)) else self.clean(v))
                for k, v in list(value.items())[:60]}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value[:10]]
        return value

    def event(self, event: str, outcome: str, state: dict[str, Any], **details: Any) -> None:
        record = dict(run_id=self.run_id, mode=self.mode, model=self.model,
                      timestamp=datetime.now(timezone.utc).isoformat(),
                      elapsed_ms=round((time.perf_counter() - self.started) * 1000, 3),
                      event=event, outcome=outcome,
                      model_attempts=state.get("model_turns", 0),
                      tool_attempts=state.get("tool_calls", 0),
                      model_remaining=6 - state.get("model_turns", 0),
                      tool_remaining=7 - state.get("tool_calls", 0), **details)
        record = self.clean(record)
        self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stream.flush()
        if self.progress is not None:
            line = format_progress(record)
            if line:
                self.progress(self.clean(line))

    def note(self, line: str) -> None:
        """Send one line to the progress sink only; it is not written to the JSONL trace."""
        if self.progress is not None:
            self.progress(self.clean(line))

    def close(self) -> None:
        self.stream.close()
