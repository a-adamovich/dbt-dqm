# 0.2.1 release verification

Production candidate: `7ce7e96d1502b1901878e1a9ddc71a6e0dda55c0`. The `v0.2.1` tag points
at that exact commit. PR [#74](https://github.com/a-adamovich/dbt-dqm/pull/74) merged the candidate;
the merge tree is identical. This later documentation-only update records its actual results.

This patch fixes BigQuery tracking in datasets with default collation. Python and dbt packages
use `0.2.1`. Uncollated installations need no schema migration; existing collated tables require
[explicit scoped recovery](operations.md#bigquery-dataset-collation).

## Candidate gates

- [CI run 39323090036](https://github.com/a-adamovich/dbt-dqm/actions/runs/39323090036): all 28 jobs
  passed. This includes unit tests, lint, packaging, native Windows/macOS checks, offline BigQuery
  compilation, and all 39 Postgres acceptance tests on every Python 3.11–3.14 and Postgres 14 or 16 combination.
- Full credentialed BigQuery acceptance: **32 passed, zero skipped**, 2,389.65 seconds. The new
  `und:ci` cases verify initialization, case-sensitive owners and payload changes, annotations and
  retries, nullable comparisons, pass/recurrence, refusal of collated tracking tables, and explicit
  recovery. Each test used its own labelled disposable dataset.
- Restricted runner/reviewer gate: **1 passed, zero skipped**, 368.04 seconds, run serially after
  the full BigQuery suite using the existing credential files and synthetic QA fixtures. No QA
  history reset, new keys or IAM changes were performed.
- Local unit suite: **425 passed, 8 optional tests skipped**. Focused SQL/docs/release checks:
  **76 passed**. Lockfile validation, Ruff, and wheel/source archive metadata checks passed.
- [Installed-wheel QA run 39323083904](https://github.com/a-adamovich/dbt-dqm/actions/runs/39323083904):
  snapshot and warehouse browser flows passed, including Sync, Health, an applied annotation and
  complete 47-row CSV exports. Browser screenshots and logs are attached to that run.
- The same QA run synced **50,000 issues**, with **585.3 MiB peak RSS** and complete CSV and Excel
  exports of **50,000 rows each**. [Raw measurement](scale-results/candidate-7ce7e96-cache-boundary.json).
- Versions agree at `0.2.1`; the dependency alert list was empty immediately before merge.

## Publication

[Release run 39330786084](https://github.com/a-adamovich/dbt-dqm/actions/runs/39330786084) uses the
existing protected `pypi` environment, trusted publisher and provenance verification. Its required
reviewer pause was observed before any job step, then approved under the maintainer's explicit
request to publish 0.2.1, after every candidate gate passed. No protection settings changed.

Publication and public artifact evidence is maintained on the
[v0.2.1 release page](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.1).


The release workflow completed successfully and published the stable GitHub release and
[PyPI version 0.2.1](https://pypi.org/project/dbt-dqm/0.2.1/) on 2026-10-19. Public PyPI and
GitHub artifact hashes match. Both downloaded artifacts passed attestation verification for the
release workflow, exact candidate commit and `refs/tags/v0.2.1`, excluding self-hosted runners.

| Artifact | SHA-256 |
| --- | --- |
| `dbt_dqm-0.2.1-py3-none-any.whl` | `d4b3dc71677939af9c20e73cbea76499119595409b80de62fc5684f0040bbd5f` |
| `dbt_dqm-0.2.1.tar.gz` | `98cbfed1ec309715a37d35aafb4f167ed6e8664772497d932a94f1f9102b88e6` |
