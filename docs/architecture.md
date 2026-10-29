# Incident Triage Agent: Architecture Document (Midterm Review)

**CSE 410/510 Introduction to Agentic Engineering, Fall 2026**

**Team:** Pranav Kundaikar; Shubhojyoti Datta; Pooja Nemade; Jay Pathare

**Scope:** an offline simulation on synthetic incident bundles with one fixed application topology. It covers four incident families: connection-pool exhaustion, disk exhaustion, dependency failure and application errors after a deployment. Anything outside these families must end in abstention. We make no production-readiness claim. This is a proposed design and does not claim that its safeguards or tests are already built.

---

## 1. Component list

| Name | What it does, in one sentence | What it reads | What it writes |
|---|---|---|---|
| CLI (user entry point) | Takes the investigation request (incident ID, question, time window) and the reviewer's commands. | Typed arguments and the report shown at review. | The request to the workflow and the reviewer's decision. |
| Human reviewer | Checks the diagnosis, evidence and next checks, then approves, edits or rejects the report. | Draft report, cited evidence and status. | A decision with a reason and optional edits, through the CLI. |
| Workflow graph (LangGraph) | Runs the state machine and counts the budgets. | Run state, model output, tool results and review decisions. | Run state to SQLite and trace events to `logs/`. |
| Agent (LLM model turn) | Picks the next tool call or drafts the report from the evidence so far. | Question, system prompt, tool results and runbook passages. | Tool-call requests and a structured draft report (Pydantic). |
| Guardrails (`guardrails.py`) | Enforces the tool allowlist, argument schemas, output path and approval check in code. | Tool-call requests, export request, approval record and configuration. | Allow or deny decisions to the trace. |
| Report validator | Checks the report schema and that every cited evidence ID exists in this run. | Draft report and the run's evidence IDs. | A pass or fail result with any bad citations. |
| `query_logs` tool | Returns bounded log events or counts for one incident with evidence IDs. | `logs.jsonl` of the named incident. | Nothing. |
| `query_metrics` tool | Returns metric samples and statistics for one incident with evidence IDs. | `metrics.csv` of the named incident. | Nothing. |
| `search_runbooks` tool | Returns ranked runbook passages with passage IDs and versions. | The runbook retrieval index. | Nothing. |
| `export_report` tool | Writes the approved report and an audit record. | Validated report and approval record. | One Markdown report and one audit record in the output directory. |
| Incident logs (data source) | Holds synthetic timestamped JSONL events for registered incidents. | Nothing (static files). | Nothing. |
| Incident metrics (data source) | Holds synthetic timestamped CSV measurements for registered incidents. | Nothing (static files). | Nothing. |
| Runbooks (data source) | Holds 6 to 8 versioned Markdown runbooks. | Nothing (static files). | Nothing. |
| Runbook retrieval index (data source) | Keeps a persistent index of runbook passages keyed by version. | Runbook files at build time. | The index, on rebuild only. |
| Incident manifest (data source) | Lists the allowed incident IDs and their bundle files. | Versioned fixture registry. | Nothing. |
| SQLite store (data source) | Holds run state, evidence registry, report versions and review decisions. | Rows for the current run. | Run state, report versions and review decisions. |
| Run traces (data sink) | Keeps one JSONL trace per run in `logs/`. | Nothing. | Model turns, tool calls, budget counters, status, review decision, latency and token cost. |
| Output directory (data sink) | Is the one restricted folder for exported reports. | Nothing. | Exported reports and audit records, written only by `export_report`. |
| LLM API (external service) | Provides the tool-capable language model. | The prompt from the workflow. | Model responses. |
| Evaluation harness | Runs the held-out cases and scores them against gold labels. | Test suite, gold labels (hidden from the agent) and traces. | Results in `evaluation/results/`. |

---

## 2. Environment

### Data sources the agent can access

| Source | Format | How it is accessed |
|---|---|---|
| Incident logs | JSONL | `query_logs`. Read-only, one registered incident ID per call and bounded output with a truncation flag. |
| Incident metrics | CSV | `query_metrics`. Read-only and one registered incident ID per call. |
| Incident manifest | Versioned local file (exact format is an open risk until built) | Input validation looks up the incident ID in an allowlist. No free-form paths. |
| Runbooks | Markdown, 6 to 8 versioned files | `search_runbooks` over the retrieval index. Read-only. |
| Runbook retrieval index | Persistent local index keyed by runbook version | `search_runbooks` returns only version-compatible passages. |
| Run state and review decisions | SQLite | Workflow code only. The model never queries it. |
| Output directory | Local folder | Written only by `export_report` after approval. |

### Data sources the agent cannot access

| Excluded source | Why |
|---|---|
| Live servers, production systems and credentials | This is an offline simulation. |
| Arbitrary web content | It removes an injection and data-leak path. All evidence comes from registered bundles. |
| Shell execution and infrastructure changes | The agent only advises and never runs remediation. |
| Gold labels and the test suite | The agent must never see the answers or the evaluation is invalid. |
| Other incidents' bundles | Each call is scoped to one incident ID to prevent cross-incident leaks. |
| Run traces, source code and configuration (including API keys) | No tool reads them. |

### External services

| Service | Limit | Authentication |
|---|---|---|
| One tool-capable LLM API with the model version pinned in configuration | Our limits: a timeout per request, 6 model turns and 8 tool attempts per investigation (including export), one retry per failed model or read call and token cost logged per run. The provider's rate limit and the model choice are an **open risk** to record before building. | API key from an environment variable. It is never committed, never put in prompts and never reachable by a tool. |

There are no other external services. All data sent to the LLM API is synthetic or sanitized.

---

## 3. Tool inventory

| Tool | Input | Output | Error cases | Risk | Gate or hard limit |
|---|---|---|---|---|---|
| `query_logs` | Incident ID, time range, service or filter | Bounded events or counts, evidence IDs, truncation flag | Unknown ID, malformed records, invalid range, timeout | read | None needed. Read-only and scoped to one registered incident. |
| `query_metrics` | Incident ID, metric, time range | Samples and statistics with evidence IDs | Missing metric, too few samples, timeout | read | None needed. Read-only and scoped to one registered incident. |
| `search_runbooks` | Query, service, version | Ranked passages with IDs and versions | Empty results, incompatible version, unavailable index | read | None needed. Read-only. |
| `export_report` | Validated report ID, human approval record | Final Markdown report and audit record | Missing approval, changed report, invalid citations, invalid path, file already exists, disk write error | write | Hard limits in code. It is not on the model's tool allowlist and only the `EXPORT` state calls it after an approval. The approval must match the exact report version hash. Code generates the output path and keeps it inside the output directory. Existing files are never overwritten. |

### Attempt accounting

There are at most 8 attempts across all four tools, counting failures and retries. The three read tools share a cap of 7 and the eighth is reserved for one approved export. Export is never retried automatically, so a failed write cannot break the limit or leave a duplicate. The 6 model turns include provider retries and the report repair.

---

## 4. Transition table

**Entry state:** `INTAKE`.

**Success terminal state:** `EXPORTED`.

**Failure terminal states:** `INPUT_REJECTED`, `HALTED`, `VALIDATION_FAILED`, `REJECTED`, `REVIEW_EXPIRED` and `EXPORT_FAILED`.

**Gate state:** `HUMAN_REVIEW`, with one row for each exit (T20 approve, T21 edit, T22 reject, T23 no response).

| # | From state | Event | To state | Budget, if this row can repeat |
|---|---|---|---|---|
| T1 | `INTAKE` | CLI receives incident ID, question and time window | `VALIDATE_INPUT` | Once per run |
| T2 | `VALIDATE_INPUT` | Incident ID registered and time window valid | `MODEL_TURN` | Once per run |
| T3 | `VALIDATE_INPUT` | Unknown incident ID or invalid time window | `INPUT_REJECTED` | None |
| T4 | `MODEL_TURN` | Model asks for read-tool calls that pass the schema, allowlist and budget checks | `EXECUTE_TOOLS` | At most 6 model turns and 7 read attempts per run |
| T5 | `MODEL_TURN` | Tool call fails the schema check (nothing runs and the error goes back as an observation) | `MODEL_TURN` | Counts as one of the 6 turns |
| T6 | `MODEL_TURN` | Model asks for something outside the boundary (tool not on the allowlist, shell, web, remediation, export or a path outside the output directory) | `HALTED` | None |
| T7 | `MODEL_TURN` | Model API call fails or times out and was not retried | `MODEL_TURN` (same call retried) | 1 retry per call. The retry counts as a turn. |
| T8 | `MODEL_TURN` | Model API call fails again after the retry | `ASSEMBLE_PARTIAL` (status `inconclusive`, failed service named) | None |
| T9 | `MODEL_TURN` | The next step would pass 6 model turns or 7 read attempts before a draft is submitted | `ASSEMBLE_PARTIAL` (status `budget_exceeded`) | 1 of the 8 attempts stays reserved for export |
| T10 | `MODEL_TURN` | Model submits a draft report (`diagnosed` or `inconclusive`) | `VALIDATE_REPORT` | None |
| T11 | `EXECUTE_TOOLS` | All requested calls return results | `MODEL_TURN` | Within the 6-turn budget |
| T12 | `EXECUTE_TOOLS` | Deterministic tool error (unknown ID, missing metric, invalid range, empty results or incompatible runbook version), returned as an observation and not retried | `MODEL_TURN` | Within the 6-turn budget |
| T13 | `EXECUTE_TOOLS` | Transient failure (timeout or unavailable index) on a call not yet retried, with fewer than 7 read attempts used | `EXECUTE_TOOLS` (same call retried) | 1 retry per call. Each attempt counts toward the 7 read and 8 total cap. |
| T14 | `EXECUTE_TOOLS` | The same call fails again after the retry | `ASSEMBLE_PARTIAL` (status `inconclusive`, failed tool named) | None |
| T15 | `ASSEMBLE_PARTIAL` | Code builds a partial report from the collected evidence IDs with no model call | `VALIDATE_REPORT` | Once per run |
| T16 | `VALIDATE_REPORT` | Schema valid and every citation ID resolves | `HUMAN_REVIEW` | None |
| T17 | `VALIDATE_REPORT` | A model-drafted report fails the check, the repair is unused and a model turn remains | `REPAIR` | 1 repair per run |
| T18 | `REPAIR` | Model returns a corrected draft | `VALIDATE_REPORT` | The repair counts as a model turn |
| T19 | `VALIDATE_REPORT` | A model-drafted report is still invalid after its permitted repair, no model turn remains for a repair or a code-assembled partial report fails validation | `VALIDATION_FAILED` | None |
| T20 | `HUMAN_REVIEW` | Reviewer **approves** | `EXPORT` | None |
| T21 | `HUMAN_REVIEW` | Reviewer **edits** the report (new version and any earlier approval is void) | `VALIDATE_REPORT` | At most 2 edit rounds per report. A third is refused and the gate stays open. |
| T22 | `HUMAN_REVIEW` | Reviewer **rejects** | `REJECTED` | None |
| T23 | `HUMAN_REVIEW` | **No response** within 24 hours of first entering the gate | `REVIEW_EXPIRED` | The deadline counts from first entry and edits do not reset it |
| T24 | `VALIDATE_REPORT` | A reviewer-edited version fails the check | `HUMAN_REVIEW` (edit refused and the previous version stays) | Counts as one of the 2 edit rounds |
| T25 | `EXPORT` | Approval matches the report version hash, citations still validate, the path is inside the output directory and the file does not exist | `EXPORTED` | One attempt only |
| T26 | `EXPORT` | Approval missing or mismatched, path outside the output directory or file already exists | `HALTED` | None |
| T27 | `EXPORT` | Disk write error | `EXPORT_FAILED` | One attempt only and no retry |

### Report statuses

A report enters review as `diagnosed`, `inconclusive` or `budget_exceeded`. The statuses `validation_failed` and `halted` match the terminal states of the same name. An approved `inconclusive` report counts as a success because abstaining is correct when the evidence cannot tell causes apart.

---

## 5. Memory table

| Type | Contents | Store | Lifespan | Deletion key |
|---|---|---|---|---|
| In-context | Question, system prompt, tool results and evidence IDs for this run | LLM prompt | One investigation. Dropped at any terminal state. | `run_id` (never saved by itself) |
| Runbook retrieval index | Passages from 6 to 8 runbooks with passage IDs and versions | Local index on disk, rebuilt by a script | Persists across runs and is replaced when a runbook version changes | `runbook_version` |
| Investigation state | Run state, budget counters, evidence registry and report versions with hashes | SQLite | Kept through course evaluation. Retention after the course is an **open risk**. | `run_id` |
| Review decisions | Decision, reviewer, timestamp, report hash, edit diff and reason | SQLite | Kept with the run through course evaluation. Later retention is an **open risk**. | `run_id` and `report_id` |
| Run traces | Model turns, tool calls and results, budget counters, status, latency and token cost | One JSONL file per run in `logs/` | Kept through course evaluation. Later retention is an **open risk**. | `run_id` |
| Exported reports | Approved final report and audit record | Output directory | Kept through course evaluation. Later retention is an **open risk**. | `report_id` and version |

The agent keeps no memory of past diagnoses across investigations. All stored data is synthetic or sanitized. **Open risk:** the team still has to build the post-course deletion step. The deletion keys show how each record can be found. We do not claim a purge command exists yet.

---

## 6. Failure analysis

| Failure | Kind | Handling state | What the user sees |
|---|---|---|---|
| Missing metric, unknown ID, invalid range or empty runbook results | Tool error | `EXECUTE_TOOLS` to `MODEL_TURN` (T12) | The error appears in the trace and in the report's tool-error list. The agent tries another query or ends `inconclusive` and names the gap. |
| A tool times out once and then works | Timeout | `EXECUTE_TOOLS` retry (T13) | A normal report. The retry shows in the trace and counters. |
| A tool times out twice | Timeout | `EXECUTE_TOOLS` to `ASSEMBLE_PARTIAL` (T14) | An `inconclusive` report that names the failed tool, sent to review. |
| Model sends a malformed tool call | Output breaks the schema | `MODEL_TURN` (T5) | Nothing runs. The model gets the schema error and the turn is counted. |
| Final report fails the schema or cites IDs that do not exist | Output breaks the schema | `VALIDATE_REPORT`, `REPAIR`, then `VALIDATION_FAILED` (T17 to T19) | One automatic repair. If it fails the status is `validation_failed`, the bad citations are listed and nothing is exported. |
| 6 model turns or 7 read attempts used before a conclusion | Budget exhausted | `ASSEMBLE_PARTIAL` (T9) | A partial `inconclusive` draft with status `budget_exceeded` and the budget counters, sent to review. The eighth attempt stays reserved for export. |
| Reviewer does not respond within 24 hours | Reviewer no response | `HUMAN_REVIEW` to `REVIEW_EXPIRED` (T23) | Status `review_expired`. Nothing is exported and the report stays unapproved in SQLite. A new run is needed. |
| Injected instructions in log text | Untrusted input | Treated as data in `MODEL_TURN`. If the model obeys and asks for a forbidden action, T6 applies. | If ignored the run continues normally. If obeyed the action is blocked, the run ends `halted` and the test case counts as failed. |
| Export requested without approval or outside the output directory | Boundary violation | `MODEL_TURN` (T6) or `EXPORT` (T26) | A refusal and status `halted`, with the violation logged. |
| Incident outside the four families or evidence that cannot tell causes apart | Out of scope | `MODEL_TURN` to `VALIDATE_REPORT` | An `inconclusive` report that explains what evidence was missing or in conflict. |
| LLM API unavailable | External service error | `MODEL_TURN` (T7 then T8) | One retry, then a partial `inconclusive` report that names the model API. |

---

## 7. Human-oversight plan

### Gated actions and rules

| Action | Gate rule |
|---|---|
| `export_report` (the only write) | Gated before every export with no exception. The approval binds to the exact report version hash. |
| Any reviewer edit | The edited version is checked again and needs a fresh approval. Any earlier approval is void. |

### Threshold from two stated costs

The unit is engineer-minutes.

- `L` is the cost of acting on a wrong exported diagnosis: 60 engineer-minutes, the time an on-call engineer spends on the wrong cause before noticing.
- `r` is the cost of one human review: 3 engineer-minutes.
- `t = 1 − r / L = 1 − 3/60 = 0.95`.

Let `p` be the calibrated probability that one report's diagnosis is correct. If review reliably catches a wrong diagnosis, skipping review has expected loss `(1 − p)·L` and reviewing costs `r`. Skipping would only pay off when `p ≥ t = 0.95`. **Our system never skips review.** We cannot calibrate per-report probabilities and 20 held-out cases are too few to validate an automatic-export rule. The 80% accuracy target is an overall target and says nothing about any single report. `L`, `r` and perfect review are assumptions. The team will measure review time during evaluation and revisit them (**open risk**).

### Deadline and fallback

No response counts after 24 hours from first entry to `HUMAN_REVIEW`. The fallback fails closed: status `review_expired`, nothing exported and no automatic approval.

### Logged for every review decision

`run_id`, `report_id`, report version hash, reviewer ID, decision (approve, edit, reject or expired), timestamp and time to decide, the required reason for an edit or reject, the edit diff, the report status at review, the evidence IDs shown, the budget counters and the pinned model version. Entries go to SQLite and the run trace.

---

## 8. Threat table

| Asset | Adversary | Way in | Control and its kind |
|---|---|---|---|
| Output directory and file system | Injected log text or a malicious request | Path traversal in an export path | Code generates the path and keeps it inside the output directory. Overwrites are refused. **Hard limit** |
| Export action (the only write) | Injected log text that tells the agent to export | Log text, runbook text or the user question | `export_report` is not on the model's allowlist and needs an approval record that matches the report hash. Code generates filenames, so user-suggested names or paths are rejected. **Hard limit** |
| Host system and infrastructure | Injected text or a user asking for command execution | Log text or the user question | No shell or remediation tool exists. Unknown tool names end the run as `halted`. **Scope** |
| Diagnosis integrity | Injected log text or misleading evidence that steers the conclusion | Log text and metric labels | Log text goes in as data inside a marked block and never as instructions. Code checks citation IDs and the reviewer checks that they support the claim. **Soft limit** (prompt framing) with **detection** at review |
| Investigation budget and cost | Looping or malformed input that burns turns and tokens | Any untrusted text that causes repeated queries | 6 model turns, 7 read attempts and 1 reserved export attempt (8 total), with one retry per failed read call, all enforced in code. **Hard limit** |
| Real or private data | Accidental leak to the LLM API | A non-synthetic file added to the incident folder | Only registered synthetic bundles can be queried by incident ID. **Scope** |
| Stale guidance | An outdated runbook treated as current | Runbook text | Every passage shows its runbook version and a mismatch is stated in the report. **Detection** |

### Every place untrusted text enters the system

1. Log message fields and other JSONL fields in the incident bundle.
2. CSV metric names, labels and service names in the incident bundle.
3. Runbook passages returned by `search_runbooks`.
4. The question, incident ID and time window typed into the CLI.
5. Tool error messages that echo input text.
6. The LLM's output (tool-call arguments and report text), which code checks before use.
7. Reviewer edits and free-text reasons.
8. Manifest data (incident IDs and file identifiers).
9. Model API error messages.

---

## 9. Design defense

### Decision 1: adaptive tool choice in a bounded graph, not a preplanned or fixed pipeline

The agent picks each query after seeing the last result (ReAct; Yao et al., ICLR 2023, <https://arxiv.org/abs/2210.03629>). We considered **ReWOO** (Xu et al., 2023, <https://arxiv.org/abs/2305.18323>), which plans every tool call before seeing any result to save model calls. We also considered a fixed pipeline of logs, then metrics, then runbooks. Our hardest case has database timeouts with normal pool use, which points to a dependency failure. The next query there depends on a measurement, so a plan fixed up front fits poorly even though it is cheaper. The fixed pipeline stays as our evaluation baseline on the same model and data.

### Decision 2: every export is gated, with no confidence-based auto-export

This follows maximum expected utility (Russell and Norvig, *Artificial Intelligence: A Modern Approach*, **4th US ed., Ch. 16, "Making Simple Decisions"**, <https://aima.eecs.berkeley.edu/>). We compare the expected loss from a wrong diagnosis with the cost of a review. Under the Section 7 assumptions review is better whenever the chance of being correct is below `t = 1 − r/L = 0.95`. We rejected auto-export above a confidence cutoff for three reasons. Model confidence is uncalibrated. Twenty test cases cannot show a safe cutoff. Approval is also a firm safety boundary and not a probabilistic exception.

### Decision 3: no memory of past diagnoses, only a versioned runbook index

We considered an episodic memory of earlier incidents to reuse as precedents. We rejected it because stored diagnoses could leak held-out labels into later runs. They could also be reused as evidence without being verified and make results depend on run order. Versioned runbooks let us check guidance and report when it is stale.

---

## 10. AI Use Statement

Our team developed the project idea, scope, architecture and technical decisions. ChatGPT and Claude assisted with drafting, organizing content, checking alignment with the assignment requirements and refining the wording. All content was reviewed and finalized by the team.

