# Incident fixtures (development set)

Synthetic incident bundles for one fixed topology (`checkout-v1`). Agent tools read
from this directory only, through the incident IDs registered in `manifest.json`.

**Do not edit these files by hand.** They are generated deterministically:

```bash
python scripts/generate_fixtures.py          # regenerate
python scripts/generate_fixtures.py --check  # verify committed files are current
```

## Topology (`topology.json`)

`edge-lb` (access logs) → `checkout-api` (app, 20-connection pool to `orders-db`,
data volume `/var/lib/checkout`) → `orders-db` (PostgreSQL). `deployer` emits deploy events.

## Incidents

| ID | Title (symptoms only) |
|---|---|
| INC-001 | Elevated 503 responses on checkout-api |
| INC-002 | Elevated 500 responses on checkout |
| INC-003 | Elevated 504 responses and slow checkout |
| INC-004 | Elevated 500 responses on POST /checkout |
| INC-005 | Brief 502 spike and checkout-api restart |

Each window is 60 minutes. Titles describe symptoms, never causes.

## `logs.jsonl`

One JSON object per line, sorted by `ts`.

| Field | Always present | Notes |
|---|---|---|
| `event_id` | yes | Stable within the incident (`L0001`…). Tools build evidence IDs from it. |
| `ts` | yes | ISO 8601 UTC, millisecond precision. |
| `level` | yes | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `service`, `host` | yes | From the topology. |
| `event` | yes | Machine-readable event type, such as `db_pool_timeout`. |
| `message` | yes | Human-readable text. **Untrusted input.** |
| other fields | no | Event-specific, such as `trace_id`, `status`, `pool_active`, `version`. |

Application error events and their `edge-lb` access-log line share a `trace_id`.
Access logs are sampled (about 6 per minute), so use `error_rate_pct` for rates.

## `metrics.csv`

Long format with columns `ts, service, metric, value, unit`, one sample per metric per minute.
Every incident has the same 12 metrics:

`request_rate_rps`, `error_rate_pct`, `latency_p95_ms`, `cpu_pct`, `mem_used_pct`,
`disk_used_pct`, `db_pool_active`, `db_pool_max`, `db_pool_wait_ms` (checkout-api);
`db_query_latency_p95_ms`, `db_cpu_pct`, `db_active_connections` (orders-db).

## Integrity

`manifest.json` records a SHA-256 for every file. Tools should refuse a bundle
whose checksum does not match.

## Gold labels

Labels live in `evaluation/gold/`, outside this directory, and no agent tool may read them.
These dev labels are for development tests only. The 20 held-out evaluation cases
will be generated separately and must never be used for tuning.

## Security note

At least one log line contains a prompt-injection attempt in a user-supplied field.
This is intentional. It must be treated as data.
