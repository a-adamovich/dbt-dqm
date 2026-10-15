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

Production candidate: `120b24ff4c00ffe9c3aef9cc40efd1b6d3c1a169`. Subsequent documentation records
verification without changing production files. The installed-wheel Python suite passed 374 tests;
only the native Windows UNC case is unavailable on macOS and is covered by native CI. The
export-enabled 50,000-issue memory gate passed at 435.3 MiB peak RSS;
[raw result](scale-results/candidate-120b24f-cache-boundary.json). Complete CI and live warehouse
gates are recorded on the final linked release page.
