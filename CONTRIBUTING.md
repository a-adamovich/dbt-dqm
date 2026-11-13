# Contributing

dbt-dqm accepts bug reports and focused pull requests. For substantial features, open an issue
first so the interface and adapter implications can be agreed before implementation.

Do not submit employer-owned code, proprietary data, credentials, customer names, or implementation
details copied from another project.

Keep changes small, add regression coverage, and run `ruff`, `pytest`, and relevant focused tests.
Every commit must include a Developer Certificate of Origin sign-off (`git commit -s`), certifying
that you have the right to contribute it under Apache-2.0. By submitting a contribution, you agree
that it is licensed under the repository's Apache-2.0 license.

Install development dependencies and both warehouse extras with
`uv sync --locked --all-groups --all-extras`. Run checks with
`uv run --group dbt1 --all-extras ruff check .` and `uv run --group dbt1 --all-extras pytest -q`.
For warehouse-specific work, use `--extra postgres` or `--extra bigquery` instead.

For macros, models, or execution changes, run the required Postgres core tier against a
disposable database: `DBT_DQM_TEST_DSN=… uv run --group dbt1 --extra postgres pytest -m core -n 4
integration_tests/acceptance/test_postgres.py`. It must select eight cases and pass without
skips. Existing test assertions must not be weakened to shorten CI.

PR CI provides the fast tier; full warehouse/platform matrices, browser QA and the 50,000-issue
memory/export check are run deliberately using **Full validation** in GitHub Actions. Do not
duplicate those long suites locally by default. Record a diagnosis before requesting a rerun.
[CI tiers and validation](docs/ci-measurements.md) explains the coverage and limits.
Coding agents share [AGENTS.md](AGENTS.md); `CLAUDE.md` imports the same instructions.
