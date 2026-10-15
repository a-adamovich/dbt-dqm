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

Production candidate: `87152709a085a6ff683a8774aa2fa6b1e18041cc`. Subsequent documentation records
verification without changing its production files. The export-enabled 50,000-issue memory gate
passed at 437.2 MiB peak RSS; [raw result](scale-results/candidate-8715270-cache-boundary.json).
CI and complete live warehouse gates are recorded on the final linked release page.
