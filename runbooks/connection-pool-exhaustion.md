---
id: connection-pool-exhaustion
title: Connection pool exhaustion on checkout-api
version: 1.0
family: connection_pool_exhaustion
updated: 2026-11-01
---

## Symptoms
- edge-lb returns 503 for checkout-api routes; request_completed lines show status 503
  with durations close to the pool acquire timeout (about 5 s).
- latency_p95_ms jumps to roughly the acquire timeout; error_rate_pct rises sharply.
- Often follows a rise in request_rate_rps (traffic surge, retry storm, batch job).

## How to confirm
- db_pool_active sits at db_pool_max (20) for several consecutive minutes.
- db_pool_wait_ms climbs from near zero to seconds, tracking the acquire timeout.
- db_pool_timeout events: timed out waiting for a connection from pool, with
  active equal to max, idle=0 and a growing waiting count.
- db_pool_utilization_high warnings appear in the minutes before the first timeout.
- The database itself stays healthy: db_query_latency_p95_ms and db_cpu_pct near baseline.
- Compare request_rate_rps before and after onset to find the demand change.

## Distinguish from
- Dependency failure: there the database is slow (high query latency, slow_query and
  db_query_timeout events) while db_pool_active stays below db_pool_max and
  db_pool_wait_ms stays low. Pool saturation with fast queries means exhaustion;
  slow queries with a normal pool means the dependency is at fault.
- Deployment regression: check for deploy_completed shortly before onset. A pool leak
  introduced by a release shows db_pool_active rising without a traffic increase.

## Mitigation
- Shed or rate-limit non-critical traffic at edge-lb; pause batch or retry sources.
- Scale out checkout-api only if orders-db has spare capacity (watch db_cpu_pct and
  db_active_connections); otherwise the bottleneck just moves to the database.
- Raising pool max is a last resort and must stay within the database connection limit.
- Fix client retry behaviour (backoff, jitter) if retries amplified the surge.

## Escalation
- Page the checkout-api owner if 503s persist more than 10 minutes after shedding load.
- Involve the orders-db owner before any pool size increase.
