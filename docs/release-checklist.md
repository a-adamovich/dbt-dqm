# Release checklist

Every release requires the normal credentialed gate. Its keyless BigQuery workflow is set up and was
proven on `0ada4e1` ([run 42969267595](bigquery-lifecycle.md#proof-runs-2026-11-11)). That run is
an infrastructure proof, not release evidence: each release needs a passing run on its exact candidate.

Use the [gates below](#gates) to prepare a candidate and verify the tagged release before
publication. See [Historical records](#historical-records) for earlier verification.

Record every result against the **exact release commit SHA and complete source tree**. Finish all
tracked text before validation. A new commit SHA requires fresh evidence, even when the source
tree is identical. Store generated receipts outside the repo.

## Gates

| # | Gate | How |
| --- | --- | --- |
| 1 | Fast CI passes on the exact release commit from a push to `main` | The `required` check covers units on Python 3.11–3.14, packaging/security floors, representative native portability, real dbt 2 parse/compile, offline BigQuery compile and eight real Postgres core cases |
| 2 | Manual full Postgres/platform validation passes | Run `Full validation` on the exact release commit. Its `validated` check requires the complete Postgres 14/16 and Python 3.11–3.14 matrix, Core 1.11.0, full packaging/portability/compile matrices, candidate QA, and passing warehouse evidence from the same run attempt |
| 3 | Credentialed BigQuery acceptance on the exact release commit | Run both `lifecycle (dbt2)` and `lifecycle (dbt1)` in one manual [keyless BigQuery lifecycle run](bigquery-lifecycle.md) on `main`, with zero skips, failures, or errors. [`bigquery_suite()`](../scripts/release_evidence.py) currently selects all 33 acceptance cases on dbt 2 and the nine `BIGQUERY_SMOKE_TESTS` cases on Core |
| 4 | Cross-engine upgrade and restricted runner/reviewer permissions, run serially after 3 | The `cross-engine-upgrade` and `restricted-permissions` jobs of the same run, both keyless, serial and on disposable datasets. The release gate requires all four jobs and their evidence from one run attempt. For development diagnosis only, `test_bigquery_permissions.py` can still run locally against shared QA with the existing runner and reviewer credential files; that local run is not release evidence |
| 5 | Installed-wheel browser QA in every Full validation run | See the `stable-candidate-browser` artifact for Sync, Health and Maintenance, and CSV export in snapshot and warehouse modes, plus a notes edit with Apply and sync in snapshot mode |
| 6 | Cache-boundary memory and exports in every Full validation run | See the `stable-candidate-memory` artifact for `integration_tests/scale/cache_boundary.py` at 50,000 issues, including CSV and Excel export |
| 7 | Versions agree | `pyproject.toml` and `dbt_project.yml` versions are equal, and the tag is `v<version>`. A Python pre-release is spelled in SemVer for dbt (`0.2.0a1` → `0.2.0-a1`); no other spelling is accepted. `release.yml` refuses a mismatch before installing or building anything. |
| 8 | No open Dependabot alerts | Or each remaining alert has a written applicability note **and** the maintainer's explicit acceptance |
| 9 | PyPI trusted publisher confirmed | Project `dbt-dqm`, owner `a-adamovich`, repository `dbt-dqm`, workflow `release.yml`, environment `pypi` ([#12](https://github.com/a-adamovich/dbt-dqm/issues/12)) |
| 10 | Release approval pause observed | The publishing job waits on `pypi`; future releases require credentialed verification and maintainer approval. |
| 11 | Release-time wheel verification and Postgres demo pass | The tagged release workflow verifies the installed candidate wheel in an isolated environment. Its Postgres demo runs from the checkout and asserts outcomes for the initial, passed, and recurrence scenarios |

Headless [AppTest cases](../tests/test_ui_browse.py) cover warehouse paging, filters, search
refusal, later-page edits, and the offline view. Automated coverage of the busy message is
limited to the [error classifier](../tests/test_sync_and_errors.py). When the app or its
dependencies change, manually verify in the browser that the busy message appears under a
held lock.

The maintainer decides when to tag the validated candidate and whether to approve publication.

The `validated` result covers Postgres and platforms only. For the roles of offline BigQuery
compilation, separately authorized dbt 2 development smoke, and credentialed release validation,
see [BigQuery tiers](ci-measurements.md#bigquery-tiers).

## Audits and query limits

Pinned pip-audit checks block full validation and publishing on unaccepted findings or service
failures. Only the five recorded sqlparse advisories are accepted in the isolated Core 1.11.0
compatibility engine. The unpublished root package may not have an advisory-service record;
that limitation is explicit, and third-party skipped dependencies are failures.

App search has its own byte ceiling. The test profile cap is not enforced by dbt 2.0.11 on
known paths, does not cover every app/permissions query, and is not a whole-suite budget.

## Operating limits that stay true after release

- Captures that share stored-failure tables must be serialized; equal row counts don't prove
  provenance.
- BigQuery supports one reconciliation per resolved DQM installation at a time; overlaps are detected, not queued.
- 0.1 users need a fresh schema for 0.2.
- Event payloads default to `full` with indefinite retention unless configured.
- App cache ceilings bound serialized data, not process memory. Above them the app browses the
  warehouse (`--browse auto`); on BigQuery each 2,000-issue read and each summary is billed.
- Maintenance health is a monitoring approximation (see
  [warehouse interfaces](warehouse-interfaces.md#optional-maintenance)).
- Live interactive OAuth sign-in for the app is not exercised by the automated suites.

## Historical records

See the [archive](archive.md) for dated verification and review records.
