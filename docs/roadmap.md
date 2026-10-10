# dbt-dqm roadmap

This is the current roadmap. The older [project review](project-review.md) is a dated historical
assessment and intentionally keeps its original findings.

## Implemented on main

- Collection errors, invalid grains, missing columns, and inconclusive statuses cannot become
  lifecycle evidence. Grain validation covers passing and failing tracked tests.
- Identity uses the adapter-independent `dqm-id-v1` byte contract with golden SHA-256 vectors.
  `identity_scheme_signature` combines the algorithm version and ordered grain, so future identity
  changes archive honestly as `IDENTITY_CHANGED`.
- Reconciliation freezes the exact unprocessed conclusive executions, rejects late evidence, and
  applies occurrences, events, receipts and state atomically under a generation fence, retaining
  fail/pass and fail/pass/fail episodes between builds. On Postgres the reconcile view is replaced
  in place after the lock is taken (#17).
- Capture supports `identity_only` (default), allowlisted context, and explicit full-row modes.
  Retention variables are validated, migrations are recorded and verified, and Postgres
  query-support indexes are installed.
- Optional maintenance (retention, BigQuery staging expiry and stage drops, log pruning) runs only
  after capture or reconciliation, never fails the dbt run on a SQL error, logs sanitized and
  attributed outcomes, and reports per-step health (#13).
- Local workspaces are owner-only and can be inspected or purged. Drift requires an explicit
  discard/overwrite choice; warehouse writes use annotation compare-and-set versions.
- The local app supports BigQuery and Postgres, renders `env_var()` profile values, and discovers
  package relations from dbt's manifest so consumer schema-generation overrides are honored.
- `workflow_status` is the primary annotation; `test_status` remains a synchronized deprecated
  alias during the 0.x transition.
- CI pins third-party actions, declares read-only permissions, builds and smoke-tests distributions,
  runs Postgres 14/16 lifecycle suites, and compiles the BigQuery project without introspection.

## Remaining before a stable v1

- Push all app filters, full-text search, metrics, and cursor pagination into warehouse queries;
  keep SQLite only for a bounded offline page/snapshot and pending patches.
- Add representative scale fixtures and automated `EXPLAIN` budget checks on Postgres and dry-run
  byte budgets on BigQuery.
- Expand the tested Python/dbt compatibility matrix only from observed demand.
- Enable the scheduled credentialed BigQuery workflow: it is keyless and ready, and needs the
  one-time cloud setup in [scheduled BigQuery verification](scheduled-bigquery.md).
- Validate dbt 1.12 and pandas 3 as separate compatibility upgrades (#11).
- Identify what puts the app's sync peak at about 560–580 MiB in many runs (independent of the
  PyArrow memory pool; see [scale](scale.md)).
- Publish relation/column stability guarantees and remove the deprecated `test_status` alias only
  in a clearly announced release.

## Adoption and commercial validation

Keep the package and local app Apache-2.0. Recruit 5–10 design partners before creating a paid
control plane, and measure activation, weekly reviewed issues, recurrence, assignment, and alerting
demand without collecting failed-row contents. If collaboration demand is proven, build a separate
metadata-only-by-default hosted/self-hosted control plane and monetize collaboration, governance,
integrations, managed operation, and support—not failure-row volume.

## 0.2 delivery

Implemented: transactional tracking and recovery, fresh installation and additive migrations, row owners, metadata and verdicts, durable history and missed reports, warehouse health with local caching, and isolated optional maintenance with its own health. The remaining release gates are in the [release checklist](release-checklist.md).

Deferred: VCG adoption/migration and warehouse-side issue filtering, search and cursor pagination. Pending local edits must remain accessible as that pagination work proceeds.
