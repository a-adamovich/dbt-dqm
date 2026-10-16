# 0.2.0a8 release verification

This release adds reconciliation changes by saved owner to `run --summary`, sorts quiet-mode
results by status and test name, and reports collections reaching their native limit. Python,
application and lock version: `0.2.0a8`; dbt package: `0.2.0-a8`.

No warehouse schema migration or history reset is required when upgrading from a7. Existing issue
identity, lifecycle rules, reviewer annotations, namespace isolation and export behavior remain.
Upgrade the Python app and Git package together, run `dbt deps`, and restart the review app.

All current [release checklist](release-checklist.md) gates apply to one frozen candidate.
The release page below records the exact source and CI revisions, complete BigQuery acceptance,
serial restricted runner/reviewer verification, export-enabled 50,000-issue memory check,
security audits, protected approval, provenance, public hashes and installation matrix.
The app and runtime dependencies are unchanged from a7, so the checklist's conditional browser
verification gate is not required; Python/UI tests and native-platform CI still run.

Final verification and publication evidence:
[v0.2.0a8 release page](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a8).
