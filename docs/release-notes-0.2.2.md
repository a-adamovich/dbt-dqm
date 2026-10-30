# dbt-dqm 0.2.2 candidate

This candidate adds a dbt 2.0.9 execution path for BigQuery while retaining dbt Core
1.11–1.12 on BigQuery and Postgres. **Credentialed BigQuery validation on both engine
generations is required before publication or a full v2 compatibility claim.** See the
[verification record](dbt-2-compatibility.md).

Install dbt separately. The Python package no longer pins dbt Core or installs dbt adapters;
`[bigquery]` and `[postgres]` install the review app's warehouse clients. Use
`--dbt-executable` or `DBT_DQM_DBT_EXECUTABLE` to choose an engine in another environment.
Both engine generations share a subprocess interface and JSON artifact handling.

Existing metadata-marked tests keep their configuration. The dedicated workflow selects
bounded, deterministic capture within the package's test materialization; ordinary dbt tests
continue through dbt's native implementation. Input state remains separate from output artifacts,
and required JSON evidence is enabled even when `DBT_WRITE_JSON=false` is inherited.

Use matching 0.2.2 Python and dbt packages. This change requires no warehouse migration or full
refresh; existing issues, identity hashes, annotations, and workspace identities are retained.
Python app support remains 3.11–3.14. Native dbt v2 Postgres support is outside this release.

For installation and upgrade instructions, see [operations](operations.md#installation-and-compatibility).
