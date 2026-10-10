# Scale measurements

Two harnesses build a disposable consumer project, initialize the DQM schema through the
package, bulk-load synthetic *processed* history (occurrences, executions, observations and
receipts, all older than any real run), and then measure real dbt runs against it. Raw results
are in [`scale-results/`](scale-results/).

```bash
# PostgreSQL: a disposable database
DBT_DQM_TEST_DSN='host=... dbname=... user=... password=...' \
  uv run python integration_tests/scale/postgres_scale.py --payload-columns 8 --compare-planner

# BigQuery: creates and deletes its own dataset (billable, small)
GOOGLE_APPLICATION_CREDENTIALS=... DBT_DQM_BIGQUERY_TEST_PROJECT=... \
  uv run python integration_tests/scale/bigquery_scale.py --label run-name
```

**Default volumes:** 200 tracked tests, 1,000,000 occurrences (100,000 Active), 50,000
executions, 5,000,000 observations, history spread over two years. Payloads are the grain
plus 8 context fields, about 408 bytes per row: roughly an `allowlist` capture of names,
emails, amounts and reasons.

**What each scenario does:**

- **Steady:** the three demo tests ran since the last reconciliation; reconcile once.
- **Mass pass:** all 200 scale tests pass at once, archiving every one of their 100,000 Active issues.
- **App sync:** a fresh process runs the review app's sync path
  ([`app_memory.py`](../integration_tests/scale/app_memory.py)):
  1. the verified issue and Health reads
  2. replacing the SQLite snapshot
  3. reading the cache back
  4. building the DataFrame with each card's record display

  Peak RSS covers the whole process: Python objects, pandas and numpy buffers, and the database
  client. The real app splits this work between a worker process (fetch) and the Streamlit
  process (cache and DataFrame), so the probe is an upper bound for either one. A running
  Streamlit server adds its own framework baseline on top.

**How cost is measured:**

- **Postgres:** the dbt-reported execution time of `dqm_reconcile`. Its transaction spans every
  hook, so this is also how long the occurrence table's EXCLUSIVE lock is held.
- **BigQuery:** `INFORMATION_SCHEMA.JOBS_BY_USER` for the jobs created during the step, counting
  non-script jobs only (a script parent repeats its children's totals).

Environment, 2026-10-07: dbt-core 1.11.11, dbt-postgres 1.11, dbt-bigquery 1.11.3. PostgreSQL
14.20 and 16.11 built from source on an Apple-silicon laptop. BigQuery on-demand, US multi-region.

## PostgreSQL

### Reconciliation

All with 1M occurrences and ~408-byte payloads; the lock is held for the full duration.

| Change | Steady | Mass pass (100k archived) |
| --- | ---: | ---: |
| Original replay, nested loops allowed | (grain-only payloads) 3.6 s | did not finish in 10 min at 200k occurrences |
| + statement-scoped `enable_nestloop = off` | — | 20.7 s (grain-only) |
| + replay reads Active rows plus affected identities' history only | 5.05 s | 17.4 s |
| + Active set limited to tests with frozen inputs, plus untracked tests | **2.36 s** | **18.8 s** |

### Why nested loops are disabled for the change set, and what it costs

The change set is one statement built from many CTEs. Postgres has no statistics for CTE
results and estimates them at one row, so it picks nested-loop joins. Those scale with
affected identities × history whenever a run touches many identities. Every join in the
change set has equality keys, so hash joins are always possible. `reconcile_pre` therefore
builds the stage with `set local enable_nestloop = off` and resets it right after, so nothing
else in the transaction is affected.

This was validated across workloads rather than one benchmark: `EXPLAIN ANALYZE` of the change
set after all the fixes above, Postgres 14.20, ~408-byte payloads.

| Occurrences (tests) | Steady, off | Steady, on | Mass pass, off | Mass pass, on |
| ---: | ---: | ---: | ---: | ---: |
| 20,000 (20) | 0.02 s | 0.003 s | 0.06 s | 1.7 s |
| 200,000 (200) | 0.70 s | 0.01 s | 1.5 s | **timeout > 300 s** |
| 1,000,000 (200) | 8.3 s | 2.1 s | 8.7 s | **timeout > 300 s** |

Nested loops are faster when a run touches few identities. They fail without bound once a run
touches many: 2,000 archived issues already cost 28× more, and 20,000 or more never finished.
The setting trades a bounded steady-state cost, a 2.4 s real reconcile at 1M occurrences (the
`EXPLAIN ANALYZE` times include heavy instrumentation overhead), for protection against an
unbounded mass-pass failure.

Choosing the setting per run from the number of affected Active rows could recover the
steady-state difference. That is a possible refinement, not implemented.

### App sync memory

PostgreSQL 16.11, ~408-byte payloads, the default 90-day archive cache window.

| Cached issues (Active + 90-day archive) | Peak RSS | Sync wall time |
| ---: | ---: | ---: |
| 30,514 | 522 MB | 2.8 s |
| 61,022 | 775 MB | 5.9 s |
| 122,034 | 1,149 MB | 19.9 s |

That is about **6.9 KB per cached issue** on top of about 310 MB fixed: 150 MB for the
interpreter and libraries, the rest from the SQLite, DataFrame and client overhead. Grain-only
payloads used about 7 KB per issue under `tracemalloc`, which counts Python allocations only, so
the earlier "870 MB at 122k" figure understated the process.

**Supported size:** keep the review app's cache, meaning Active issues plus the archive window,
**at or below about 50,000 issues with payloads up to ~400 bytes**. That's a peak of about 650 MB
for the sync path, plus Streamlit's own baseline. Beyond that:

- shrink `--archive-cache-days` (0 caches Active issues only), or
- capture fewer context columns.

Warehouse-side filtering and pagination, which the roadmap defers, is the fix for larger
backlogs. Memory remains an open concern rather than a solved one.

The app now enforces 50,000 issues and 128 MiB of serialized issue data, including retained
issues with pending edits. `--max-cache-issues` and `--max-cache-mib` can lower these limits,
but cannot raise them. Sync checks the warehouse count before download, streams rows in pages
of 500, and refuses an oversized result without replacing issues, Health, or the sync timestamp.
Pending edits remain available in 100-issue local pages even for an oversized historical cache.
The byte limit measures compact UTF-8 JSON, not resident memory: Python, pandas, client buffers
and Streamlit add overhead, and one fetched row may temporarily exceed the budget before rejection.
Pending replacement text is charged conservatively in addition to the cached JSON before overlay.

### Safeguard boundary rerun (2026-10-09)

The reproducible `integration_tests/scale/cache_boundary.py` harness measured the new real sync
path on PostgreSQL 16.11 at exactly 50,000 Active issues, with synthetic processed history and
408-byte captured payloads. It includes verified warehouse reads, atomic SQLite issue/Health
replacement and the issue DataFrame. A fresh process measured **148.8 MiB baseline / 420.8 MiB
peak RSS**, **2.17 s warehouse read / 5.90 s total**. Other acceptance suites were running on
the same machine, so timings are indicative rather than a regression threshold. A running
Streamlit server adds framework overhead; this is not an end-to-end server-memory guarantee.
Raw results: [`cache-boundary-safeguards.json`](scale-results/cache-boundary-safeguards.json).

### PyArrow 23 rerun (2026-10-10)

The same harness at 50,000 issues compared PyArrow 19.0.1 and 23.0.1, with identical code and lock otherwise
(dbt-core 1.11.15, pandas 2.3.3, Streamlit 1.65.0). Runs alternated on an otherwise idle machine:

| PyArrow | Peak RSS (2 runs) | Mean peak | Mean baseline |
| --- | --- | --- | --- |
| 19.0.1 | 457.6 / 461.8 MiB | 459.7 MiB | 160.5 MiB |
| 23.0.1 | 445.2 / 458.4 MiB | 451.8 MiB | 163.4 MiB |

PyArrow 23 changes peak RSS by about −8 MiB, which is within run-to-run variation, and the baseline by
about +3 MiB. Both are about 40 MiB above the 420.8 MiB recorded on 2026-10-09; the 19.0.1 control
shows the same rise, so it comes from other changes since then (including the dependency updates), not
from PyArrow. Raw results: [`pyarrow-23-cache-boundary.json`](scale-results/pyarrow-23-cache-boundary.json).

```bash
DBT_DQM_TEST_DSN='host=... port=... dbname=... user=... password=...' \
uv run python integration_tests/scale/cache_boundary.py --output /tmp/cache-boundary.json
```

## BigQuery

Same volumes and payloads. Cost is in bytes processed (on-demand billing also applies a 10 MB
minimum per query) plus slot time. Wall time is dominated by the number of script statements and
varies between runs by up to about 2× (compare `steady_first` across runs), so the bytes are the
stable signal.

| Step | Baseline GB | + observation floor GB | + Active scope GB | Wall time (last run) |
| --- | ---: | ---: | ---: | ---: |
| Steady reconcile, 100k occurrences | 0.41 | 0.16 | 0.16 | 36–71 s |
| Steady reconcile, 1M occurrences | 4.09 | 1.52 | **1.47** | 41–52 s |
| Mass pass, 1M occurrences | 4.39 | 1.79 | **1.77** | 55–64 s |
| App sync, 100,007 issues | 1.12 | 1.23 | 1.12 | 68–72 s (peak RSS 911 MB) |

**Observation floor.** The baseline's stage build read the whole `dqm_issue_observations`
table, 3.3 of its 4.1 GB, because nothing filtered its partition column.
- **The fix:** a run now computes `min(captured_at) - 1 day` over its frozen inputs into a
  script variable. A variable counts as a constant for partition pruning.
- **Why it never excludes evidence:** capture writes a test's observations after that test's
  execution row, in the same on-run-end script, so `observed_at >= captured_at`. The day of
  margin covers clock differences between statements. Late evidence and older frozen inputs
  lower the floor; they're never cut off.

**Clustering was measured and not added.** After the floor, what's left is three full scans
of `dqm_issue_occurrences`, about 0.73 GB each at 1M rows:
- the change set's Active and affected-history reads
- the MERGE target
- the app's `dqm_all_issues` read

None of them can be pruned by clustering:
- **MERGE:** matches on `occurrence_id`.
- **History read:** a join.
- **App read:** filters on Active *or* a recent archive date.

Clustering benefit depends on the actual filters and execution plan. Existing tables can have
their clustering metadata changed in place with `bq update --clustering_fields`; a table swap
is not required. Existing rows may need a separate reclustering operation. The measurements
above do not establish that clustering can never help these queries. Further controlled
experiments remain deferred; this release keeps the measured query improvements and no new
occurrence clustering. See [Google's clustered-table guide](https://docs.cloud.google.com/bigquery/docs/manage-clustered-tables).

**App sync** reads about 1.1 GB and takes about 65 s for 100k issues. The read itself dominates,
because the client downloads rows over the REST API. The same supported cache size as on
Postgres applies.

Plans and per-run JSON are in [`scale-results/`](scale-results/). The planner-comparison plan
files are from the last run that completed each case. The nested-loop mass-pass plan is from the
20,000-occurrence run, because the larger ones timed out.
