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

## Remaining v1 interface and operating goals

- Refine the existing warehouse-browsing preview using measured latency and byte costs.
  Keyset pages, warehouse filters/search/metrics, bounded caches, scale fixtures and Postgres
  query-plan checks already exist; they are not new implementation work.
- Configure and prove approved, on-demand credentialed GitHub validation before enabling it.
  Cloud PR smoke and WIF release validation remain unavailable pending maintainer setup.
  No scheduled cloud execution is planned.
- Add BigQuery byte-budget regressions when cloud validation is authorized.
- Expand compatibility from observed demand. Core 1.12 and pandas 3 are already validated.
- Identify the cause of the snapshot sync peak documented in [scale](scale.md).
- Publish relation/column stability guarantees. Remove the deprecated `test_status` alias only
  in an announced compatibility release.
- Use the [Python quality baseline](quality-baseline.md) to prioritize tests and focused
  refactoring; a type-checking rollout and query-label/cost-reporting work remain follow-ups.

## Adoption and commercial validation

Keep the package and local app Apache-2.0. Recruit 5–10 design partners before creating a paid
control plane, and measure activation, weekly reviewed issues, recurrence, assignment, and alerting
demand without collecting failed-row contents. If collaboration demand is proven, build a separate
metadata-only-by-default hosted/self-hosted control plane and monetize collaboration, governance,
integrations, managed operation, and support—not failure-row volume.

## 0.2 delivery

Implemented: transactional tracking and recovery, fresh installation and additive migrations, row owners, metadata and verdicts, durable history and missed reports, warehouse health with local caching, and isolated optional maintenance with its own health. The remaining release gates are in the [release checklist](release-checklist.md).

Deferred: VCG adoption/migration and promotion of the warehouse-browsing preview. Existing warehouse filtering, search and pagination preserve access to pending local edits.
