# 0.2.0a5 preparation evidence

This is an **unreleased** compatible follow-up to 0.2.0a4. Python/application/lock version:
`0.2.0a5`; dbt package version: `0.2.0-a5`. No tag or publication was created. The warehouse
contract remains `dqm-full-v1`, with the existing migration registry; upgrading from a4 does not
require a history reset.

## Verified locally

Checks ran on macOS with Python 3.11.15, dbt-core 1.12.5, dbt-bigquery 1.12.1, and
Postgres 16.11. These results describe the working source, not a published revision.

| Check | Result |
| --- | --- |
| Python/UI suite with built-wheel metadata checks enabled | 301 passed; 3 skips: native Windows UNC handling and two warehouse equivalence checks requiring configured environment variables |
| Full disposable-database Postgres acceptance suite | 37 passed in 1,533.35 seconds |
| Focused final Postgres safeguards | 2 passed: empty selection preserves active issues; invalid inherited owner lists stop before refresh; deferral, favor-state, custom artifact paths and reviewer ownership persist |
| Offline BigQuery protocol tests | 24 passed, including protected eligibility reads, unknown-result fallback, manual cleanup, custom dispatch, cutoff fencing, empty outcome attribution, and job dispatch |
| Initialized collection lookup regression | Passed; relation lookup is rejected by the test to prove setup is not evaluated during initialized collection |
| Wheel installations | Base, BigQuery, Postgres and both adapters passed in four fresh temporary environments using cached dependencies |
| Distribution build, dependency lock and lint | Wheel/source archive built; offline lock check, Ruff and whitespace checks passed |
| Test resource cleanup | All disposable databases created for these checks were removed |

Initial local attempts exposed environment setup problems (a shell-selected older Python and an
ASCII-encoded disposable database). The passing results above use the project interpreter and a
new UTF-8 database; failed/interrupted attempts are excluded.

## Release candidate verification and performance gate

The maintainer requested release on 2026-10-14. Production candidate:
`c9fa528e668d535b67e9c447d8f550c3d2c1ac91`, based on published main `4a8eb90`.
Preparation: [PR #67](https://github.com/a-adamovich/dbt-dqm/pull/67).
Release checks use the candidate's built wheel and dbt package on Python 3.14.6.
Existing development, runner and reviewer credential files are available and are being used;
no new keys, IAM changes or shared QA reset are needed. The permissions gate follows complete
BigQuery acceptance serially.

| Candidate gate | Status |
| --- | --- |
| CI / Postgres 14 and 16 across Python 3.11–3.14 | In progress in [run 38515826514](https://github.com/a-adamovich/dbt-dqm/actions/runs/38515826514); 20 completed non-Postgres jobs passed, including native Windows/macOS |
| Python 3.14 candidate tests with built-wheel metadata | 302 passed, 3 optional environment/native skips; one third-party `pytimeparse` deprecation warning |
| Full credentialed BigQuery acceptance | In progress; 28 collected tests |
| Serial restricted runner/reviewer gate | Waits for complete BigQuery acceptance |
| Real browser QA, installed wheel | Passed: both browsing modes, actual status/owner/note edits and Apply, stable order, paging, Health, configured maintenance outcomes, held-lock pending preservation/retry, filters, offline recovery; actual BigQuery dry-run search refusal verified read-only against QA views |
| 50,000-issue cache boundary | Passed: 314.4 MiB peak RSS, 2.66 s warehouse reads, 9.54 s total, 408-byte mean payload; [raw result](scale-results/candidate-c9fa528-cache-boundary.json) |
| Security | No open Dependabot alerts; 117 locked runtime dependencies and 117 fresh-wheel dependencies audited, zero known vulnerabilities |
| Distributions / lock | Candidate wheel and source archive built; Twine and offline lock agreement passed |
| Publisher and protected approval | Existing `release.yml`/`pypi` trusted publishing retained; required reviewer `a-adamovich`, no admin bypass, `v*` tag policy confirmed |

CI results provide native platform verification, without claiming a new corporate Windows trial.
Live performance comparisons have not run; the optimization remains disabled by default.

`dbt_dqm_maintenance_shortcuts` defaults to **false**. Offline executor tests verify fewer dispatched
jobs for proven-empty default steps, but do not establish a warehouse runtime improvement. Raw
retention always advances its cutoff and generation fence; active inputs and live stages remain
protected. Manual/custom steps retain their execution behavior, and uncertain eligibility executes
normally. Enable the optimization by default only after credentialed acceptance and matched
benchmark evidence show a benefit without correctness regressions.

The expanded benchmark uses 15 tests/264 issues in four scenarios: fresh installation, full refresh,
unchanged runs and retention backlog. It defaults to three trials per scenario, compares candidate
normal/opt-in modes, saves phase/job/statement medians and individual observations, and optionally
includes a separately installed a4 interpreter and a4 dbt-package checkout. Artifacts and routing
settings are isolated per trial, credentials are inherited, and datasets are disposable and labelled.

```bash
# Configure DBT_DQM_BIGQUERY_TEST_PROJECT and ADC first.
python integration_tests/acceptance/benchmark_capture.py \
  --output /tmp/dqm-a5-benchmark.json --repeats 3 \
  --baseline-python /path/to/a4-environment/bin/python \
  --baseline-package /path/to/v0.2.0a4-checkout
```

A credentialed acceptance case additionally checks empty-job skipping, attributed Health,
retention fencing, and joined metadata owners using the package defaults. It remains unverified
until the BigQuery gate runs. No percentage speedup is claimed.
