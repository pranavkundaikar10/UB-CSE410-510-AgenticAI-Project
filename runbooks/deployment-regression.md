---
id: deployment-regression
title: Application errors after a checkout-api deployment
version: 1.0
family: deployment_regression
updated: 2026-11-01
---

## Symptoms
- error_rate_pct rises within minutes of a rollout; usually 500 on specific routes.
- Responses fail fast: latency_p95_ms and resource metrics stay near baseline.

## How to confirm
- deployer emits deploy_started and deploy_completed for checkout-api; note
  from_version, to_version and commit.
- unhandled_exception errors in checkout-api begin right after deploy_completed and
  carry the new version field; the exception type (KeyError, TypeError, ...) and code
  location repeat consistently.
- error_rate_pct steps up within a few minutes of deploy_completed and stays up;
  before deploy_started it is at baseline.
- Match each error to its edge-lb request_completed line via trace_id to see which
  routes fail.
- cpu_pct, mem_used_pct, disk_used_pct, db_pool_active and db_query_latency_p95_ms
  stay normal, which rules out resource causes.

## Distinguish from
- Disk exhaustion: also fast 500s, but with write_failed / ENOSPC and a rising
  disk_used_pct. A deployment regression has no resource trend.
- Coincidental timing: a deploy shortly before an incident is not proof. Require
  errors from the new version only, and none from the old version before the deploy.
- Pool exhaustion or dependency failure: those show slow 503/504 and abnormal
  db_pool_* or db_query_* metrics.

## Mitigation
- Roll back checkout-api to the previous version; confirm errors stop on that version.
- If rollback is blocked, disable the feature flag or route covering the failing path.
- Capture the exception and request shape for the fix; add a regression test.

## Escalation
- Notify the release owner and the author of the commit.
- Freeze further deploys of checkout-api until the fix is verified.
