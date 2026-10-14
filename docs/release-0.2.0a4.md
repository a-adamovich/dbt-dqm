# 0.2.0a4 release evidence

Published 2026-10-14 UTC through the existing protected reviewer, trusted publisher and provenance workflow.
Python/application/lock version: `0.2.0a4`; dbt version: `0.2.0-a4`; released tag: `v0.2.0a4`.
Baseline: `13bf825900ceee330639c773474288db70a10bf8`. Branch: `codex/dqm-workflow-alpha4`.
Production candidate: `608ce231f12a0179eeae6d5339b43a42bd6522c4`.
Preparation: [PR #65](https://github.com/a-adamovich/dbt-dqm/pull/65), merged as
`6124027b4862b02d31ec64bf329fb897bbcf83a6`. Its full tree matches final PR head
`8ed5f789c216bd7fb0fba989cbbf8fa277a3bdbd`; only documentation differs from the production candidate.
Later evidence additions change documentation only and name this tested production revision.

This alpha introduces a breaking warehouse contract (`dqm-full-v1`). There is no history migration.
The maintainer authorized implementation, merge and publication after all gates pass, and explicitly
authorized a DQM full refresh in `dbt-dqm.dbt_dqm_qa` for the serial permissions gate. Only known
DQM-owned tables and manifest-resolved package relations may be reset. Preserve unrelated QA
relations, dataset access, identities, keys and IAM permissions. No new keys or IAM grants are authorized.

Required evidence on the production candidate:

| Gate | Status |
| --- | --- |
| Current CI, optional extras/Python 3.11–3.14 and native Windows/macOS/Linux checks | 28/28 passed in [run 38489648216](https://github.com/a-adamovich/dbt-dqm/actions/runs/38489648216) |
| Full PostgreSQL 14/16 acceptance | 36/36 passed on Postgres 14/16 across all four supported Python minors in CI, no skips; local Postgres 16 passed 36/36 in 1,030.58 s |
| Complete credentialed BigQuery acceptance, disposable datasets | 27/27 passed in one complete run, 2,168.50 s |
| Serial restricted runner/reviewer gate after the authorized scoped QA reset | Passed, 339.16 s; independently verified runner/reviewer identities, capture, retention, review writes/retries and denied privilege probes |
| Real browser stable ordering during edits/Apply, both browsing modes, paging/filter/offline recovery | Passed on the candidate installed wheel and dbt package, including actual changed status/owner values, notes, Apply, paging, Health, business-tag options, locked-write pending preservation/retry and offline recovery |
| Cache-boundary memory | Passed: 50,000 issues, 408-byte average payload, 317.9 MiB peak RSS, 5.76 s reads, 15.61 s total; [raw result](scale-results/candidate-608ce23-cache-boundary.json) |
| Security audits and trusted publisher/environment verification | Locked and freshly installed wheel dependency audits clean; no open Dependabot alerts. Existing required reviewer `a-adamovich`, no admin bypass and `v*` deployment policy retained; trusted publication and protected approval passed |
| Capture benchmark: 15 tests, 264 raw failed rows, timing and statement counts | Passed: 264 captured issues; 129.151 s capture, 308.523 s workflow; 3 capture SQL calls, 77 child statements; [raw result](scale-results/candidate-608ce23-capture-benchmark.json) |
| Candidate review, exact tested revision and merge content verification | Passed: final PR head `8ed5f78`, 28/28 in [run 38494519195](https://github.com/a-adamovich/dbt-dqm/actions/runs/38494519195); merged tree matches exactly; production equals `608ce23`, DCO signoff retained |
| Protected approval, trusted publication and provenance verification | Passed in [release run 38498948777](https://github.com/a-adamovich/dbt-dqm/actions/runs/38498948777); required reviewer `a-adamovich` approved after the zero-step protected pause; no bypass or protection changes |
| Public hashes/metadata, all 16 PyPI combinations and Git-tag dbt installation | Passed: both public artifacts/source/metadata/provenance verified; base/BigQuery/Postgres/both on Python 3.11–3.14; Git-tag disabled/enabled parse and offline compilation |

Automated native Windows coverage must be distinguished from corporate-environment verification:
no new corporate Windows consumer verification has been claimed for this alpha.

The serial restricted run deliberately removes development ADC credentials and uses only the
independently loaded runner/reviewer credentials. Its global disposable-dataset cleanup fixture
reports no ADC credentials; no shared QA dataset cleanup is attempted. The uniquely named
permission fixtures remain in the authorized QA dataset.

## Completed candidate checks

The final wheel on Python 3.14.6 passed 250 unit/UI tests in 84.49 s. Eight skips are native
Windows, warehouse opt-in, or built-wheel metadata checks executed in their separate gates.
Wheel and source-archive production files match the candidate; version and optional-extra
metadata agree. Both distributions pass Twine checks. All security floors and optional-adapter
installation combinations remain covered in CI.

The maintainer-authorized QA reset completed through `dbt-dqm run --full-refresh`, using the
existing development credentials. Read-only inventories before and afterward verify that all
64 pre-existing non-reset relations retain their schemas, row counts, modification times and
view definitions. Dataset access and access policies for all 22 pre-existing DQM relations
are unchanged, including the four reviewer table grants. The only added relation is the new
reconciliation stage, verified against the run registry. No dataset, identity, key or IAM grant
was created or changed. This reset does not replace the separate restricted-account gate.

A further read-only inventory after the restricted gate confirms preservation of the same
64 original unrelated relations, all 22 existing DQM relation access policies and dataset access.
The newly named permission fixtures and registered DQM stages are separate from those originals.

The capture benchmark retains 15 separate `BEGIN`/`COMMIT` pairs. The 77 child statements
also include 15 temporary-table constructions, 15 updates, 16 merges and one summary query.
Three capture SQL calls include shared metadata retrieval and the capture script. This synthetic
measurement does not establish a speedup over the consumer's reported 124-second run.

## Scope and interrupted attempts

Live mechanics checks use the existing development identity and labelled disposable datasets.
The serial permissions gate uses independently loaded runner and reviewer credentials and
verifies both identities with `SESSION_USER()`. No corporate Windows verification is inferred
from service-account or automated native-platform checks.

Earlier candidates exposed an extra BigQuery statement delimiter and redundant staging-table
metadata updates exceeding BigQuery's update-rate quota; both were corrected. Candidate
`82c4e1a` subsequently passed all 28 CI jobs and one complete credentialed BigQuery suite
(27/27, 2,110.02 s), plus the 15-test/264-row capture benchmark (120.444 s capture,
77 child statements). Its serial restricted-account gate exposed an unconditional dataset
creation request even when the dataset existed. The current candidate checks existence first;
no additional IAM permissions were granted. A focused restricted-account initialization,
package-model and maintenance run passed before renewing the full gates.

Earlier interrupted HTTPS-read attempts and a 26-pass/one-timeout BigQuery run are excluded
from complete acceptance and performance evidence. No failed or interrupted warehouse run
is counted as a complete passing gate.

## Publication and public verification

[Release run 38498948777](https://github.com/a-adamovich/dbt-dqm/actions/runs/38498948777)
passed every step: version agreement, lint/tests/build/distribution checks, full Postgres release
lifecycle, provenance generation and verification, trusted publication and GitHub prerelease creation.

- [GitHub prerelease `v0.2.0a4`](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a4), published at `2026-10-14T02:06:31Z`.
- [PyPI `0.2.0a4`](https://pypi.org/project/dbt-dqm/0.2.0a4/): Python `>=3.11,<3.15`, extras exactly `bigquery` and `postgres`.
- Downloaded PyPI distributions match the published hashes below and GitHub assets byte-for-byte. All 14 wheel app modules and source-archive app/model/macro/project files match released revision `6124027`; dependency metadata matches the audited candidate.
- Both public artifacts independently pass provenance verification constrained to this repository's `release.yml`, exact merged source digest, `refs/tags/v0.2.0a4` and GitHub-hosted runners.
- All 16 fresh PyPI installations pass: base, BigQuery only, Postgres only and both on Python 3.11–3.14. Dependency consistency, adapter isolation, CLI/workspace/shared imports, absent-adapter hints and warehouse error/optional-health behavior pass.
- A fresh consumer installs the dbt package from Git tag `v0.2.0a4`, verifies all 27 released model/macro/project files, parses with DQM disabled and enabled, validates the writer dependency chain, and compiles offline using the published Python 3.14 BigQuery installation.

| Published artifact | SHA-256 |
| --- | --- |
| `dbt_dqm-0.2.0a4-py3-none-any.whl` | `110fe10e5a796eb32b1bbff7967e435de0ebf353584fbb0b49dd6b99c3e11a29` |
| `dbt_dqm-0.2.0a4.tar.gz` | `1b27570d23f3d836e950efa8f5716db1bd2a93ea0764f343924431bd936641ad` |

These publication-status additions change documentation only. They identify the tested and
released revisions rather than claiming that live warehouse suites ran on a later evidence commit.
