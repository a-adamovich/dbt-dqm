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
Postgres uses four workers. The full suite contains 41 cases after adding generic coverage.

The `validated` check requires all 27 matrix/QA jobs and nine warehouse evidence records from
the same run attempt and source tree. Each warehouse record includes the SHA, tree, matrix,
installed versions, dependency digest, timestamps, and JUnit counts. Failures, missing jobs,
unexpected skips, duplicate reports, or fewer than 41 passing warehouse cases fail validation.
Reports are retained for 30 days; this is retention, not permission to reuse them for release.

Stage 1 certifies **Postgres and platforms only**. A passing result does not replace the
credentialed BigQuery release gate. PyPI approval and provenance verification remain in place.
The maintainer must configure `required` as a required status and require up-to-date PRs;
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

BigQuery billing measurements will be added in Stage 2. Do not infer cost from fixture size or
label elapsed time as billed usage. Cloud metrics will separate labelled queries from unlabelled
app queries and exclude parent `SCRIPT` rows to avoid double-counting.
