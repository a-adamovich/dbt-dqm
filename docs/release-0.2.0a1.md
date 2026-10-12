# 0.2.0a1 release candidate evidence

Recorded 2026-10-11. Every gate in the [release checklist](release-checklist.md) passed on the
production candidate below. This is evidence for tagging, not proof that publication completed.

- Baseline: `82a2917f2fd91bf64e00eadee4ec869b4a393ea9`.
- Production candidate: `69703f710a323cec075a0dc67f6d7d8bd0543d1f`.
- Preparation branch: `codex/release-0.2.0a1`, [PR #60](https://github.com/a-adamovich/dbt-dqm/pull/60).
- [Candidate CI run 38189103369](https://github.com/a-adamovich/dbt-dqm/actions/runs/38189103369): 5/5 jobs passed.

A later documentation-only commit adds this record and the memory results; the warehouse suites
ran on the candidate, not on that commit.

## Changes since the first preparation candidate

The first candidate, `d0385e6`, was blocked and its results are superseded.

- **dbt version spelling.** dbt-core's project schema accepts only SemVer and rejects
  `version: 0.2.0a1`. With the maintainer's approval, `dbt_project.yml` declares `0.2.0-a1`, while
  the Python package and tag stay `0.2.0a1`. The release check accepts exactly that SemVer
  spelling of the Python version and nothing else.
- **GitHub release labelling.** The release step previously marked every tag as an alpha, with
  hard-coded `0.2.0a1` notes. It now creates a pre-release, with notes derived from the tag, only
  for `a`/`b`/`rc` tags.
- **`dbt_dqm_schema` was silently ignored (found during consumer QA).** The var was read in the
  package's own `dbt_project.yml`, which dbt renders without consumer or CLI vars
  ("Vars supplied to <Configuration> = {}"). Every table therefore landed in the target schema,
  and 0.1 users had no working way to follow the fresh-schema instruction. The defect predates
  0.2: it reproduces on dbt 1.11.15 and 1.12.5. Each package model now sets the schema itself. A
  compile-only unit test covers it; it fails without the fix.

## Results for the candidate

| # | Gate | Result | Evidence / limitation |
| --- | --- | --- | --- |
| 1 | CI | Passed 5/5 | Lint and unit, package smoke, BigQuery compile, Postgres 14 and 16 acceptance ([run 38189103369](https://github.com/a-adamovich/dbt-dqm/actions/runs/38189103369)) |
| — | Unit suite, local | 194 passed, 3 skipped | The skips are the opt-in live browsing-equivalence tests and the CI-supplied wheel-metadata check |
| 2 | Postgres 16.11 acceptance | 32/32 (1,037 s) | Local, from a clean clone at the candidate |
| 2 | Postgres 14.20 acceptance | 31/32 in the full run (1,364 s); the remaining test passed alone (25 s) | `test_reconciliation_serializes_and_reviewer_edit_survives` hit its 90 s subprocess timeout in the *initial* `dbt build`, while 6 BigQuery workers ran concurrently on the same machine. It passed alone locally and in CI on both servers. |
| 3 | Credentialed BigQuery acceptance | 25/25 in one run, 6 workers, no skips (1,913 s) | Project `dbt-dqm`, labelled disposable datasets, existing local service-account file |
| 4 | Restricted runner/reviewer permissions | Passed (352 s) | Run serially after gate 3 against `dbt_dqm_qa`. The test checks each identity with `SESSION_USER()` before acting. Shared QA history and IAM unchanged. |
| 5 | Browser QA | Passed | See below |
| 6 | 50,000-issue memory probe | Peak RSS 583.1 / 583.8 / 583.9 MiB; 4.4–4.7 s per sync | Inside the documented 435–585 MiB range and consistent with the recorded PyArrow 25 runs (577–585 MiB). [`candidate-69703f7-cache-boundary.json`](scale-results/candidate-69703f7-cache-boundary.json) |
| 7 | Versions agree | `0.2.0a1` (Python, `__version__`, lockfile), `0.2.0-a1` (dbt) | The release step script runs in unit tests against matching, mismatched and wrongly spelled versions |
| 8 | Dependabot | No open alerts | Rechecked 2026-10-11; `pip-audit` of the clean wheel install reports no known vulnerabilities |
| 9 | Trusted publisher | Confirmed by the maintainer | `pypi` required-reviewer and `v*` tag protection inspected; nothing changed |
| 10 | Release approval pause | Not yet observed | Observed when the tag is pushed |

### Browser QA

`dbt-dqm app` from the clean wheel install, against a Postgres 16 consumer with 208 Active issues
in `dbt_dqm_schema: dqm_alpha` (the app showed `alpha_consumer_dqm_alpha`).

- **Snapshot mode (`auto`, under the cache limits):** Sync loaded 208 issues. A notes edit was
  applied (batch `4d34bc23…`) and survived the following sync. With
  `dqm_issue_occurrences` held in `ACCESS EXCLUSIVE` mode, Apply showed the "reconciliation is
  running … edits are kept locally" message and kept the edit pending. The retry after release
  applied it (batch `3bdff810…`). Health showed test and area health, and Maintenance showed all
  four steps `OK` once retention was configured.
- **Warehouse mode (`--browse warehouse`):** Next/Previous paging across pages 1–3. An edit made on
  page 3 was applied (batch `f48d75cd…`) and the app stayed on page 3. A 2-character search was
  refused ("Search needs 3 to 200 characters") with the previous results kept. A search for
  `missing_region` returned the expected 58 of 208. The "Recurrences only" filter returned 0, as
  expected for first-time issues, and clearing it restored the results.
- **Offline:** with the Postgres server stopped, paging showed the "Offline: showing recently
  loaded issues only" view with "Retry the warehouse". An edit made offline stayed pending with
  Apply disabled. After the server restarted, Retry restored browsing and Apply saved it (batch
  `e8da8db4…`). All four edits were confirmed in the warehouse.
- BigQuery's dry-run byte-ceiling search refusal was not exercised in the browser. It is covered
  by the BigQuery acceptance suite.

### Distribution and installation

| Check | Result |
| --- | --- |
| Build | One wheel (16 files) and one source archive (190 files); both pass `twine check` |
| Inventory | No private keys, `.env`, local `profiles.yml`, ADC files, SQLite caches, bytecode, targets, logs, installed packages or iCloud duplicate files |
| Clean Python 3.11 install | Outside the checkout, without the lockfile: 118 compatible packages, `import dbt_dqm_app` reports `0.2.0a1`, the CLI and app start |
| dbt consumer from Git | `dbt deps` installed the candidate commit. The installed package declares `0.2.0-a1`. `parse`, `seed`, `run --select package:dbt_dqm`, `test --select tag:dqm` (WARN 1) and `build --select package:dbt_dqm` all exit 0. All 21 `dqm_` relations are in `alpha_consumer_dqm_alpha`. After the bad row was fixed, the issue became Archived with a `DISAPPEARED` event. |

| Artifact | SHA-256 (local candidate build) |
| --- | --- |
| `dbt_dqm-0.2.0a1-py3-none-any.whl` | `d659964f9441641a2d261ba416225cc62f087fa43089e657b90c9ed57c71384e` |
| `dbt_dqm-0.2.0a1.tar.gz` | `6c8b6a5bf2a32e22614527dab448f341c7752dbc0b946daffd07a089771d8523` |

The release workflow rebuilds and attests its own artifacts; these hashes identify the local QA
build only. The wheel is byte-identical to the first candidate's, because the fixes touched only
dbt package files.

### Environment note

The working checkout lives in an iCloud-synced folder. iCloud restored thousands of `* 2.*`
duplicates into its `.venv`, including `macros/relations 2.sql` in dbt-postgres. These caused
spurious `DuplicateMacroInPackageError` failures in an earlier local run. Every warehouse suite
above was rerun from a clean clone outside iCloud. The wheel environment, the consumers and the
browser QA ran outside iCloud from the start.

## WIF

`tests/test_wif.py` passes 7/7, offline, through the real app and dbt ADC loaders with simulated
token exchange and impersonation. Live WIF token exchange and IAM are not verified; no WIF
infrastructure was provisioned.

## Publication and Hub status

At the time of recording, Python `0.2.0a1` was not on PyPI and the `v0.2.0a1` tag did not exist.
Hub registration follows publication. [Hub's parser](https://github.com/dbt-labs/hubcap/blob/main/hubcap/version.py)
rejects the Python-style `v0.2.0a1` tag, so registration does not make this alpha installable
through Hub. Install it through Git.
