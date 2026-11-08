# dbt-dqm 0.2.3

This release delivers dbt 2.0.11 support on BigQuery and retains Core 1.11–1.12 support
on BigQuery and Postgres, with Python 3.11–3.14. Install dbt separately and use matching
0.2.3 Python and dbt packages. The app extras install warehouse clients rather than engines.
There is no warehouse migration or reset.

The 0.2.2 tag was never published to PyPI: draft-evidence retrieval failed before publishing.
Version 0.2.3 corrects release delivery while preserving the validated runtime and dependencies.
Its warehouse evidence is maintainer-attested local validation: 33 passing dbt 2.0.11 cases,
33 passing Core 1.12.6 cases and one serial runner/reviewer permissions case. Original reports
retain their 0.2.2 identities, and an explicit reviewed source/payload comparison binds them
to this release. Fresh CI, packaging and audits are required. This does not certify WIF.

The dedicated workflow disables execution reuse and test batching to obtain fresh evidence.
Ordinary dbt commands retain native behavior. dbt 2.0.11 did not apply the profile's query-byte
limit in tested test/run-operation paths; do not assume it is a hard spending control.

Earlier fixture failures and their diagnoses are preserved alongside the passing reports.
The accepted sqlparse advisory exception applies solely to the isolated Core 1.11 test engine;
the app and current engine retain patched dependencies. Cost visibility is incomplete.
The temporary publication/evidence-reuse exception applies only to 0.2.3 and will be tracked
for removal immediately after shipping. Human PyPI approval and trusted provenance remain required.
