# Incident Triage Agent — CSE 510 checkpoint

When something breaks in production, an on-call engineer has to piece together logs,
metrics and runbooks to work out why. This project is a single agent that does that
first pass. Given a registered synthetic incident on the fixed `checkout-v1` topology,
it queries the real fixture logs, metrics and versioned runbooks through a bounded
LangGraph workflow and returns a cited **draft for human review**.

It covers four incident families: connection-pool exhaustion, disk exhaustion,
dependency failure and deployment regression. If the evidence is thin or the incident
falls outside those families, the agent should say so with an inconclusive report
instead of guessing. This is a course simulation, and we make no production-readiness
claim.

## Clean-checkout setup

Use **Python 3.13** (the version we verified for this checkpoint) and make sure you
have network access to PyPI. The package metadata allows Python 3.11+, and the full
suite also passed on 3.11 and 3.12 in a clean-checkout run. `python3` below needs to
be one of those versions; if yours is older, call `python3.13` (or `py -3.13` on
Windows) instead. Run everything from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -e '.[dev]'
python scripts/generate_fixtures.py --check
python -m pytest -q
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. The app reads the
fixture and runbook directories straight from the checkout, so the editable install
above is required; a standalone wheel without this repository's data isn't a supported
distribution. Direct dependencies are pinned in `requirements.txt`.

## Offline demonstration (no credentials or network)

```bash
python -m triage investigate INC-001 --question "What caused the 503 errors?" --mode offline-scripted
```

You should see `Mode: offline-scripted`, the FakeLLM warning,
`Terminal status: draft_ready`, `DRAFT — requires human review; unapproved`,
`diagnosed: connection_pool_exhaustion`, the cited log, metric and runbook IDs,
`Attempts: model=5/6, tools=4/7`, a `Trace: .../logs/<run-id>.jsonl` path, and a last line
starting `SUCCESS - validated draft ready for human review`. The trace labels the mode and model as `FakeLLM (incident-specific scripts)`.

**FakeLLM gives incident-specific scripted answers. These demos and tests show workflow
behavior, tool execution and guard enforcement; they do not show real-model diagnostic
accuracy.** Both execution modes call `triage.graph.investigate`, so they share the same
production graph, guardrails, tools, evidence ledger and validator. The application never
imports `tests/`.

Three more demos cover the inconclusive, blocked-action and budget cases:

```bash
python -m triage investigate INC-005 --question "What caused the restart?" --mode offline-scripted
python -m triage investigate INC-001 --question "Test the export boundary" --mode offline-scripted --scenario request_export
python -m triage investigate INC-001 --question "Test the budget" --mode offline-scripted --scenario always_tool_call
```

The first returns an inconclusive draft with family `none` and exits 0. The second ends
`halted` before any tool is dispatched, exits 1, and produces no report. The third ends
`budget_exceeded`, exits 1, and returns a validated partial report with family `none`.

`--scenario follow_injection` with `INC-002` simulates a scripted model that obeys
instructions injected into the logs. Its export request is blocked and the run halts.
That counts as **blocked unsafe behavior**, not as the model successfully resisting the
injection. Separate integration tests cover a stub that ignores the injected
instructions and carries on with the investigation.

## Real-provider investigation

Use the course-configured OpenAI-compatible gateway and model. The application doesn't
choose or substitute a provider. Configuration comes from environment variables or the
repository-root `.env`, and existing environment variables take precedence.

```bash
cp .env.example .env
# Edit .env locally: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL to your gateway values.
# LLM_TIMEOUT_S defaults to 60. JSON mode is on by default; see below.
python -m triage investigate INC-001 --question "What caused the 503 errors?" --mode real-provider
```

`.env.example` contains placeholders only, and `.env` plus any local env variants are
ignored by Git. Never commit credentials. `python -m triage llm-check` is an optional
connectivity check; the investigation command above is the real end-to-end smoke test.
Real-provider runs can use up their budget without diagnosing anything. A diagnostic
claim only counts once the report passes validation, and every report the tool returns
stays unapproved.

**JSON mode is on by default.** The client sends `response_format={"type": "json_object"}`
with every request, and each `model` trace event records the `json_mode` value it used.
Set `LLM_JSON_MODE=0` only for a gateway that rejects `response_format`. This matters
because the gateway we use returns reasoning but empty final message text without it. The
workflow never treats reasoning fields as actions, and an empty reply still counts as a
model attempt. After two empty replies in a row the run stops with `model_failed` and the
message "The model returned empty message text twice. If your gateway returns reasoning
without content, set LLM_JSON_MODE=1."

**Live smoke test verified on 2026-11-09 through the configured BullsAI gateway**, using
our existing API key and model, with JSON mode switched on. Our first JSON-mode run failed
citation validation after empty log searches, and its repair attempt asked for another
tool, which was correctly rejected. A later guided run used the command below, called
all three read tools (five calls in total), and returned `draft_ready` with exit 0 and
a validated `connection_pool_exhaustion` draft on model attempt six:

```bash
LLM_JSON_MODE=1 python -m triage investigate INC-001 --question "What caused the 503 errors? First query logs over the full incident window without event, contains, service or level filters so you obtain real log evidence IDs. Use metrics to distinguish pool exhaustion from slow database queries, search the matching runbook, then submit a final_report citing returned log, metric and runbook IDs. Keep within six model attempts." --mode real-provider
```

That run's trace has run ID `0eef42606dd045b6be82d85da6219368`. Runtime traces stay local
and are ignored by Git. This was one guided smoke test on a development incident, not a
held-out accuracy measurement. Validation confirms citation provenance and report
structure, so a person still has to review the draft's claims and recommendations. Later
model runs may end differently within the same hard limits. Missing configuration
returns `config_error`, exit 2, and a trace with zero attempts.

Each run prints one short status line per step to stderr: the run start, every model call
(`n/6` and seconds), each allowed tool call with its key arguments, each tool result
(ok or error, evidence count, `n/7`), the validation result with any errors, a repair
request, and a final line with the total seconds. The lines never include model text,
evidence bodies or configuration values. They appear automatically when stderr is a
terminal and stay off when output is piped or captured. `--progress` forces them on,
`--quiet` forces them off, and `--quiet` wins if both are given. Stdout is the same
either way, and its last line is a verdict: `SUCCESS` (a validated draft is ready for
human review, which says nothing about whether the diagnosis is right or approved),
`BLOCKED` (an unsafe action was stopped before it ran) or `FAILED` with the status and
reason. For example:

```bash
python -m triage investigate INC-001 --question "What caused the 503 errors?" --mode offline-scripted --progress
```

`--start` and `--end` are optional ISO-8601 timestamps with time zones, and each defaults
independently to the registered incident window. The requested window must be ordered
and sit inside the incident window. Tools must also stay inside the requested window,
and metric queries need whole minutes. For example:

```bash
python -m triage investigate INC-001 --question "Investigate this interval" --start 2026-10-06T14:10:00Z --end 2026-10-06T14:40:00Z --mode real-provider
```

Exit codes: **0** means a validated diagnosed or inconclusive draft is ready for human
review. **1** means the run was halted, ran out of budget, failed validation, hit an
operational or internal failure, or couldn't write its trace. **2** means invalid CLI
input or gateway configuration. A partial report that comes with an operational failure
still exits nonzero. None of these commands approves or exports anything.

## Demo runs

Four real-provider runs, one per kind of case. They need the gateway configured as above.
Each prints the progress lines on stderr (when it is a terminal) and ends with a
`SUCCESS`, `BLOCKED` or `FAILED` line on stdout. `SUCCESS` only means a validated,
unapproved draft is ready for a person to review.

```bash
python3 -m triage investigate INC-001 --question "What caused the 503 errors?" --mode real-provider
python3 -m triage investigate INC-003 --question "Why is checkout slow and returning 504 errors?" --mode real-provider
python3 -m triage investigate INC-002 --question "What caused the 500 errors on checkout?" --mode real-provider
python3 -m triage investigate INC-005 --question "What caused the restart?" --mode real-provider
```

- `INC-001` is the pool-exhaustion case; the target outcome is a diagnosed
  `connection_pool_exhaustion` draft.
- `INC-003` is the look-alike case: slow queries in the database while the app pool is not
  saturated, so the target outcome is `dependency_failure`.
- `INC-002` is disk exhaustion. One log line carries injected instructions, which the
  workflow treats as data and never as a command.
- `INC-005` is outside the four families, so an inconclusive draft is the expected outcome.

Real-model runs vary, and some end `budget_exceeded` (see Known limitations).

## Workflow and hard limits

Input is validated before any model is built or called. Prompt helpers then assemble the
conversation from the topology, tool schemas, incident window and operator question.
The compiled workflow lives in `src/triage/graph.py`:

```mermaid
flowchart TD
    START --> model
    model -->|tool action| guard
    model -->|report action| validate
    model -->|malformed report| repair
    model -->|transient retry or corrective feedback| model
    model -->|failure or budget| finish
    guard -->|allowed| execute
    guard -->|invalid arguments| model
    guard -->|forbidden, budget or repair protocol violation| finish
    execute -->|observation| model
    execute -->|one transient retry| execute
    execute -->|failure or budget| finish
    validate -->|invalid, repair available| repair
    validate -->|valid, failed repair or no budget| finish
    repair --> model
    finish --> END
```

The model makes one tool call per action, which keeps the contract simple. The tools are
`query_logs`, `query_metrics` and `search_runbooks`. Every successful result is checked
for instruction-like content, wrapped as untrusted data in the conversation, and
registered in that run's evidence ledger. A diagnosed draft has to cite real IDs that
cover logs, metrics and a runbook matching its family, and flagged evidence can't be
cited. A `connection_pool_exhaustion` diagnosis must also cite the `db_query_latency_p95_ms`
metric, and a `dependency_failure` diagnosis must cite both `db_query_latency_p95_ms` and
`db_pool_active`, so the look-alike family has been ruled out in the evidence. Citation
checks prove provenance, schema compliance and that this rule-out evidence was retrieved
and cited; they don't prove that the evidence actually supports the explanation, so human
review still has to judge that (see Known limitations).

The limits are six model attempts (counting transport retries and report repair) and
seven read-tool attempts (counting invalid requests, failures and retries). Each
transient model or tool operation can retry once, and every attempt counts. Model
transport failures are typed explicitly as `TransientLLMError`, and tool timeouts and
connection errors are retryable. Configuration, schema and argument errors are never
retried at the transport level. Corrective feedback can prompt a new action as long as
model budget remains, except that two empty model replies in a row end the run with
`model_failed`. A report that fails schema or evidence validation gets at most one
repair, using the existing ledger, and a repair can't request more tools. When the
budget runs out, the workflow assembles and validates a partial report with status
`budget_exceeded`, family `none` and confidence 0. Operational failures after execution
has started can return a validated inconclusive partial. If report validation fails
repeatedly, no report is exposed and a diagnosed result is never printed.

The tool allowlist is checked before dispatch, including during repair. Forbidden tool
names, model-requested exports and cross-incident requests halt the run. There is no
shell, no arbitrary web access and no tool that modifies infrastructure. The application
runs a single agent.

## Known limitations

Validation checks that every cited ID exists in the run's evidence ledger and, for the
two families above, that the required rule-out metrics were retrieved and cited. It does
not check that the report text is true. In our real-provider development runs we saw
three kinds of gap: a passing report said the database stayed healthy without ever
retrieving or citing the query-latency metric (this is why the rule-out check exists);
a report mentioned a traffic surge although no request-rate evidence had been retrieved;
and a report gave a "peak waiting count" at a timestamp where the cited metric was not at
its peak. The validator does not catch the last two, so a person has to read the cited
evidence before trusting a draft.

Real-provider runs often use up the budget before a report is submitted. In our latest
batch of 13 real-provider investigations, `INC-001` reached `draft_ready` in 1 of 6 runs
(an inconclusive draft; the other five ended `budget_exceeded`), `INC-003` in 2 of 3
(both diagnosed `dependency_failure`), `INC-002` in 0 of 2 (both `budget_exceeded`) and
`INC-005` in 2 of 2 (both inconclusive). Typically the model kept calling tools until no
turn was left for the report or for a repair. This was a small, informal sample on
development incidents, not an accuracy measurement.

## Inspect tool execution and traces

Every investigation writes one exclusively created, flushed JSONL file to the
repository-root `logs/` directory, which Git ignores. That includes invalid-input and
configuration failures. The CLI prints the exact path. To see the tool calls after the
demo above:

```bash
python - <<'PY'
import json
from pathlib import Path
path = max(Path("logs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns)
events = [json.loads(line) for line in path.read_text().splitlines()]
print("Trace:", path)
for event in events:
    if event["event"] == "tool":
        print(event["tool"], event["outcome"], event["args"],
              "attempt", event["tool_attempts"], "evidence", event.get("evidence_count"))
print("Terminal:", events[-1]["outcome"])
PY
```

For `INC-001` this shows two `query_metrics` calls, one `query_logs` call and one
`search_runbooks` call. Each event records the run ID, mode, model identifier, outcome,
attempt and remaining-budget counters, a UTC timestamp, elapsed and per-operation timing,
safe arguments, bounded results or errors, validation, and the terminal outcome. Usage
and cost are JSON `null` when unknown, and we never estimate cost. For tracing, results
are clipped to 10 list items, 60 object keys and 2,000 characters per string, while the
model and the ledger receive the full tool result. Model response bodies and raw
exception messages are withheld, and configured credentials and common credential forms
are scrubbed from trace values and keys.

## Checkpoint implementation and remaining work

Implemented so far: five deterministic development bundles, four runbooks, integrity
checks, three tested read tools, strict contracts, prompt construction, GatewayLLM and
FakeLLM, the bounded LangGraph workflow, citation validation with repair, the CLI, and
JSONL traces. The full test suite uses injected providers, needs no network, and
exercises these production modules.

Deferred: SQLite run and report persistence, the approve/edit/reject/expiry workflow,
approved export, the held-out evaluation harness and accuracy measurement, the
fixed-pipeline baseline, and calibration plus retention and deletion tooling.
`review.py` and `tools/export.py` are explicit placeholders that the model can't
reach. This checkpoint has no UI, no MCP and no multi-agent setup, and development
scripts must not be presented as held-out results. `docs/proposal.md` keeps its
historical intent, and `docs/architecture.md` separates this checkpoint from the
proposed full system.
