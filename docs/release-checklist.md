# Release checklist

This is the single current list of what must pass before a release tag is pushed. Dated records of
earlier verification are kept as history (see [Historical records](#historical-records)); they
don't replace this list.

Record every result against one **candidate commit**: the exact production commit being released.
A later commit that only adds these results to the docs must name the candidate, and passes
ordinary CI itself; the warehouse suites below ran on the candidate, not on that docs commit.

## Gates

| # | Gate | How |
| --- | --- | --- |
| 1 | CI passes on the candidate | `.github/workflows/ci.yml`: lint and unit tests, package build and wheel checks (including dependency security floors), Postgres 14 and 16 acceptance, BigQuery compile |
| 2 | Postgres 14 and 16 acceptance, full suite | `DBT_DQM_TEST_DSN=… uv run pytest integration_tests/acceptance/test_postgres.py` against each server |
| 3 | Credentialed BigQuery acceptance, **one full run** on the candidate | `DBT_DQM_BIGQUERY_TEST_PROJECT=… uv run pytest integration_tests/acceptance/test_bigquery.py`. Every collected test passes; no credential-related skips; record the actual count. |
| 4 | Restricted runner/reviewer permissions gate, run serially after 3 | `DBT_DQM_BIGQUERY_RUNNER_CREDENTIALS=… DBT_DQM_BIGQUERY_REVIEWER_CREDENTIALS=… uv run pytest integration_tests/acceptance/test_bigquery_permissions.py` against the shared QA dataset |
| 5 | Browser QA of the review app, when the app or its dependencies changed | Sync, edit and Apply, the "busy" message under a held lock, the Health tab including Maintenance |
| 6 | Cache-boundary memory, when runtime code or dependencies changed | `integration_tests/scale/cache_boundary.py` at 50,000 issues; record it in [scale](scale.md) |
| 7 | Versions agree | `pyproject.toml` and `dbt_project.yml` versions are equal, and the tag is `v<version>`. `release.yml` refuses a mismatch before installing or building anything. |
| 8 | No open Dependabot alerts | Or each remaining alert has a written applicability note **and** the maintainer's explicit acceptance |
| 9 | PyPI trusted publisher confirmed | Project `dbt-dqm`, owner `a-adamovich`, repository `dbt-dqm`, workflow `release.yml`, environment `pypi` ([#12](https://github.com/a-adamovich/dbt-dqm/issues/12)) |
| 10 | Release approval pause observed | Pushing the tag queues `release.yml`; its job waits for a reviewer on the `pypi` environment **before any step runs**. Approve only after gates 1–9; after approval the job checks the tag, verifies, builds, attests, verifies provenance and publishes in one run, so a failure in any step stops publication |

Tagging and publishing remain the maintainer's decision after every gate passes.

## Results

| Gate | Candidate commit | Result | Evidence |
| --- | --- | --- | --- |
| — | — | Not yet run for 0.2.0 | — |

## Operating limits that stay true after release

- Captures that share stored-failure tables must be serialized; equal row counts don't prove
  provenance.
- BigQuery supports one reconciliation per dataset at a time; overlaps are detected, not queued.
- 0.1 users need a fresh schema for 0.2.
- Event payloads default to `full` with indefinite retention unless configured.
- App cache ceilings bound serialized data, not process memory.
- Maintenance health is a monitoring approximation (see
  [warehouse interfaces](warehouse-interfaces.md#optional-maintenance)).
- Live interactive OAuth sign-in for the app is not exercised by the automated suites.

## Historical records

Dated verification and review records, preserved as written:

- [Permissions and release safeguards verification](qa-release-safeguards.md) (2026-10-09)
- [0.2 implementation and verification](implementation-0.2.md) (2026-10-07)
- [Project review](project-review.md) (2026-08-09)
