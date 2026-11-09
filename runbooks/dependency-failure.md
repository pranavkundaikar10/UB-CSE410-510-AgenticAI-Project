---
id: dependency-failure
title: Dependency failure in orders-db
version: 1.0
family: dependency_failure
updated: 2026-11-01
---

## Symptoms
- 504 responses and slow checkout; request_completed lines show durations near the
  statement timeout (about 3 s).
- latency_p95_ms rises for every route that queries orders-db.

## How to confirm
- db_query_latency_p95_ms for orders-db rises by orders of magnitude over baseline.
- db_cpu_pct on orders-db near saturation; io_wait_high warnings on the database host.
- slow_query warnings from orders-db and db_query_timeout errors in checkout-api
  (canceling statement due to statement timeout).
- Look for a database-side trigger just before onset: autovacuum_started on a large
  table, a long-running migration, a backup, or a lock-heavy batch.
- The connection pool is NOT saturated: db_pool_active stays below db_pool_max and
  db_pool_wait_ms stays near zero.

## Distinguish from
- Connection pool exhaustion: there queries are fast (db_query_latency_p95_ms normal)
  but the pool saturates: db_pool_active is pinned at db_pool_max with high
  db_pool_wait_ms and db_pool_timeout events. Pool saturation with fast queries means
  exhaustion; a slow database with a normal pool means dependency failure.
- Deployment regression: a new release can add a slow query; check deploy_completed
  timing and whether only one query shape is slow.

## Mitigation
- Remove the database load source: pause or reschedule the vacuum, migration or batch.
- Reduce query pressure: shed non-critical reads, serve cached responses where safe.
- Do not raise the checkout-api pool size; more connections add load to a slow database.
- If a single query regressed, check its plan and indexes before restoring traffic.

## Escalation
- Page the orders-db owner (DBA on call) immediately when db_cpu_pct stays saturated.
- Escalate to the incident commander if latency does not recover within 15 minutes.
