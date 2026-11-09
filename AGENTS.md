# Development and validation policy

These instructions apply to Codex, Claude, and other coding agents. Explicit maintainer
instructions take precedence. Preserve existing assertions and acceptance cases.

## While developing

- Read the affected implementation and tests first. Run Ruff and relevant unit tests while
  iterating; run the complete unit suite before pushing a coherent batch of changes.
- Use draft PRs while iterating. Mark finished work ready for review. The fast `required` CI
  tier runs on drafts too; read its logs before changing code or requesting a rerun.
- Changes to macros, models, or dbt execution should also run the Postgres core tier once when
  Docker is available: `pytest -m core -n 4 integration_tests/acceptance/test_postgres.py` with
  `DBT_DQM_TEST_DSN` pointing only to a disposable database. To diagnose a failure, select the
  failing test. Do not run full acceptance suites locally by default.
- Before starting a long validation, inspect existing queued, active, and successful GitHub
  runs for the candidate revision. Do not dispatch duplicate full suites.

## CI tiers and evidence

- PR CI checks units on Python 3.11–3.14, endpoint Python wheel installations, representative
  Windows/macOS portability, offline BigQuery compilation, the real dbt 2 executable, and
  eight real Postgres core cases. All required jobs must succeed.
- Full compatibility matrices, the demo walkthroughs, installed-wheel browser QA, and the
  50,000-issue memory/export check belong to manual full validation.
- Stage 1 full validation covers Postgres and platforms only. It cannot certify BigQuery or
  authorize publication. The existing credentialed release gate stays in force.
- Later stages add approved BigQuery PR smoke and complete release validation, then validated
  result reuse. Do not report those stages as available before implementation and verification.
- Report the command, location, candidate SHA, engine, selected test count, result, and duration.
  Compile-only or mocked checks are not warehouse runtime evidence.

## Approvals and failures

- Agents may queue the agreed checks for a ready PR and dispatch validation for a maintainer-
  designated release candidate. Human approval of cloud access remains separate. This policy
  does not authorize agents to approve environments or call the pending-deployment approval API.
- Credentialed local BigQuery runs, tags/publication, IAM or key changes, shared-QA writes, and
  CI control-file changes require explicit maintainer authorization for the action. CI control
  files include workflows, gates, allowlists, core selections, and evidence minimums.
- Read the failure output and document a diagnosis before a manual rerun. Do not automatically
  retry until green. Repeated infrastructure failures still require evidence and explanation.
- Never weaken assertions, add skips/xfails, or alter timeouts to obtain a passing result.
  A measured, justified timeout or test-architecture change requires review.
- Do not reset shared QA, introduce schedules, create service-account keys, or change project
  quotas as part of routine test execution.

## Temporary 0.2.4 release policy

- Only 0.2.4 may use fresh local BigQuery attestation. Never reuse historical warehouse reports.
- Freeze all tracked files before expensive validation. Require complete GitHub platform evidence,
  both 33-case BigQuery suites and the serial restricted-permissions test on that source tree.
- Present the agreed candidate/cost/redaction checkpoint before credentialed execution. No new
  GCP setup, IAM, keys, quotas, schedules or shared-QA resets are authorized.
- Keep private originals outside Git and retain failed attempts and diagnoses. Public copies
  redact secrets, local paths, account emails and project/dataset names.
- Verify-only execution cannot publish or request environment approval. Synthetic fixtures are
  not runtime evidence. Use numeric draft/asset IDs and verify bundle digests.
- Preserve immutable tags, release-specific maintainer attestation and human PyPI approval.
  Open a removal issue after shipping. Later versions require the credentialed GitHub gate.
