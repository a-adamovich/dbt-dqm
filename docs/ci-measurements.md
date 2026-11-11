# CI tiers and measurements

The development policy has three stages. Stage 1 provides fast required PR checks and manual
Postgres/platform validation. Stage 2 will add approved BigQuery smoke and full cloud checks;
Stage 3 will add evidence reuse and simplify publication. Stages 2 and 3 are not implemented
by the Stage 1 change.

## Automatic PR checks

Every PR, including drafts, and every push to `main` runs the fast tier. Superseded PR runs are
cancelled; `main` runs are not. There are twelve jobs including the final `required` check:

| Check | Matrix |
| --- | --- |
| Units, lint, consistency, app startup | Python 3.11–3.14 |
| Wheel variants, metadata, security floors | Python 3.11 and 3.14 |
| Portability and wheel variants | Windows/Python 3.11; macOS/Python 3.14 |
| Real dbt 2.0.11 and 2.0.9 parse/compile | Python 3.14, no warehouse credentials |
| BigQuery compile | Core 1.12.6, Python 3.14, no warehouse access |
| Postgres core | Core 1.12.6, Postgres 16, Python 3.14, four workers |

The core tier has exactly eight cases: lifecycle/history, collection reporting, native limits
and disabled commands, changed-evidence rejection, late/skipped evidence, rollback/empty mode,
reviewer-edit preservation, and generic/singular lifecycle. The generic fixture is disabled
unless the acceptance case opts in. The existing demo walkthrough is not repeated in this tier.

## Manual full validation

Run `Full validation` from GitHub Actions, selecting the candidate branch. All jobs check out
the dispatch event's immutable SHA, including reusable candidate QA. No schedule or automatic
push trigger starts the long matrix.

The workflow retains the complete Python 3.11–3.14/Postgres 14–16 acceptance matrix, the
Core 1.11.0 job, complete Windows/macOS and installed-wheel matrices, all BigQuery offline
compile combinations, walkthroughs, query-plan checks, browser QA, and memory/export QA.
Postgres uses four workers. The full suite contains 42 cases including generic lifecycle and
populated identity-migration coverage.

The `validated` check requires all 27 matrix/QA jobs and nine warehouse evidence records from
the same run attempt and source tree. Each warehouse record includes the SHA, tree, matrix,
installed versions, dependency digest, timestamps, and JUnit counts. Failures, missing jobs,
unexpected skips, duplicate reports, or fewer than 42 passing warehouse cases fail validation.
Reports are retained for 30 days; this is retention, not permission to reuse them for release.

Stage 1 certifies **Postgres and platforms only**. A passing result does not replace the
BigQuery release gate. The maintainer must configure `required` as a required status and require up-to-date PRs;
adding a workflow alone does not enforce branch protection.

## Measurement record

Targets are measurements to verify, not promises: Postgres core within ten minutes and fast
PR wall time within fifteen minutes. Keep all assertions if setup or execution needs tuning.
Each warehouse run writes JUnit and `--durations=25` output, installed versions, and JSON
evidence. Record suite, engine, worker count, elapsed time, result, and source revision below.

| Suite | Engine / warehouse | Workers | Duration | Result / revision |
| --- | --- | --- | --- | --- |
| New generic/singular lifecycle, local | Core 1.12.5 / Postgres 16 | 1 | 54.81 s | 1 passed; Stage 1 worktree |
| Required Postgres core, local Python 3.11 | Core 1.12.5 / Postgres 16 | 4 | 184.53 s | Exactly 8 passed, no skips; Stage 1 worktree |
| Real dbt 2 offline smoke, local Python 3.11 | dbt 2.0.9 / no warehouse access | 1 | 6.82 s | Exactly 11 passed, no skips; Stage 1 worktree |

The current local receipt helper measures billed bytes and slot milliseconds after each approved
suite, excluding parent `SCRIPT` rows. It separates labelled queries (including children) from
unlabelled queries referencing the test datasets; shared-QA activity may include other users.
Missing metadata permissions are reported as unavailable, never as zero usage. No GCP roles or
quotas are changed. Do not infer cost from fixture size or elapsed time.

Acceptance fixture clients and dbt profiles request a per-query limit of 1 GiB by default,
configurable with `DBT_DQM_BIGQUERY_MAX_BYTES_BILLED`. Real 2.0.9 and 2.0.11 offline checks
accept the profile/label configuration. dbt 2.0.11 did not enforce this profile limit on previously tested paths. Treat it as configured, not guaranteed; record fresh observations during live validation.
App search retains its own configured limit; other app queries and the restricted-permissions
fixture are not covered by this test cap. This is not a total-suite spending ceiling.

## BigQuery development measurements

`scripts/bigquery_measure.py` runs a BigQuery selection once and writes `measurement.json` to a
private directory outside the repository. Its output is marked as a development measurement and
can never count as release evidence. Each case reports:

| Field | Meaning |
| --- | --- |
| `case_seconds` | Whole case from JUnit: setup, call and teardown |
| `fixture_setup_seconds` | Pytest setup stage: `fixture_dataset_seconds`, `fixture_project_seconds` and `fixture_seed_seconds` |
| `installation_seconds` | The case's first `initialize_dqm` plus its first deployment of the package models |
| `installation_steps` | The steps counted as installation, in order |
| `installation_includes_reconcile` | The first deployment also reconciled captured evidence, so its time is an upper bound on installing |
| `initialize_repeat_seconds` | Later, idempotent `initialize_dqm` calls. Not installation |
| `scenario_seconds` | `case_seconds` minus fixture setup, installation and teardown |
| `runner_phase_seconds`, `runner_sql_calls`, `runner_query_jobs` | Per phase from each `dbt-dqm run`'s `dqm_workflow_metrics.json`, read after that invocation |
| `runner_metrics_missing` | Invocations that wrote no fresh metrics file. Their installation time is not attributed |

Columns overlap, so do not add them together. The seed is in both fixture setup and the dbt
subprocess totals, installation is inside the dbt and runner totals, and cases that run concurrent
work can sum above their wall time. `runner_sql_calls` and `runner_query_jobs` are null when the
engine reported none, which means unknown rather than zero. Fixture setup and installation are the
most a merge can save per absorbed case. They do not show that two scenarios can share state.

### What the smoke pilot can justify

The pilot measures only the cases it selects, and it is one run on one engine. The 0.2.5 receipts
give per-case totals without phases, so they cannot fill the gap. A consolidation needs measured
fixture and installation time for **every** case it merges, and the scenarios must be able to share
state.

| Proposed consolidation | Cases in smoke | Outcome |
| --- | --- | --- |
| Apply fencing | Both | Merged: about 109 s saved |
| Collation | Both | Stays separate: about 10 s to save, and the scenarios cannot share state |
| Batch rollback and `--empty` | Retained case only; `empty_changes_no_tracking_table` is not | Not justified by smoke |
| Capture integrity, recurrence payloads, maintenance faults, maintenance cleanup and health, stages and retention, annotation staging, upgrades, install and migration leases, app audit and browsing | None | Not justified by smoke |

The rows not justified by smoke need a measurement of the `full` selection, or stay separate. The
cost of a case outside the smoke selection must not be inferred from a smoke case.

Collation cannot share state in either order. The refusal scenario must create its collated
`dqm_install` in a dataset with no tracking tables, and the lifecycle scenario asserts exact counts
from a clean installation. In the refusal case, initialization and the full refresh are the behavior
under test, so only its fixture setup could be saved.

### Assertion map

| Retained case | Absorbed case | Where its assertions are |
| --- | --- | --- |
| `test_bigquery_apply_fencing_overlap_stale_generation_and_abandoned_runner` | `test_bigquery_overlapping_applies` | Unchanged, as the first half. It needs the clean installation |
| same | `test_bigquery_stale_generation_annotations_and_abandoned_runner` | Unchanged, as the second half, after the overlap leaves the same seven occurrences |

In a merged case, the first failure hides any later ones.

### Smoke pilot, 2026-11-10

Candidate `7b72db75`, dbt 2.0.11, six workers, ten cases before the merge above: 10 passed in
1,038.6 s (17m19s). Case time summed to 5,364 s, an effective parallelism of 5.2, and the log has no
rate-limit, quota or concurrent-update messages. It compares with 6,133 s for the same ten cases in
the four-worker 0.2.5 receipt. That is one run on each side, and 15 cases now skip post-capture
maintenance, so the difference is not attributed to any single change.

| Share of case time | Seconds | % |
| --- | --- | --- |
| Fixture setup (dataset, project, seed) | 104 | 2 |
| First installation (upper bound, includes reconciliation in 9 of 10 cases) | 1,465 | 27 |
| Teardown | 8 | under 1 |
| Scenario, including 372 s of repeated `initialize_dqm` | 3,787 | 71 |

Runner query jobs were null on dbt 2.0.11, and SQL calls were reported. Spend was not measured:
the metadata query failed with `BadRequest`. A later run recorded `invalidQuery`, and a dry run
traced it to a correlated subquery reading another table. `query_metrics.py` no longer uses that
form and also records the view and BigQuery's structured error code.

## BigQuery spend, 0.2.5 and the reduced selection, 2026-11-10

Read after the runs from `INFORMATION_SCHEMA.JOBS_BY_USER` with the corrected `job_metrics` query.
Both sides use the same identity scope and attribution rules, and each run's own validation ID and
time window. The project-wide view is denied, so jobs run by any other identity are not included.
The GCP billing report is the only complete figure.

Only stages that passed are compared. 0.2.5 ran all 34 cases on both engines. The reduced selection
runs 33 cases on dbt 2.0.11 and 9 on Core 1.12.6, so most of the saving comes from running fewer
Core cases and does not mean Core is cheaper per case.

| Stage | 0.2.5 cases, jobs, billed, slot hours | Reduced cases, jobs, billed, slot hours |
|---|---|---|
| Full on dbt 2.0.11 | 34, 12,749, 82.3 GiB, 7.2 | 33, 11,977, 79.2 GiB, 9.1 |
| Core 1.12.6 | 34, 12,320, 79.0 GiB, 6.9 | 9, 3,912, 26.5 GiB, 2.9 |
| Cross-engine upgrade | 1, 800, 5.8 GiB, 0.7 | 1, 800, 5.8 GiB, 0.6 |
| Total | 25,869, 167.1 GiB, 14.8 | 16,689, 111.5 GiB, 12.6 |

Billed bytes fell 33%, almost entirely on Core. The dbt 2 stage billed 3.1 GiB less, which is an
observed difference between two runs and is not attributed to any single change: they also differ
in the merged apply-fencing case, the cases that skip `maintain_dqm`, and worker count. Excluded
from the comparison: one failed Core smoke
attempt on the reduced selection (3,749 jobs, 25.5 GiB) that stopped on a BigQuery 500 error, the
smoke pilot, the 0.2.5 restricted-permissions run and earlier 0.2.5 attempts. Each labelled total
is an exact multiple of 10 MiB, BigQuery's minimum charge per query, so the number of queries
appears to drive billed bytes. Totals count reported statistics only, and up to a few dozen jobs
per run reported no billed bytes.

## Outcome of #103, 2026-11-11

[#103](https://github.com/a-adamovich/dbt-dqm/issues/103) asked for the same 33 + 33 + 1 selections
to run faster. The maintainer instead accepted a coverage tradeoff in #110, which replaced that
equal-count target:

- Core now runs only the nine `BIGQUERY_SMOKE_TESTS` cases.
- dbt 2 runs the full suite, now 33 cases, down from 34, because the two apply-fencing cases were
  merged.

**Like-for-like local timing.** Both sides are local runs, one run each:

| Stage | 0.2.5 receipts | Reduced selection (#110) |
| --- | --- | --- |
| dbt 2.0.11 | 34 cases, 4 workers, 77m52s | 33 cases, 6 workers, 61m11s |
| Core 1.12.6 | 34 cases, 2 workers, 2h15m | 9 cases, 4 workers, 23m18s |
| Cross-engine upgrade | 1 case, serial, 20m11s | 1 case, serial, 19m11s |

The Core reduction comes mostly from running fewer cases. The dbt 2 difference is not attributed to
any single change. The reduced-selection run also differs in worker count, the merged case and the
cases that skip `maintain_dqm`.

**Billed bytes.** The same stages fell from 167.1 GiB to 111.5 GiB, which is 33% less. This counts
the measured identity only (see the spend section above).

**GitHub proof run, not comparable.** The
[keyless proof run](bigquery-lifecycle.md#proof-runs-2026-11-11) on `0ada4e1` took 1h39m in total.
It does not compare with the table above for three reasons:

- it used GitHub runners;
- it ran both engines at the same time; and
- it included the restricted-permissions job.

**Accepted risks of the tradeoff.**

- A Core-only BigQuery adapter bug outside the smoke selection can slip through.
- A failure in the merged apply-fencing case hides its later scenarios.

## BigQuery tiers

Full acceptance runs on dbt 2 and the exact `BIGQUERY_SMOKE_TESTS` selection runs on Core, with six workers on dbt 2 and four on Core, matching the validated runs.
Before
merging a change to macros, models, BigQuery adapter or SQL behavior, `warehouse.py`, browsing or
the CLI runner, run the smoke selection on dbt 2 once, with maintainer authorization. The 1 GiB
qualifications in the measurement record above apply to every tier.

## 0.2.4 development measurements

| Check | Environment | Result | Duration |
| --- | --- | --- | --- |
| Postgres core | Core 1.12.6 / disposable Postgres 16, Python 3.11, four workers | 8 passed | 187.99 s |
| Required CI Postgres core | Core 1.12.6 / Postgres 16, Python 3.14, run 42539282721 | 8 passed | 7m58s job time |
| Native Windows portability | Windows / Python 3.11, same run | Passed, including real engines and startup-injected symlink denial | 4m00s job time |

The first local Postgres attempt failed because pgcrypto was missing. Its output is retained;
the extension was installed in the disposable database before the successful rerun.
Development checks are not final-candidate BigQuery runtime evidence.

## 0.2.5 development checks (before candidate freeze)

These checks guide development; they are not frozen-candidate BigQuery release evidence.

| Check | Engine / location | Result | Duration / source |
| --- | --- | --- | --- |
| Postgres core, four workers | Core 1.12.6 / disposable local Postgres 16 | Exactly 8 passed | 186.61 s; initial #105 development worktree |
| Populated identity migration, damaged-name refusal and retry | Core 1.12.6 / disposable local Postgres 16 | 1 passed | 30.02 s; worktree committed as `ba5664a` |
| Migration plus actual payload-event behavior | Core 1.12.6 / disposable local Postgres 16 | 2 passed | 68.66 s; #105 development worktree |
| Real dbt 2 offline checks | dbt 2.0.11 / local, no warehouse queries | 17 passed | 16.41 s; #105 development worktree |
| Complete release-preparation units | Python 3.11 / local | 662 passed, 25 conditional skips | 62.91 s; 0.2.5 preparation worktree |

The release additionally requires a separate serial `bigquery-upgrade` receipt. It reproduces the
0.2.4 defect on pinned historical source, migrates and repairs that disposable installation,
and exercises both engines against it. Both engine inventories and dependency digests are
required. Its execution and the two full warehouse suites remain subject to the candidate/cost
checkpoint; ordinary offline compilation does not certify this recovery path.
