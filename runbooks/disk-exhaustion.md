---
id: disk-exhaustion
title: Disk exhaustion on the checkout-api data volume
version: 1.0
family: disk_exhaustion
updated: 2026-11-01
---

## Symptoms
- 500 responses on routes that write files (receipts, uploads, temporary exports);
  read-only routes keep working.
- error_rate_pct rises while latency_p95_ms, cpu_pct and pool metrics stay normal.

## How to confirm
- disk_used_pct for checkout-api climbs steadily and reaches or approaches 100%.
- disk_usage_high warnings for the data volume (/var/lib/checkout) crossing thresholds.
- write_failed events with "No space left on device" (OSError errno 28, ENOSPC)
  on the same mount.
- Find what is filling the volume: look for a config_changed event (for example a log
  level raised to DEBUG) or high-volume events such as payload_dump writing files there.
- Estimate the fill rate from the first and last disk_used_pct values to predict time to full.

## Distinguish from
- Connection pool exhaustion and dependency failure: those return 503/504 with slow
  responses and abnormal db_pool_* or db_query_* metrics. Disk exhaustion fails fast
  with 500 and leaves pool and database metrics at baseline.
- Deployment regression: also fast 500s, but with an exception tied to a new version and
  no ENOSPC. Check the error text and disk_used_pct, not only the status code.

## Mitigation
- Stop the writer first: revert the verbose logging or disable the debug dump.
- Free space safely: delete or move debug artifacts and rotated logs; never delete
  application data or open database files.
- Confirm write_failed stops and disk_used_pct drops before closing the incident.
- Add an alert well below 100% and a size cap or rotation for debug output.

## Escalation
- Page the checkout-api owner if space cannot be freed within 15 minutes.
- Involve the platform team if the volume must be resized.
