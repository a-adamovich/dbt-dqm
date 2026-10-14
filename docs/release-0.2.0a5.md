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

## Remaining verification and performance gate

No ADC credentials or BigQuery test project were configured for this session. Credentialed
BigQuery acceptance and live performance comparisons have **not** run. Native Windows/Linux and
the other supported Python versions remain CI/release gates; local macOS results do not establish
corporate Windows verification.

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
