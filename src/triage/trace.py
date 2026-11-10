"""Bounded, secret-scrubbed JSONL events, flushed after every transition."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, path: Path, run_id: str, mode: str, model: str | None,
                 secrets: tuple[str, ...] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.run_id, self.mode, self.model = path, run_id, mode, model
        self.secrets = tuple(s for s in (*secrets, os.getenv("LLM_API_KEY"),
                                        os.getenv("OPENAI_API_KEY")) if s)
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
        self.stream.write(json.dumps(self.clean(record), ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()
