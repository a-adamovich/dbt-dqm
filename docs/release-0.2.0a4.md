# 0.2.0a4 release evidence

Status: implementation and validation in progress; no tag or publication yet.
Python/application/lock version: `0.2.0a4`; dbt version: `0.2.0-a4`; intended tag: `v0.2.0a4`.
Baseline: `13bf825900ceee330639c773474288db70a10bf8`. Branch: `codex/dqm-workflow-alpha4`.

This alpha introduces a breaking warehouse contract (`dqm-full-v1`). There is no history migration.
The maintainer authorized implementation, merge and publication after all gates pass, and explicitly
authorized a DQM full refresh in `dbt-dqm.dbt_dqm_qa` for the serial permissions gate. Only known
DQM-owned tables and manifest-resolved package relations may be reset. Preserve unrelated QA
relations, dataset access, identities, keys and IAM permissions. No new keys or IAM grants are authorized.

Required evidence on the production candidate:

| Gate | Status |
| --- | --- |
| Current CI, optional extras/Python 3.11–3.14 and native Windows/macOS/Linux checks | Pending |
| Full PostgreSQL 14/16 acceptance | Pending |
| Complete credentialed BigQuery acceptance, disposable datasets | Pending |
| Serial restricted runner/reviewer gate after the authorized scoped QA reset | Pending |
| Real browser stable ordering during edits/Apply, both browsing modes, paging/filter/offline recovery | Pending |
| Cache-boundary memory | Pending |
| Security audits and trusted publisher/environment verification | Pending |
| Capture benchmark: 15 tests, 264 raw failed rows, timing and statement counts | Pending |
| Candidate review, exact tested revision and merge content verification | Pending |
| Protected approval, trusted publication and provenance verification | Pending |
| Public hashes/metadata, all 16 PyPI combinations and Git-tag dbt installation | Pending |

Automated native Windows coverage must be distinguished from corporate-environment verification:
no new corporate Windows consumer verification has been claimed for this alpha.
