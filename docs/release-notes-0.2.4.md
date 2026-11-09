# dbt-dqm 0.2.4

This release fixes the Windows execution regressions reported in #97 and #98 and the dbt 2
BigQuery parser warnings in #99. Install matching Python and dbt packages at 0.2.4.

- Temporary projects use private copies when Windows denies symlink creation. DQM does not
  require administrator privileges or Developer Mode for its project projection.
- Compiled queries, collection results and output-column maps use temporary project variables
  rather than command-line payloads. Literal SQL is preserved. Oversized remaining user arguments
  produce a clear platform-limit error.
- BigQuery COLLATE function names are quoted, preserving their behavior while retaining strict
  static analysis. Analysis is not disabled for package models or their descendants.

Warehouse tables, issue identities and annotations are preserved. No reset or migration is needed.
Upgrade the Git revision in packages.yml, run `dbt deps`, and install `dbt-dqm[bigquery]==0.2.4`
or `dbt-dqm[postgres]==0.2.4` as appropriate. Keep the separately selected dbt engine.

Python support remains 3.11–3.14. Core 1.11–1.12 supports BigQuery and Postgres; dbt 2.0.11
supports BigQuery, with 2.0.9 retained in offline regression coverage. Windows regressions run
natively with real engines and forced privilege denial, including a runner child process.
Credentialed BigQuery evidence is produced locally on macOS.

## Validation and publication

Publication requires all platform/full-validation jobs, both fresh 33-case BigQuery suites and
the serial restricted-permissions test. BigQuery evidence for this release is maintainer-attested
local evidence. It does not certify live WIF or native Windows warehouse execution. Historical
0.2.3 reports cannot authorize this release. Evidence and retained diagnoses are attached to the
[release](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.4).

The exception is restricted to 0.2.4; protected PyPI approval, trusted publishing and provenance
verification remain required. Pinned dependency audits block on unaccepted findings or failed
assessment. The accepted sqlparse advisories apply only to the isolated Core 1.11.0 test engine;
app installations require patched dependencies. An unpublished root package may have no advisory
service record; that limitation is recorded separately from assessed third-party dependencies.

The configured profile query cap is not a whole-suite budget. dbt 2.0.11 ignored it on previously
tested paths; app search has a separate limit, and other app/permissions queries are not covered.
Available bytes/slot measurements exclude parent SCRIPT totals and may be incomplete.
