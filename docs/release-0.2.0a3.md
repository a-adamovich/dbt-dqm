# 0.2.0a3 release evidence

Published 2026-10-13. The existing protected reviewer gate was observed and satisfied; trusted publishing and provenance checks passed without changing the protections.

- Production candidate: `4c362266edd5457dc0bedae6348f455e064fcc90`.
- Preparation: `codex/windows-compat-alpha3`, [PR #63](https://github.com/a-adamovich/dbt-dqm/pull/63).
- `09ac850` only corrects a Windows test fixture that attempted to rename its current directory. Production files are unchanged from the candidate.
- Later documentation commits record these results and pass ordinary CI; they do not change production content.
- Released revision: `b36dfd793e4e7c7448b8b53734a7e9b6ab271cae`, squash merge of PR #63. Its full tree equals the tested final PR head `c32b999c8614bddfb337496c693f96c3559733f0`; production files equal `4c36226`.

## Changes

Adapters are optional (`bigquery`, `postgres`, or both). The app follows dbt project/profile/target defaults and environment variables, selects dbt beside its own interpreter before PATH, and logs executable/version and complete command diagnostics. Worker failures are normalized before process transfer, preserving cache-limit switching and committed-Apply refresh outcomes. Required app relations are checked before sync.

Setup-writing models are ordered before reconciliation. SQL and app queries honor model aliases; macro-created tracking tables retain fixed identifiers. Alias rollout preserves canonical data and gives new aliased Postgres tables their own bounded unique-index names. Selected profile values are rendered once so literal environment credentials are not reinterpreted. No warehouse schema migration is introduced.

## Results

| Gate | Result | Evidence |
| --- | --- | --- |
| Final pre-merge CI | 28/28 passed | [Run 38369686165](https://github.com/a-adamovich/dbt-dqm/actions/runs/38369686165), final head `c32b999`; Windows/macOS/Linux, Python 3.11–3.14 and all adapter variants. Earlier production-equivalent head `09ac850` also passed 28/28 |
| Postgres 14/16 | 33/33 on both versions | Python 3.14 local 16: 348.49 s; CI 14: 1,200.978 s and 16: 1,242.069 s. CI covers both on all four Python minors; alias rollout verifies canonical data preservation and new unique indexes |
| Credentialed BigQuery | 26/26, one complete run, no skips | Python 3.14.6, six workers, 1,786.71 s; existing development identity and labelled disposable datasets |
| Restricted runner/reviewer | Passed: one test, 363.44 s | Both identities verified with `SESSION_USER()`; serially after the full BigQuery gate, existing credentials and shared QA preserved |
| Browser QA | Passed | Clean Postgres-only wheel; no project/profile/target flags; snapshot sync/edit/Apply and Health/Maintenance; held-lock busy/pending preservation/retry; warehouse page-three edit, paging, search/refusal, filters and offline/recovery |
| Cache boundary | 433.8 MiB peak RSS | 50,000 issues; 408-byte average payload; 2.27 s reads, 5.67 s total; [raw result](scale-results/candidate-4c36226-cache-boundary.json) |
| Versions | Agreed | Python/app/lock `0.2.0a3`, dbt `0.2.0-a3`; intended tag `v0.2.0a3` |
| Security | Both audits clean | 124 locked dependencies and 117 normal wheel dependencies, no known vulnerabilities; final wheel Requires-Dist unchanged from the audited metadata |
| Publisher/approval | Passed, protections retained | The release job waited with zero steps before required reviewer `a-adamovich` approved; `release.yml`, protected `pypi` environment and trusted publisher retained, no bypass or IAM changes |

Local Python 3.14 regression verification with the built wheel: 231 passed, three platform/opt-in skips. Ruff and clean dependency consistency checks pass. All four clean installed wheel variants pass locally.

## Publication and public verification

[Release run 38373244267](https://github.com/a-adamovich/dbt-dqm/actions/runs/38373244267) passed every step: version agreement, lint/tests/build/distribution inspection, full Postgres release lifecycle, provenance generation and verification, trusted PyPI publication and GitHub prerelease creation. The required environment approval is recorded under `a-adamovich`.

- [GitHub prerelease `v0.2.0a3`](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a3), published at `2026-10-13T09:04:35Z`.
- [PyPI `0.2.0a3`](https://pypi.org/project/dbt-dqm/0.2.0a3/), Python `>=3.11,<3.15`, extras exactly `bigquery` and `postgres`.
- Both downloaded distributions match the public PyPI SHA-256 hashes below. Wheel application modules and source-archive application/dbt files match the released revision; dependency metadata matches the audited candidate.
- Both artifacts independently pass `gh attestation verify`, constrained to this repository's `release.yml`, the exact merged source digest, `refs/tags/v0.2.0a3` and GitHub-hosted runners.
- All 16 fresh **PyPI** installations pass: base, BigQuery only, Postgres only and both on each of Python 3.11–3.14. Dependency consistency, adapter isolation, CLI/workspace/shared imports, absent-adapter hints and warehouse error/optional-health handling pass.
- A fresh consumer installs the dbt package from Git revision `v0.2.0a3` and parses successfully using the published Python 3.14 BigQuery installation. All 24 dbt model/macro/project files match the released revision, including the writer dependency chain.

| Published artifact | SHA-256 |
| --- | --- |
| `dbt_dqm-0.2.0a3-py3-none-any.whl` | `764494483cde792baf33c8947d81b1c52129ca2ca75362d1698a6f704c609a1f` |
| `dbt_dqm-0.2.0a3.tar.gz` | `8de2d8567b56bea56fb53cc841881aee3f7780022c540b592d79e0f42e2e098a` |

These publication-status additions change documentation only. They name the tested and released revisions rather than claiming that live warehouse suites ran on the later evidence commit.

## Scope of evidence

Native Windows CI verifies configuration, executable selection, spawn-process error transfer, profile/authentication libraries and wheel variants. It does not certify an individual corporate network, identity setup, UNC server or consumer project. Live BigQuery checks use existing service-account credentials; WIF retains offline loading/exchange/refresh coverage. No keys were created or downloaded and no shared QA dataset was reset.

An earlier live attempt encountered a confirmed BigQuery `backendError`/HTTP 500 during capture. It was stopped; no test was weakened or waived. The complete run above passed, including that case. Browser automation was synchronized with Streamlit rerenders; application errors were not suppressed.

Synthetic browser schemas, cached workspace and local servers were cleaned after verification. Acceptance dataset cleanup remains confined to labelled, uniquely named disposable datasets.
