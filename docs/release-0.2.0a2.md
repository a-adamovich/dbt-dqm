# 0.2.0a2 release candidate evidence

Recorded 2026-10-12. These checks apply to the production candidate below. Tagging and publication
are pending; this record does not claim that the release has been published.

- Baseline: `800d330ae4774c47dc81c5f7e9732885777579ae` (released 0.2.0a1).
- Production candidate: `708b6c27460be0669b55d68c7096c43e41f8c5ae`.
- Preparation branch: `codex/python-311-314-alpha2`, [PR #62](https://github.com/a-adamovich/dbt-dqm/pull/62).
- [Candidate CI run 38344711820](https://github.com/a-adamovich/dbt-dqm/actions/runs/38344711820): 20/20 jobs passed.

The later documentation commit records these results; the warehouse suites and memory probe ran
on `708b6c2`, not on that documentation commit. Runtime, dbt package, dependency and CI files are
unchanged by the evidence commit.

## Changes

- Python support expands to 3.11–3.14 (`>=3.11,<3.15`), with 3.14 recommended.
- dbt-core stays at 1.12.5, dbt-bigquery at 1.12.1 and dbt-postgres at 1.11.x.
- Mashumaro 3.14 was reproduced failing during dbt initialization on Python 3.14. The distributable
  now requires `mashumaro>=3.17,<3.18`. Streamlit's minimum is 1.52, where Python 3.14 support began.
- The old lock's AI Platform 1.163.0/GenAI 2.17.0 branch conflicted with dbt-bigquery's Cloud Storage
  constraint. Refreshing that branch resolves AI Platform 1.148.1/GenAI 1.75.0. All other locked
  dependency versions are retained.
- CI covers all four Python minors, including clean wheel installs, upgrades from vulnerable and
  incompatible installed dependencies, BigQuery compilation and Postgres 14/16 acceptance.
- Release and scheduled BigQuery workflows use Python 3.14. No warehouse schema migration is added.

## Results

| Gate | Result | Evidence |
| --- | --- | --- |
| CI | 20/20 passed | Four unit/app jobs, four distribution jobs, four BigQuery compile jobs, eight Postgres jobs in the candidate CI run |
| Postgres acceptance | 32/32 on both 14 and 16 | Python 3.14/Postgres 14 CI JUnit: 932.733 s; local Python 3.14.6/Postgres 16.11: 902.96 s. CI also passes both servers on every other supported minor |
| Full credentialed BigQuery | 25/25 in one run, no skips | Python 3.14.6, six workers, 1,500.85 s; existing development service-account file and labelled disposable datasets in `dbt-dqm` |
| Restricted runner/reviewer | Passed: one test, 380.95 s; both identities verified with `SESSION_USER()` | Existing local runner/reviewer files, serially after the full BigQuery run; no IAM changes or shared-dataset reset |
| Browser QA | Passed | Clean wheel install on Python 3.14.6; synthetic Postgres consumer, details below |
| 50,000-issue memory boundary | 483.0 MiB peak RSS | 1.99 s reads, 5.06 s total; [raw result](scale-results/candidate-708b6c2-cache-boundary.json) and [scale record](scale.md#python-314-alpha-candidate-2026-10-12) |
| Versions agree | Passed | Python/application/lock `0.2.0a2`, dbt `0.2.0-a2`; intended tag `v0.2.0a2` |
| Security | No open Dependabot alerts; no known vulnerabilities in either audited install | `pip-audit`: 118 packages in the normal wheel install, 125 in the locked environment |
| Publisher and approval configuration | Existing configuration retained | `a-adamovich/dbt-dqm`, `release.yml`, environment `pypi`, `v*` tag policy and required reviewer `a-adamovich` |
| Release approval pause | Pending tag | The release job still waits for the required reviewer before any step runs |

Local Python 3.14 unit verification: 197 passed, four opt-in/CI-only checks skipped. With the wheel
supplied, all five security/Python metadata and dbt initialization checks passed. Ruff and installed
dependency consistency checks passed. Live WIF certification is not added by this release.

The restricted test creates no disposable datasets. Its generic session-end sweep lacked default
ADC and was skipped; this was not a skipped test. The full BigQuery gate used development ADC
and completed its own disposable-dataset cleanup.

## Browser QA

An isolated Postgres 16 consumer contained 208 synthetic Active issues. The app ran from a clean
wheel installation outside the synced checkout; all warehouse tests used a clean candidate clone.

- Snapshot sync loaded all 208 issues. A notes edit persisted. With the fixture occurrence table
  locked, Apply displayed the busy/retry message and kept the edit pending. Releasing the lock
  and retrying committed the edit once (the same occurrence reached annotation version 2).
- Health showed test and area health. Maintenance showed all four configured steps as `ok`.
- Warehouse paging traversed pages 1–3. A page-3 notes edit applied without moving away from page 3.
- A two-character search was refused without applying it. Searching `ALPHA2` returned the expected
  201 synthetic customer issues. The recurrence filter returned zero, and clearing both restored 208.
- After stopping only the isolated QA database, a read showed the offline cached view. A second
  issue's edit stayed pending with Apply disabled. Restarting the database and retrying restored
  browsing, and Apply persisted that edit.
- Final warehouse verification found the busy/retry edit at version 2 and the warehouse-page and
  offline edits at version 1 each. Browser, app servers and the isolated database were stopped.

## Candidate distributions

Both distributions pass `twine check`; inventory excludes credentials, local profiles, caches,
bytecode, logs, targets and installed dependencies. The wheel has 16 entries and the source archive
193 entries. These hashes identify the local candidate build; the release workflow rebuilds and
attests its own artifacts.

| Artifact | SHA-256 |
| --- | --- |
| `dbt_dqm-0.2.0a2-py3-none-any.whl` | `9e227c16dcc92abcd7fd34284df3e966d7924c579eeedae248c69fbcba11a6e6` |
| `dbt_dqm-0.2.0a2.tar.gz` | `73d6c0277c0f95473b1be1e44d7de2d407da37f10e85d2b27c5bf79697eca0a7` |
