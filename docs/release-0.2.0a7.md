# 0.2.0a7 release verification

This source implements relation isolation, optional NEW/ACTIVE owner workload reporting, ordered
CSV/Excel exports with timezone labels, and quiet-mode test deduplication. Python/application/lock
version: `0.2.0a7`; dbt package: `0.2.0-a7`. The warehouse row schema remains `dqm-full-v1`.

Unchanged destinations retain history. A corrected prefixed namespace requires an explicit,
confirmed fresh start; legacy shared tables are preserved and history is not copied. Upgrade both
the app and Git package and preserve pending edits before switching namespaces.

All release-checklist gates must pass on one production candidate before tagging. The preparation
PR and [v0.2.0a7 release page](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a7) record the
exact source revisions, CI and warehouse counts, restricted identities, browser/memory checks,
protected approval, trusted publishing, artifact hashes/provenance and public installation matrix.
This is a preparation record; final publication evidence is recorded on that release page.

Naming discovery uses private artifacts so concurrent reviewer requests cannot overwrite the
consumer manifest or partial-parse cache. It opens no warehouse connection. Final source, complete
CI/live warehouse results and installed-wheel browser/memory evidence are recorded on the linked
release page after verification of the corrected candidate.
