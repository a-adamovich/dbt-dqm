# Release checklist

Every release requires the normal credentialed gate. Its keyless BigQuery workflow is set up and was
proven on `0ada4e1` ([run 42969267595](bigquery-lifecycle.md#proof-runs-2026-11-11)). That run is
an infrastructure proof, not release evidence: each release needs a passing run on its exact candidate.

This is the single current list of what must pass before a release tag is pushed. Dated records of
earlier verification are kept as history (see [Historical records](#historical-records)); they
don't replace this list.

Record every result against one **candidate SHA and complete source tree**. For ordinary candidates, finish
all tracked text before validation. A merge may change the SHA while preserving the identical
tree; record both identities and verify equality through GitHub. Any tree change, including
documentation, invalidates that candidate's evidence. Store generated receipts outside the repo.

## Gates

| # | Gate | How |
| --- | --- | --- |
| 1 | Fast CI passes on the candidate | The `required` check covers units on Python 3.11–3.14, packaging/security floors, representative native portability, real dbt 2 parse/compile, offline BigQuery compile and eight real Postgres core cases |
| 2 | Manual full Postgres/platform validation passes | Run `Full validation` on the candidate. Its `validated` check requires the complete Postgres 14/16 and Python 3.11–3.14 matrix, Core 1.11.0, full packaging/portability/compile matrices and candidate QA; warehouse counts and exact-run evidence must pass |
| 3 | Credentialed BigQuery acceptance, **one full run per engine** on the candidate | The `lifecycle (dbt2)` and `lifecycle (dbt1)` jobs of a manual [`bigquery-lifecycle.yml`](bigquery-lifecycle.md) run on the candidate: every case in `test_bigquery.py` on dbt 2 and the exact `BIGQUERY_SMOKE_TESTS` selection on Core, with zero skips/failures/errors |
| 4 | Cross-engine upgrade and restricted runner/reviewer permissions, run serially after 3 | The `cross-engine-upgrade` and `restricted-permissions` jobs of the same run, both keyless, serial and on disposable datasets. The release gate requires all four jobs and their evidence from one run attempt. For development diagnosis only, `test_bigquery_permissions.py` can still run locally against shared QA with the existing runner and reviewer credential files; that local run is not release evidence |
| 5 | Browser QA of the review app, when the app or its dependencies changed | Sync, edit and Apply, the "busy" message under a held lock, the Health tab including Maintenance; in warehouse mode (`--browse warehouse`) paging, filters, search (including a refusal), an edit applied from a later page, and the offline view |
| 6 | Cache-boundary memory, when runtime code or dependencies changed | `integration_tests/scale/cache_boundary.py` at 50,000 issues; record it in [scale](scale.md) |
| 7 | Versions agree | `pyproject.toml` and `dbt_project.yml` versions are equal, and the tag is `v<version>`. A Python pre-release is spelled in SemVer for dbt (`0.2.0a1` → `0.2.0-a1`); no other spelling is accepted. `release.yml` refuses a mismatch before installing or building anything. |
| 8 | No open Dependabot alerts | Or each remaining alert has a written applicability note **and** the maintainer's explicit acceptance |
| 9 | PyPI trusted publisher confirmed | Project `dbt-dqm`, owner `a-adamovich`, repository `dbt-dqm`, workflow `release.yml`, environment `pypi` ([#12](https://github.com/a-adamovich/dbt-dqm/issues/12)) |
| 10 | Release approval pause observed | The publishing job waits on `pypi`; future releases require credentialed verification and maintainer approval. |

Tagging and publishing remain the maintainer's decision after every gate passes.

The `validated` result covers Postgres and platforms only. Automated cloud PR smoke and broader evidence reuse remain unavailable. See [CI tiers and measurements](ci-measurements.md).

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
