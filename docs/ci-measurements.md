# CI tiers and validation

The current policy combines required PR checks, manual Postgres and platform validation, and a
keyless BigQuery release gate. The redesign's approved ADRs will define future validation tiers.

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

Full validation covers **Postgres and platforms only**. Publication requires the credentialed
[release gate](release-checklist.md#gates). The maintainer must configure `required` as a required
status and require up-to-date PRs.

## Measurement record

Targets are measurements to verify, not promises: Postgres core within ten minutes and fast
PR wall time within fifteen minutes. Keep all assertions if setup or execution needs tuning.
Each warehouse run writes JUnit and `--durations=25` output, installed versions, and JSON
evidence. Record suite, engine, worker count, elapsed time, result, and source revision in the run evidence.

[Historical measurements](https://github.com/a-adamovich/dbt-dqm/blob/3043befbf99b8b82519f79ce6081a2a0711232d5/docs/ci-measurements.md#measurement-record).

The archived [development measurement tool](https://github.com/a-adamovich/dbt-dqm/blob/3043befbf99b8b82519f79ce6081a2a0711232d5/scripts/bigquery_measure.py) measured billed bytes and slot milliseconds after each approved
suite, excluding parent `SCRIPT` rows. It separates labelled queries (including children) from
unlabelled queries referencing the test datasets; shared-QA activity may include other users.
Missing metadata permissions are reported as unavailable, never as zero usage. No GCP roles or
quotas are changed. Do not infer cost from fixture size or elapsed time.

Acceptance fixture clients and dbt profiles request a per-query limit of 1 GiB by default,
configurable with `DBT_DQM_BIGQUERY_MAX_BYTES_BILLED`. Real 2.0.9 and 2.0.11 offline checks
accept the profile/label configuration. dbt 2.0.11 did not enforce this profile limit on previously tested paths. Treat it as configured, not guaranteed; record fresh observations during live validation.
App search retains its own configured limit; other app queries and the restricted-permissions
fixture are not covered by this test cap. This is not a total-suite spending ceiling.

## Assertion map

| Retained case | Absorbed case | Where its assertions are |
| --- | --- | --- |
| `test_bigquery_apply_fencing_overlap_stale_generation_and_abandoned_runner` | `test_bigquery_overlapping_applies` | Unchanged, as the first half. It needs the clean installation |
| same | `test_bigquery_stale_generation_annotations_and_abandoned_runner` | Unchanged, as the second half, after the overlap leaves the same seven occurrences |

In a merged case, the first failure hides any later ones.

## BigQuery tiers

- **Offline compilation** runs in automatic PR CI and manual Full validation without warehouse
  access. It verifies compilation.
- **Development smoke** runs the nine cases in `BIGQUERY_SMOKE_TESTS` on dbt 2.0.11. Before merging
  a change to macros, models, BigQuery adapter or SQL behavior, `warehouse.py`, browsing, or the CLI
  runner, obtain maintainer authorization for one run. Set up approved Application Default
  Credentials for a test project where the fixture can create and tear down disposable datasets.
  Set `DBT_DQM_BIGQUERY_TEST_LOCATION` to the approved location (the default is `US`).
  From the repository root, run:

  ```bash
  DBT_DQM_BIGQUERY_TEST_PROJECT="approved-test-project" \
    DBT_DQM_BIGQUERY_TEST_LOCATION="US" \
    DBT_DQM_DBT_EXECUTABLE="/absolute/path/to/dbt-2.0.11" \
    uv run --group dbt1 --extra bigquery pytest -q -rA -n 4 -m bigquery_smoke \
    integration_tests/acceptance/test_bigquery.py
  ```

- **Release validation** runs the keyless lifecycle workflow on the exact release candidate on
  `main`. The current selections in [`bigquery_suite()`](../scripts/release_evidence.py) are 33
  full acceptance cases on dbt 2 (six workers) and the nine `BIGQUERY_SMOKE_TESTS` cases on Core
  (four workers). The same attempt also supplies cross-engine upgrade and restricted-permissions
  evidence. See the [release checklist](release-checklist.md#gates).

The 1 GiB qualifications in the measurement record above apply to credentialed runs.
