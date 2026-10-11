# Scale measurements

## Current figures

The figures to use today; everything below is the method and the dated measurements behind them.

- **Review app cache ceiling:** 50,000 issues and 128 MiB of serialized issue data (enforced).
  At that boundary the sync path's peak RSS varies widely between identical runs: **about
  435–585 MiB** on 2026-10-10, where the single 420.8 MiB figure from 2026-10-09 sits at the low
  end. A running Streamlit server adds its own baseline. See
  [Repeated memory measurements](#repeated-memory-measurements-across-revisions-2026-10-10).
  Many runs reach the top of that range whichever PyArrow memory pool is used; the cause is not
  identified ([allocator A/B](#allocator-ab-2026-10-10)).
- **Postgres reconciliation at 1M occurrences:** about 2.4 s steady and 19 s for a 100k mass pass;
  the occurrence lock is held for that time. See [Reconciliation](#reconciliation).
- **BigQuery reconciliation at 1M occurrences:** about 1.5 GB processed steady and 1.8 GB for a
  mass pass. See [BigQuery](#bigquery).
- Measurements of 100,000+ cached issues predate the enforced ceiling and are historical.
- **Warehouse browsing at 1M occurrences / 100k Active (preview, `--browse warehouse`):** app
  memory stays about 175 MiB whatever the backlog. On Postgres with migration 0007's index a
  page takes about 0.02 s unfiltered and up to 0.2 s with filters; the header summary 0.4–1.1 s
  (a tag filter 2.5–4.8 s). On BigQuery the app reads pages in chunks of 2,000 issues (80 pages
  of 25): a chunk costs about **0.96 GB** and 4–8 s, about 12 MB and 0.1 s per page amortized; a
  summary costs about 0.16 GB, a searched read about 1.4 GB. See
  [Warehouse browsing](#warehouse-browsing-2026-10-11).

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

## Warehouse browsing (2026-10-11)

[`browse_probe.py`](../integration_tests/scale/browse_probe.py) calls the app's own reads in a
fresh process: filter choices, the header summary (metrics and the filtered count) and pages of
25 for the whole Active population and eight typical filters, ten deep pages of 200, each page
stored in a page cache as the app does. Times are wall time per call through the app's functions
(median of 5 on Postgres, 3 on BigQuery); BigQuery bytes are from the first call of each read
(repeats hit the query cache, as they would in the app). Both harnesses accept `--browse`; the
synthetic history varies workflow status, priority, owner and tags so sorting and filters see
realistic data, and has no recurrences.

```bash
uv run python integration_tests/scale/postgres_scale.py --payload-columns 8 --browse --compare-index --skip-sync
uv run python integration_tests/scale/bigquery_scale.py --browse --skip-sync --label browse
```

Commit `ef2e406` (single-statement reads from #56 plus migration 0007), 1M occurrences / 100k
Active, 8-column payloads. dbt-core 1.12.5; PostgreSQL 16.11 on the same laptop; BigQuery
on-demand, US. Raw results and plans: [`scale-results/browse-2026-10-11/`](scale-results/browse-2026-10-11/).

**Postgres** (seconds, median through the app):

| Read | Matching | Page 1 with 0007 | Page 1 without | Page 2 with | Page 2 without | Summary |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All Active | 100,007 | 0.019 | 2.675 | 0.020 | 0.308 | 0.70 |
| Status BLOCKED | 12,500 | 0.137 | 0.154 | 0.105 | 0.058 | 0.43 |
| Priority high | 28,574 | 0.195 | 0.200 | 0.187 | 0.171 | 0.56 |
| One owner | 5,000 | 0.155 | 0.143 | 0.046 | 0.050 | 0.52 |
| Created in the last 30 days | 39,706 | 0.019 | 0.191 | 0.025 | 0.168 | 0.94 |
| One tag | 33,334 | 0.019 | 0.313 | 0.025 | 0.317 | 4.78 |
| Recurrences only | 0 | 0.513 | 4.896 | | | 1.08 |
| Search | 11 | 0.191 | 0.186 | | | 0.57 |

- **Migration 0007's index** (a partial expression index on the page sort keys, Active rows
  only) turns the unfiltered page from a sort of every Active issue (2.4 s in the plan, spilling
  to disk) into an index range scan (0.3 ms). Deep pages of 200 drop from 0.34 to 0.05 s.
  Building it took 1.5–5 s at 1M rows, under the reconcile lock; the mass-pass reconcile showed
  no measurable cost from maintaining it.
- **Filters the index can't order** (status, priority, owner, search) scan the Active rows:
  0.1–0.2 s here.
- **The summary** counts the whole Active population through the history join (for the
  Recurring metric), 0.4–1.1 s; a tag filter parses every Active issue's tag JSON, 2.5–4.8 s.
  The app reads it once per filter set and session.
- **Filter choices** (distinct statuses, owners and tags, read once per session) took 4 s.
- **Peak RSS of the probe process: 174 MiB**, against 435–585 MiB for a 50,000-issue snapshot
  sync. The page cache is bounded at 2,000 issues.

**BigQuery** (first call, MB billed; seconds first call / median of repeats):

| Read | MB per read | Seconds |
| --- | ---: | ---: |
| Page (any non-search filter) | 960–976 | 4.4–6.3 / 2.3–2.6 |
| Page, recurrences only (no rows) | 210 | 2.6 / 1.2 |
| Page with a search | 1,412 (dry-run estimate 653 for the searched part) | 6.0–6.5 / 2.9–3.3 |
| Summary | 161–176 | 2.5–3.5 / 1.2–1.3 |
| Summary with a search | 619 | 3.0–3.4 / 1.5–1.7 |
| Filter choices | 185 | 3.3 |
| Deep page of 200 | (as a page) | 4.9–6.6 |

- **Why a page costs about 1 GB:** phase 1 (key columns for the filtered population) reads about
  160 MB. Phase 2 reads the page's 25 full rows by `occurrence_id`, and BigQuery bills the
  referenced columns of every row, including the payload: about 800 MB at 1M occurrences,
  whether 25 or 2,000 rows come back. Clustering can't help in place (an existing table's
  clustering can't be changed with SQL, and the read would also need pruning on the history
  join), and search indexes are only populated above 10 GB.
- **Chunked reads (measured on `0fd69f7`):** because phase 2 costs the same for 25 or 2,000
  rows, the app now reads BigQuery pages in chunks of the page-cache size (2,000 issues, 80 pages
  of 25) and pages through each chunk locally. A chunk read of all Active issues cost 961.5 MB
  billed, the same as one page of 25, and took 8.0 s uncached (4.1 s repeated): about **12 MB and
  0.1 s per page** amortized. Paging back into an earlier chunk reuses it while its rows are still
  in the local cache. Peak RSS stayed at 174 MiB. Postgres keeps reading one page per statement.
- The search ceiling (`--max-search-gib`, 1 GiB) applies to the searched part of a read, which the
  dry run estimated at 653 MB here.
- Peak RSS of the probe process: 165 MiB.


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

*Historical (2026-10-07), before the enforced 50,000-issue ceiling; kept for the per-issue cost.*

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

*Superseded by the repeated measurements below:* two runs per version were too few. The original
conclusion ("PyArrow 23 changes peak RSS by about −8 MiB, within run-to-run variation; the rise above
420.8 MiB comes from other changes") does not hold. Raw results:
[`pyarrow-23-cache-boundary.json`](scale-results/pyarrow-23-cache-boundary.json).

### Repeated memory measurements across revisions (2026-10-10)

Same harness, workload and Postgres 16.11 server. Each revision ran in its own worktree with
`uv sync --locked --all-groups`; runs alternated between revisions on an otherwise idle machine. The
harness files are identical across these revisions.

| Revision | Environment | Runs | Peak RSS median | Range |
| --- | --- | ---: | ---: | --- |
| `52d474f` (420.8 MiB recorded here) | dbt-core 1.11.11, PyArrow 19.0.1, Streamlit 1.61.1 | 5 | 453 MiB | 424–521 |
| `29e14f6` | same | 5 | 486 MiB | 455–506 |
| `956dc97` (#25) | dbt-core 1.11.15, PyArrow 19.0.1, Streamlit 1.65.0 | 11 | 465 MiB | 436–542 |
| `0f08cff` (#32) | as #25, PyArrow 23.0.1 | 11 | **531 MiB** | 450–583 |
| `0f08cff`, `ARROW_DEFAULT_MEMORY_POOL=system` | as #32 | 6 | 459 MiB | 435–513 |

What this shows, and what it doesn't:
- **Run-to-run variation is large.** The same revision spans up to about 100 MiB. The single
  420.8 MiB figure sits at the low end of `52d474f`'s own range, so the earlier "+40 MiB" was mostly
  noise.
- **PyArrow 23 is the one interval with a pattern.** About half its runs land at 450–474 MiB, like
  PyArrow 19, and half at 531–583 MiB, a level only one PyArrow 19 run reached (Mann-Whitney
  U = 84/121, about p ≈ 0.12, so not statistically settled).
- *Superseded:* six runs with the system pool suggested the default allocator (mimalloc) caused the
  high cluster. The larger A/B below does not support that.
- The cache ceilings are unchanged: they bound serialized data, not RSS. Plan for about 435–585 MiB
  at the 50,000-issue boundary.

Raw results: [`memory-interval-2026-10-10.json`](scale-results/memory-interval-2026-10-10.json).

### PyArrow 25 (2026-10-11)

Same harness, pandas 3.0.6 with the app's object strings, 6 alternating rounds:

| PyArrow | Peak RSS range | Median |
| --- | --- | ---: |
| 23.0.1 | 577–585 MiB | 579 MiB |
| 25.0.1 | 547–583 MiB | 569 MiB |

Equal or slightly lower, so the pin moved to 25.x. PyArrow 26.0.0 was one day old at the time and
was not evaluated. Raw results: [`pyarrow-25-2026-10-11.json`](scale-results/pyarrow-25-2026-10-11.json).

### pandas 3 (2026-10-11)

Same harness at 50,000 issues, alternating runs, 6 per variant:

| Variant | Peak RSS median | Mean |
| --- | ---: | ---: |
| pandas 2.3.3 | 567 MiB | 556 MiB |
| pandas 3.0.6, default (Arrow-backed strings) | 602 MiB | 601 MiB |
| pandas 3.0.6, Arrow-backed strings (second run) | 636 MiB | 630 MiB |
| pandas 3.0.6, object strings | 569 MiB | 560 MiB |
| pandas 2.3.3 (confirmation) | 536 MiB | 530 MiB |
| pandas 3.0.6 with the app's `configure_pandas()` | 543 MiB | 533 MiB |

pandas 3 infers Arrow-backed strings by default, and that costs about 70 MiB at this boundary
(Arrow higher in 33 of 36 pairs). The review app therefore keeps object strings
(`dbt_dqm_app/frames.py`), which brings pandas 3 back in line with pandas 2 (20 of 36 pairs, no
difference). Raw results: [`pandas-3-2026-10-11.json`](scale-results/pandas-3-2026-10-11.json).

### Allocator A/B (2026-10-10)

PyArrow 23.0.1, same harness and Postgres 16.11, 12 rounds alternating which pool ran first:

| PyArrow memory pool | Runs | Peak RSS median | Mean | Runs at or above 520 MiB |
| --- | ---: | ---: | ---: | ---: |
| default (mimalloc) | 12 | 575 MiB | 536 MiB | 8 |
| system (`ARROW_DEFAULT_MEMORY_POOL=system`) | 12 | 563 MiB | 536 MiB | 8 |

No difference (Mann-Whitney z = 0.72). The high cluster is not caused by the allocator, so the app
keeps PyArrow's default pool. Both pools show the same two levels (about 450–470 and 560–580 MiB);
what selects between them is not identified. Raw results:
[`allocator-ab-2026-10-10.json`](scale-results/allocator-ab-2026-10-10.json).

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
| App sync, 100,007 issues (historical: above today's 50,000 ceiling) | 1.12 | 1.23 | 1.12 | 68–72 s (peak RSS 911 MB) |

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
