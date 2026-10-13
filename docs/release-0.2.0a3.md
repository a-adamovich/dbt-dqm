# 0.2.0a3 release candidate evidence

Recorded 2026-10-13. Publication is pending; the existing release reviewer gate, trusted publisher and provenance checks remain required.

- Production candidate: `4c362266edd5457dc0bedae6348f455e064fcc90`.
- Preparation: `codex/windows-compat-alpha3`, [PR #63](https://github.com/a-adamovich/dbt-dqm/pull/63).
- `09ac850` only corrects a Windows test fixture that attempted to rename its current directory. Production files are unchanged from the candidate.
- Later documentation commits record these results and pass ordinary CI; they do not change production content.

## Changes

Adapters are optional (`bigquery`, `postgres`, or both). The app follows dbt project/profile/target defaults and environment variables, selects dbt beside its own interpreter before PATH, and logs executable/version and complete command diagnostics. Worker failures are normalized before process transfer, preserving cache-limit switching and committed-Apply refresh outcomes. Required app relations are checked before sync.

Setup-writing models are ordered before reconciliation. SQL and app queries honor model aliases; macro-created tracking tables retain fixed identifiers. Alias rollout preserves canonical data and gives new aliased Postgres tables their own bounded unique-index names. Selected profile values are rendered once so literal environment credentials are not reinterpreted. No warehouse schema migration is introduced.

## Results

| Gate | Result | Evidence |
| --- | --- | --- |
| Candidate CI | 28/28 passed | [Run 38365848088](https://github.com/a-adamovich/dbt-dqm/actions/runs/38365848088), production-equivalent test-only head `09ac850`; Windows/macOS/Linux, Python 3.11–3.14 and all adapter variants |
| Postgres 14/16 | 33/33 on both versions | Python 3.14 local 16: 348.49 s; CI 14: 1,200.978 s and 16: 1,242.069 s. CI covers both on all four Python minors; alias rollout verifies canonical data preservation and new unique indexes |
| Credentialed BigQuery | 26/26, one complete run, no skips | Python 3.14.6, six workers, 1,786.71 s; existing development identity and labelled disposable datasets |
| Restricted runner/reviewer | Passed: one test, 363.44 s | Both identities verified with `SESSION_USER()`; serially after the full BigQuery gate, existing credentials and shared QA preserved |
| Browser QA | Passed | Clean Postgres-only wheel; no project/profile/target flags; snapshot sync/edit/Apply and Health/Maintenance; held-lock busy/pending preservation/retry; warehouse page-three edit, paging, search/refusal, filters and offline/recovery |
| Cache boundary | 433.8 MiB peak RSS | 50,000 issues; 408-byte average payload; 2.27 s reads, 5.67 s total; [raw result](scale-results/candidate-4c36226-cache-boundary.json) |
| Versions | Agreed | Python/app/lock `0.2.0a3`, dbt `0.2.0-a3`; intended tag `v0.2.0a3` |
| Security | Both audits clean | 124 locked dependencies and 117 normal wheel dependencies, no known vulnerabilities; final wheel Requires-Dist unchanged from the audited metadata |
| Publisher/approval | Retained | `release.yml`, protected `pypi` environment and required reviewer `a-adamovich`; no protection bypass or IAM changes |

Local Python 3.14 regression verification with the built wheel: 231 passed, three platform/opt-in skips. Ruff and clean dependency consistency checks pass. All four clean installed wheel variants pass locally.

## Scope of evidence

Native Windows CI verifies configuration, executable selection, spawn-process error transfer, profile/authentication libraries and wheel variants. It does not certify an individual corporate network, identity setup, UNC server or consumer project. Live BigQuery checks use existing service-account credentials; WIF retains offline loading/exchange/refresh coverage. No keys were created or downloaded and no shared QA dataset was reset.

An earlier live attempt encountered a confirmed BigQuery `backendError`/HTTP 500 during capture. It was stopped; no test was weakened or waived. The complete run above passed, including that case. Browser automation was synchronized with Streamlit rerenders; application errors were not suppressed.

Synthetic browser schemas, cached workspace and local servers were cleaned after verification. Acceptance dataset cleanup remains confined to labelled, uniquely named disposable datasets.
