# Python application quality baseline

Measured during 0.2.4 preparation on the runtime source at `498d38a`, merged in PR #100.
The later release preparation changes version metadata, verification and documentation.
This is a development baseline, not final-candidate warehouse runtime evidence.

| Measurement | Result |
| --- | --- |
| Statements | 2,971 / 3,550 covered (83.7%) |
| Branches | 923 / 1,248 covered (74.0%) |
| Combined coverage | 81.2% |
| Unit run under coverage | 605 passed, 24 conditional skips; Python 3.11.15 |
| Tools | coverage 7.17.2; Ruff 0.17.3 |

Coverage includes `src/dbt_dqm_app` and excludes SQL and child-process execution. The native
engine/Windows regressions and warehouse acceptance suites provide separate evidence.
Full per-file coverage and all 231 per-function complexity measurements are in the
[machine-readable baseline](quality-baseline.json).

The largest decision paths remain `DbtCLI.invoke` (43), `Workflow._execute` (37), `run_app`
(32), `_render_warehouse_issues` (31), and `_render_review_tools` (19). Prioritize focused
failure-path tests and responsibility extraction when changing these functions. This release
records the baseline; it does not introduce an arbitrary percentage gate or a type checker.

To repeat, install the development environment, add `coverage==7.17.2` to that environment,
and run its Python with `-m coverage run --branch --source=src/dbt_dqm_app -m pytest -q`.
Run `-m coverage report` or `-m coverage json` with the same interpreter. Keep generated reports
outside tracked source when validating a frozen candidate. Ruff's C901 rule with
`lint.mccabe.max-complexity=0` reports every function's measured complexity.
