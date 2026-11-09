# CI tiers and measurements

The development policy has three stages. Stage 1 provides fast required PR checks and manual
Postgres/platform validation. Stage 2 will add approved BigQuery smoke and full cloud checks;
Stage 3 will add evidence reuse and simplify publication. Stages 2 and 3 are not implemented
by the Stage 1 change.

## Automatic PR checks

Every PR, including drafts, and every push to `main` runs the fast tier. Superseded PR runs are
cancelled; `main` runs are not. There are twelve jobs including the final `required` check:

| Check | Matrix |
| --- | --- |
| Units, lint, consistency, app startup | Python 3.11–3.14 |
| Wheel variants, metadata, security floors | Python 3.11 and 3.14 |
| Portability and wheel variants | Windows/Python 3.11; macOS/Python 3.14 |
| Real dbt 2.0.11 and 2.0.9 parse/compile | Python 3.14, no warehouse credentials |
| BigQuery compile | Core 1.12.6, Python 3.14, no warehouse access |
| Postgres core | Core 1.12.6, Postgres 16, Python 3.14, four workers |

The core tier has exactly eight cases: lifecycle/history, collection reporting, native limits
and disabled commands, changed-evidence rejection, late/skipped evidence, rollback/empty mode,
reviewer-edit preservation, and generic/singular lifecycle. The generic fixture is disabled
unless the acceptance case opts in. The existing demo walkthrough is not repeated in this tier.

## Manual full validation

Run `Full validation` from GitHub Actions, selecting the candidate branch. All jobs check out
the dispatch event's immutable SHA, including reusable candidate QA. No schedule or automatic
push trigger starts the long matrix.

The workflow retains the complete Python 3.11–3.14/Postgres 14–16 acceptance matrix, the
Core 1.11.0 job, complete Windows/macOS and installed-wheel matrices, all BigQuery offline
compile combinations, walkthroughs, query-plan checks, browser QA, and memory/export QA.
Postgres uses four workers. The full suite contains 42 cases including generic lifecycle and
populated identity-migration coverage.

The `validated` check requires all 27 matrix/QA jobs and nine warehouse evidence records from
the same run attempt and source tree. Each warehouse record includes the SHA, tree, matrix,
installed versions, dependency digest, timestamps, and JUnit counts. Failures, missing jobs,
unexpected skips, duplicate reports, or fewer than 42 passing warehouse cases fail validation.
Reports are retained for 30 days; this is retention, not permission to reuse them for release.

Stage 1 certifies **Postgres and platforms only**. A passing result does not replace the
BigQuery release gate. Version 0.2.5 has a separately scoped, fresh local-evidence exception; older reports cannot satisfy it. The maintainer must configure `required` as a required status and require up-to-date PRs;
adding a workflow alone does not enforce branch protection.

## Measurement record

Targets are measurements to verify, not promises: Postgres core within ten minutes and fast
PR wall time within fifteen minutes. Keep all assertions if setup or execution needs tuning.
Each warehouse run writes JUnit and `--durations=25` output, installed versions, and JSON
evidence. Record suite, engine, worker count, elapsed time, result, and source revision below.

| Suite | Engine / warehouse | Workers | Duration | Result / revision |
| --- | --- | --- | --- | --- |
| New generic/singular lifecycle, local | Core 1.12.5 / Postgres 16 | 1 | 54.81 s | 1 passed; Stage 1 worktree |
| Required Postgres core, local Python 3.11 | Core 1.12.5 / Postgres 16 | 4 | 184.53 s | Exactly 8 passed, no skips; Stage 1 worktree |
| Real dbt 2 offline smoke, local Python 3.11 | dbt 2.0.9 / no warehouse access | 1 | 6.82 s | Exactly 11 passed, no skips; Stage 1 worktree |

The current local receipt helper measures billed bytes and slot milliseconds after each approved
suite, excluding parent `SCRIPT` rows. It separates labelled queries (including children) from
unlabelled queries referencing the test datasets; shared-QA activity may include other users.
Missing metadata permissions are reported as unavailable, never as zero usage. No GCP roles or
quotas are changed. Do not infer cost from fixture size or elapsed time.

Acceptance fixture clients and dbt profiles request a per-query limit of 1 GiB by default,
configurable with `DBT_DQM_BIGQUERY_MAX_BYTES_BILLED`. Real 2.0.9 and 2.0.11 offline checks
accept the profile/label configuration. dbt 2.0.11 did not enforce this profile limit on previously tested paths. Treat it as configured, not guaranteed; record fresh observations during live validation.
App search retains its own configured limit; other app queries and the restricted-permissions
fixture are not covered by this test cap. This is not a total-suite spending ceiling.

## 0.2.4 development measurements

| Check | Environment | Result | Duration |
| --- | --- | --- | --- |
| Postgres core | Core 1.12.6 / disposable Postgres 16, Python 3.11, four workers | 8 passed | 187.99 s |
| Required CI Postgres core | Core 1.12.6 / Postgres 16, Python 3.14, run 42539282721 | 8 passed | 7m58s job time |
| Native Windows portability | Windows / Python 3.11, same run | Passed, including real engines and startup-injected symlink denial | 4m00s job time |

The first local Postgres attempt failed because pgcrypto was missing. Its output is retained;
the extension was installed in the disposable database before the successful rerun.
Development checks are not final-candidate BigQuery runtime evidence.

## 0.2.5 development checks (before candidate freeze)

These checks guide development; they are not frozen-candidate BigQuery release evidence.

| Check | Engine / location | Result | Duration / source |
| --- | --- | --- | --- |
| Postgres core, four workers | Core 1.12.6 / disposable local Postgres 16 | Exactly 8 passed | 186.61 s; initial #105 development worktree |
| Populated identity migration, damaged-name refusal and retry | Core 1.12.6 / disposable local Postgres 16 | 1 passed | 30.02 s; worktree committed as `ba5664a` |
| Migration plus actual payload-event behavior | Core 1.12.6 / disposable local Postgres 16 | 2 passed | 68.66 s; #105 development worktree |
| Real dbt 2 offline checks | dbt 2.0.11 / local, no warehouse queries | 17 passed | 16.41 s; #105 development worktree |
| Complete release-preparation units | Python 3.11 / local | 662 passed, 25 conditional skips | 62.91 s; 0.2.5 preparation worktree |

The release additionally requires a separate serial `bigquery-upgrade` receipt. It reproduces the
0.2.4 defect on pinned historical source, migrates and repairs that disposable installation,
and exercises both engines against it. Both engine inventories and dependency digests are
required. Its execution and the two full warehouse suites remain subject to the candidate/cost
checkpoint; ordinary offline compilation does not certify this recovery path.
