# Incident Triage Agent: Bounded Investigation of Application Failures

**CSE 510 — Introduction to Agentic Engineering, Fall 2026 · Week 7 Project Proposal**

**Team:** Pranav Kundaikar; Shubhojyoti Datta; Pooja Nemade; Jay Pathare

## 1. Problem statement

On-call engineers must combine logs, metrics, and runbooks to explain application failures, and log summaries often confuse symptoms with causes: database timeouts may indicate connection-pool exhaustion or an unavailable dependency. We propose an agent that investigates a registered incident bundle and drafts an evidence-linked report containing observed symptoms, a supported likely cause or an explicit inconclusive result, and documented next checks. Users are developers and on-call engineers. Evaluation uses an offline simulation; we make no production-readiness claim.

**Scope:** one fixed application topology; four incident families (connection-pool exhaustion, disk exhaustion, dependency failure, including unavailability or invalid response format, application errors after a deployment); JSONL logs, CSV metrics, and 6–8 versioned Markdown runbooks. Incidents outside these four families must end in abstention.

## 2. Why an agent?

Each observation determines the next query. Database timeouts prompt a pool-utilization check; normal utilization instead prompts dependency-health checks. The agent retrieves applicable runbook guidance, decides whether the evidence supports a diagnosis, and recovers from tool errors. A single prompt cannot selectively gather evidence or branch on what it finds.

**Stack:** Python, LangGraph, Pydantic, SQLite, and one tool-capable LLM API pinned to an exact model version recorded in configuration (a smaller model for development, the pinned model for final evaluation). Memory is a persistent, versioned runbook retrieval index; SQLite stores investigation state and review decisions. The interface is a CLI.

## 3. PEAS

**Performance:** Incident-family accuracy, correct abstention when evidence is insufficient, citation validity, and boundary compliance. Latency and cost are logged and reported, with no fixed target. Targets appear in Section 5.

**Environment:** Registered synthetic incident bundles, versioned runbooks, and one restricted local output directory. Excluded: live servers, production credentials, arbitrary web access, shell execution, and infrastructure changes. All data sent to the LLM API is synthetic or sanitized.

**Actuators:** Log queries, metric queries, runbook retrieval, and approval-gated report export. Recommendations are informational only; the agent cannot execute remediation.

**Sensors:** Incident ID, user question and time window, timestamped logs and metrics, runbook versions, tool errors, and human review decisions.

## 4. Tool inventory and workflow

| Tool | Input | Output | Errors | Risk |
|---|---|---|---|---|
| `query_logs` | Incident ID, time range, service/filter | Bounded events or counts, evidence IDs, truncation flag | Unknown ID, malformed records, invalid range, timeout | Low (read-only) |
| `query_metrics` | Incident ID, metric, time range | Samples and statistics with evidence IDs | Missing metric, insufficient samples, timeout | Low (read-only) |
| `search_runbooks` | Query, service, version | Ranked passages with IDs and versions | Empty results, incompatible version, unavailable index | Low (read-only) |
| `export_report` | Validated report ID, human approval record | Final Markdown report and audit record | Missing approval, changed report, invalid citations or path | High (only write; approval-gated, path-restricted) |

**Workflow:** validate input → select tool → observe → query again, draft, or abstain → validate citations → human review → export or reject. Tool choice depends on observations. The reviewer checks the diagnosis, supporting evidence, and recommended next checks before approving the report as final.

**Budgets (enforced in code):** at most 6 model turns per investigation; a turn may request several tool calls, with at most 8 tool attempts in total. Each request has a timeout, and a failed call is retried at most once; failures and retries count against the budgets. Invalid citations allow one repair attempt.

**Termination statuses:** `diagnosed` (supported cause), `inconclusive` (insufficient, contradictory, or out-of-family evidence), `budget_exceeded` (partial draft marked inconclusive), `validation_failed` (citations still invalid after repair), and `halted` (boundary violation attempted). Drafts ending `diagnosed`, `inconclusive`, or `budget_exceeded` go to human review, which ends in `approved` or `rejected`. Only an approved report can be exported, and approval binds to the exact report version.

**Observability:** every run writes a structured JSONL trace to `logs/`: model turns, tool calls with arguments, results and errors, budget counters, termination status, review decision, latency, and token cost. These run traces are separate from the incident logs the agent reads.

## 5. Success criteria

These are targets on the held-out set (Section 10), not achieved results.

1. ≥80% top-1 incident-family accuracy on the 12 diagnosable cases.
2. ≥75% abstention recall on the 4 cases that must abstain; wrong abstentions on diagnosable cases are also reported.
3. 100% of exported citations resolve to real evidence IDs, checked in code.
4. Zero unauthorized exports or out-of-scope operations, all 4 failure/adversarial cases end in their expected outcome (Section 10), and 100% of runs stay within budget.

## 6. Risks and mitigations

**Unsupported diagnosis.** Correlation is not causation. Reports separate observed facts from hypotheses, surface conflicting evidence, and abstain when measurements cannot discriminate. Code checks that citation IDs exist; the human reviewer checks that they support the claim. Review is required before export.

**Prompt injection and privacy.** Log text is untrusted data, never instructions. Permissions are enforced in code, outside prompts. All inputs are synthetic, so no real data reaches the API, the run traces, or the repository. Tests cover injected instructions, path traversal, and command-execution requests.

**Stale guidance.** Runbooks are versioned, and the agent discloses a version mismatch rather than treating outdated guidance as current evidence.

## 7. Team roles

| Member | Primary responsibility |
|---|---|
| Pranav Kundaikar | Workflow graph, orchestration, integration |
| Shubhojyoti Datta | Log and metric tools, incident fixture generation |
| Pooja Nemade | Runbook retrieval, evidence validation, approval gate |
| Jay Pathare | Evaluation harness, labels, adversarial tests and results |

Each member reviews one other member's component and contributes to the README, architecture document, report, and demo, so everyone can defend any part in the oral.

## 8. CSE 510 complexity claim

- **Extra 1 — Compound task:** the next query (pool metrics, dependency health, or deploy events) depends on what the previous observation showed, and the run ends in a diagnosis or an abstention.
- **Extra 3 — Designed failure modes:** missing evidence, contradictory signals, and tool timeouts each have an explicit graph transition and held-out test cases, and the demo includes at least one designed failure.
- **Extra 5 — Published pattern:** observation-dependent tool selection follows ReAct (Yao et al., 2023); the architecture review and evaluation compare it against a fixed log → metrics → runbook pipeline.


## 9. Why this is not a CSE 410 log summarizer

The 410 version clusters and summarizes logs. It cannot tell competing explanations apart, because the same timeout pattern fits pool exhaustion and dependency failure. Our agent separates them with follow-up metric queries and runbook applicability checks, and it recognizes when the evidence cannot discriminate. A runbook match alone never justifies a diagnosis.

## 10. Evaluation sketch and delivery plan

**Data.** A small development set, plus 20 held-out synthetic incidents: 12 diagnosable (3 per family), 4 that must abstain (3 insufficient or contradictory, 1 out-of-family), and 4 tool-failure or adversarial. Each case has a gold family or abstain label, written by one member and spot-checked by another. Labels are never visible to the agent.

**Expected outcomes for the 4 failure/adversarial cases:**

- Transient tool timeout: retry once, recover, and diagnose normally.
- Persistent tool failure: end `inconclusive`, naming the failed tool.
- Injected instructions in log text: ignore them, take no unsafe action, and continue the investigation.
- Request to export without approval or outside the output directory: refuse and end `halted`.

**Comparison and metrics.** The adaptive graph is compared with a fixed log → metrics → runbook pipeline using the same model and data. We report accuracy and abstention recall with 95% Wilson intervals, boundary-violation counts, and average tool calls, plus a short failure taxonomy. With 20 cases the intervals will be wide, and we will say so.

**Designed-hard cases:**

1. Database timeouts with normal pool utilization (true cause: dependency failure).
2. Missing metrics, so the run must abstain.
3. An incident outside the four families.
4. A tool timeout mid-investigation.
5. Injected instructions inside log text.

**Milestones.**

- **Weeks 8–10:** architecture review comparing ReAct-style selection with a fixed pipeline.
- **Week 12:** CLI, three read tools, persistent retrieval, the observation-dependent branch running in the demo, ≥10 tests, and a working README.
- **Week 14:** approval and export, the full evaluation and comparison, architecture document, and draft report.
- **Week 15 demo:** a supported diagnosis, an inconclusive investigation, and a blocked boundary violation.

A UI is optional and only after the required deliverables. Implementation begins after proposal approval.

## References and AI disclosure

- Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models*, ICLR 2023. https://arxiv.org/abs/2210.03629. We adapt observation-dependent tool selection within a bounded graph and do not claim to reproduce the paper.
- LangGraph documentation: https://docs.langchain.com/oss/python/langgraph/overview

**AI disclosure:** ChatGPT reviewed the course documents and generated the initial draft. Claude checked that draft against the course handouts and produced this revision. The team verified the content and will record all later AI-assisted coding, debugging, evaluation, and writing in the final AI Use Statement.
