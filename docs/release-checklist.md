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
| 3 | Credentialed BigQuery acceptance, **one full run** on the candidate | `DBT_DQM_BIGQUERY_TEST_PROJECT=… uv run pytest -n 6 --dist load integration_tests/acceptance/test_bigquery.py` (about 25 minutes in parallel; each test uses its own labelled, disposable dataset, and leftovers are swept at session end). Every collected test passes; no credential-related skips; record the actual count. |
| 4 | Restricted runner/reviewer permissions gate, run serially after 3 | `DBT_DQM_BIGQUERY_RUNNER_CREDENTIALS=… DBT_DQM_BIGQUERY_REVIEWER_CREDENTIALS=… uv run pytest integration_tests/acceptance/test_bigquery_permissions.py` against the shared QA dataset |
| 5 | Browser QA of the review app, when the app or its dependencies changed | Sync, edit and Apply, the "busy" message under a held lock, the Health tab including Maintenance; in warehouse mode (`--browse warehouse`) paging, filters, search (including a refusal), an edit applied from a later page, and the offline view |
| 6 | Cache-boundary memory, when runtime code or dependencies changed | `integration_tests/scale/cache_boundary.py` at 50,000 issues; record it in [scale](scale.md) |
| 7 | Versions agree | `pyproject.toml` and `dbt_project.yml` versions are equal, and the tag is `v<version>`. A Python pre-release is spelled in SemVer for dbt (`0.2.0a1` → `0.2.0-a1`); no other spelling is accepted. `release.yml` refuses a mismatch before installing or building anything. |
| 8 | No open Dependabot alerts | Or each remaining alert has a written applicability note **and** the maintainer's explicit acceptance |
| 9 | PyPI trusted publisher confirmed | Project `dbt-dqm`, owner `a-adamovich`, repository `dbt-dqm`, workflow `release.yml`, environment `pypi` ([#12](https://github.com/a-adamovich/dbt-dqm/issues/12)) |
| 10 | Release approval pause observed | Pushing the tag queues `release.yml`; its job waits for a reviewer on the `pypi` environment **before any step runs**. Approve only after gates 1–9; after approval the job checks the tag, verifies, builds, attests, verifies provenance and publishes in one run, so a failure in any step stops publication |

Tagging and publishing remain the maintainer's decision after every gate passes.

### 0.2.0a1 authentication and publication criteria

The maintainer confirmed PyPI trusted publishing and the `pypi` approval environment, and
authorized the following alpha criteria on 2026-10-11:

- Gates 3 and 4 still require complete live BigQuery runs, using existing local service-account
  files. No keys are created or downloaded. Gate 4 verifies each identity with `SESSION_USER()`.
- WIF support is verified separately by `tests/test_wif.py`, offline through real app/dbt ADC
  loaders and simulated exchange/impersonation responses. It requires no WIF infrastructure.
  Live WIF and IAM are not certified by this smoke test.
- All other gates remain required. No shared dataset reset, IAM change or approval bypass is
  authorized. Scheduled WIF verification stays inactive unless already configured.
- Publish only `v0.2.0a1`. dbt Hub rejects this Python-style alpha tag; submit registration after
  publication and retain Git installation instructions without claiming an alpha Hub listing.

The [0.2.0a1 evidence](release-0.2.0a1.md) records every gate passing on candidate `69703f7`.
The maintainer approved the SemVer spelling `0.2.0-a1` for `dbt_project.yml` on 2026-10-11. The
first preparation candidate, `d0385e6`, was blocked and is superseded.

### 0.2.0a2 migration and publication criteria

The maintainer authorized the Python 3.11–3.14 migration, merging its pull request and publishing
`v0.2.0a2` on 2026-10-12. Gates 3–6 run on Python 3.14; CI covers all four supported Python minors
and both Postgres versions. Existing local service-account files are used for live BigQuery gates,
without new keys, IAM changes or a shared QA reset. The permissions gate runs serially after the
full BigQuery suite. Existing release approval, trusted publishing and provenance checks remain.

## Results

Production candidate: `708b6c27460be0669b55d68c7096c43e41f8c5ae` for 0.2.0a2. Full evidence is in
[the candidate record](release-0.2.0a2.md). A later documentation commit records these results.

| Gate | Candidate | Result | Evidence |
| --- | --- | --- | --- |
| 1 | `708b6c2` | CI passed 20/20 | [Run 38344711820](https://github.com/a-adamovich/dbt-dqm/actions/runs/38344711820), Python 3.11–3.14 |
| 2 | `708b6c2` | Postgres 14 and 16: 32/32 each | Eight CI acceptance jobs pass; Python 3.14/Postgres 14 JUnit 932.733 s; local Postgres 16.11 902.96 s |
| 3 | `708b6c2` | BigQuery 25/25 in one run, no skips | Six workers, 1,500.85 s, labelled disposable datasets |
| 4 | `708b6c2` | Passed: one test, 380.95 s; both identities verified with `SESSION_USER()` | Existing restricted runner/reviewer identities, serially after gate 3 |
| 5 | `708b6c2` | Browser QA passed | Snapshot, busy/retry, Health/Maintenance, warehouse paging/search/filter/edit and offline recovery |
| 6 | `708b6c2` | Peak RSS 483.0 MiB | [Raw 50,000-issue result](scale-results/candidate-708b6c2-cache-boundary.json) |
| 7 | `708b6c2` | Python `0.2.0a2`, dbt `0.2.0-a2` | Intended tag `v0.2.0a2`; version agreement and actual tag-check script tested |
| 8 | 2026-10-12 | No open alerts; both audits clean | 118 normal wheel dependencies, 125 locked packages, zero known vulnerabilities |
| 9 | — | Existing publisher/approval configuration retained | `release.yml`, `pypi`, required reviewer `a-adamovich`, tag policy `v*` |
| 10 | — | Pending tag | Observe and satisfy the existing reviewer gate before publication |

## 0.2.0a1 results (historical)

| Gate | Candidate commit | Result | Evidence |
| --- | --- | --- | --- |
| 1 | `69703f7` | Passed 5/5 | [CI run 38189103369](https://github.com/a-adamovich/dbt-dqm/actions/runs/38189103369) |
| 2 | `69703f7` | Postgres 16.11: 32/32 (1,037 s); Postgres 14.20: 31/32 in the full run, with the remaining test passing alone (25 s) | Local, clean clone. The one test timed out in setup under concurrent BigQuery load and passed alone and in CI ([details](release-0.2.0a1.md#results-for-the-candidate)) |
| 3 | `69703f7` | 25/25 in one run, 6 workers, no skips (31 m 53 s) | Local credentialed run, project `dbt-dqm`, disposable datasets |
| 4 | `69703f7` | Passed (5 m 52 s) | Local, run serially after 3 against `dbt_dqm_qa`; identities checked with `SESSION_USER()` |
| 5 | `69703f7` | Passed | Snapshot and warehouse modes, busy/retry, Health with Maintenance `OK`, paging, filter, search and refusal, edit from page 3, offline edit and recovery ([details](release-0.2.0a1.md#browser-qa)) |
| 6 | `69703f7` | Peak RSS 583.1 / 583.8 / 583.9 MiB | Inside the documented 435–585 MiB range; [`candidate-69703f7-cache-boundary.json`](scale-results/candidate-69703f7-cache-boundary.json) |
| 7 | `69703f7` | Python `0.2.0a1`, dbt `0.2.0-a1`; the release workflow refuses any other pairing | Unit tests run the release step script |
| 8 | 2026-10-11 | No open Dependabot alerts; `pip-audit` clean | GitHub Dependabot alerts |
| 9 | — | Confirmed by the maintainer for 0.2.0a1 | [#12](https://github.com/a-adamovich/dbt-dqm/issues/12); `pypi` required-reviewer and `v*` tag protection inspected 2026-10-11 |
| 10 | — | Not yet observed (no tag pushed) | Observed on the `v0.2.0a1` tag |

This table was added by a later documentation-only commit; the warehouse suites ran on `69703f7`,
not on that commit. The `613e089` results it replaces are in this file's git history.

## Operating limits that stay true after release

- Captures that share stored-failure tables must be serialized; equal row counts don't prove
  provenance.
- BigQuery supports one reconciliation per dataset at a time; overlaps are detected, not queued.
- 0.1 users need a fresh schema for 0.2.
- Event payloads default to `full` with indefinite retention unless configured.
- App cache ceilings bound serialized data, not process memory. Above them the app browses the
  warehouse (`--browse auto`); on BigQuery each 2,000-issue read and each summary is billed.
- Maintenance health is a monitoring approximation (see
  [warehouse interfaces](warehouse-interfaces.md#optional-maintenance)).
- Live interactive OAuth sign-in for the app is not exercised by the automated suites.

## Historical records

Dated verification and review records, preserved as written:

- [Permissions and release safeguards verification](qa-release-safeguards.md) (2026-10-09)
- [0.2 implementation and verification](implementation-0.2.md) (2026-10-07)
- [Project review](project-review.md) (2026-08-09)
