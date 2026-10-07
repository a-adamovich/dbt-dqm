# Scale measurements

`integration_tests/scale/postgres_scale.py` builds a disposable consumer project, initializes the
DQM schema through the package, bulk-loads synthetic processed history, and measures
reconciliation and app sync. Run it against a disposable database:

```bash
DBT_DQM_TEST_DSN='host=localhost port=5432 dbname=dbt_dqm_ci user=postgres password=...' \
  uv run python integration_tests/scale/postgres_scale.py --output scale-results
```

It writes `postgres.json` plus `EXPLAIN (ANALYZE, BUFFERS)` plans for the freeze and the
reconciliation change set. The reconcile model's execution time spans the whole transaction, so
it is also how long the occurrence table's EXCLUSIVE lock is held (app writes wait at most
`--lock-timeout` seconds and then report the warehouse as busy).

## PostgreSQL 16.11 (local, Apple silicon), 2026-10-07

Volumes: 200 tracked tests, 1,000,000 occurrences (100,000 Active), 50,000 executions, 5,000,000
observations; history spread over two years.

| Measurement | Before fixes | After fixes |
| --- | ---: | ---: |
| Steady reconcile (3 new executions), lock held | 3.6 s | 2.4 s |
| Mass pass (all 200 tests pass, 100,000 issues archived), lock held | did not finish in 10 min at 200k occurrences; 20.7 s with the planner fix alone | 12.0 s |
| App sync (122,037 cached issues: Active + 90 days archived) | 14.9 s, 869 MB Python peak | 11.4 s, 869 MB |

Fixes made from these measurements:

- **Planner:** Postgres has no statistics for the change set's CTEs and estimated them at one row,
  so it chose nested loops that grew with affected identities × history (40 million join
  comparisons for 2,000 archives in a 20,000-occurrence history). Every join there has equality
  keys, so the stage is built with `enable_nestloop` off for that one statement.
- **Replay reads:** the change set used to materialize the whole occurrence table, including
  payloads, on every run. It now reads Active rows plus the history of identities the run
  touches; occurrence numbering, recurrence context and structural closures are unchanged.

Remaining cost is proportional to the Active backlog, not to total history. App sync memory is
the next limit: about 7 KB of Python memory per cached issue. At this volume it stays under the
30 s / 1 GB budget that would trigger warehouse-side pagination, but not by much.

BigQuery bytes-processed measurements still need a credentialed run.
