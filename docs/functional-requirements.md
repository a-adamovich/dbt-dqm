# dbt-dqm functional requirements

## Adapter support

- The dbt package supports BigQuery and Postgres. Behavior described below (identity hashing,
  lifecycle transitions, granularity handling) is a functional requirement, not an
  implementation detail, and must hold identically on every supported adapter — a warehouse-specific
  divergence in observable behavior is a bug. Adapter differences are confined to
  `macros/adapters.sql`.
- The local review app supports BigQuery and Postgres and resolves package relations from dbt's
  generated manifest.

## Collection

- dbt-dqm tracks data tests with the configured tag (`dqm` by default) and
  `store_failures: true`.
- The package captures conclusive test executions and configured stored-failure fields in an
  `on-run-end` hook. It never interprets an unselected, skipped, interrupted, or collection-error
  test as a pass.
- `meta.dbt_dqm.granularity` is the only source of issue identity columns. It is mandatory for
  every tracked test (including passing tests), must be a non-empty list of distinct non-empty
  names after case folding, and every configured column must be returned by a failing test query.
- Capture modes are `identity_only` (default), `allowlist` with `context_columns`, and explicit
  `full`. Invalid capture metadata is a configuration error and never lifecycle evidence.
- Test tags are stored as a compact JSON string in manifest order.

## Issue identity and lifecycle

- `unique_id` is a lowercase SHA-256 of the versioned `dqm-id-v1` length-prefixed byte contract,
  containing ordered lowercase key names, trimmed/lowercase textual values, and explicit null
  markers. Display JSON is never a hash input.
  Because null is a valid, meaningful identity value, several failing rows that legitimately share
  a null in every configured granularity column will collapse into one issue, the same as any other
  shared key — this is by design, not a defect, but it means a granularity choice with a
  frequently-null column collapses more than a reviewer might expect.
- Duplicate failed rows at the configured grain form one issue observation with a row count and a
  deterministic representative payload.
- The first observation creates an Active occurrence. Continued observations update it. A
  conclusive run without the identity archives it as Passed — but only when the test's
  `meta.dbt_dqm.granularity` is unchanged since the occurrence was last touched. If the granularity
  changed (a column added, removed, or reordered), every previously Active occurrence's identity
  hash changes too, and the old identity's apparent absence is archived as `IDENTITY_CHANGED`
  instead of `Passed`, so a re-grained test doesn't read as a silently resolved backlog. A later
  observation creates a new occurrence with blank annotations and `NEW` workflow status.
- A test that is renamed or removed from the project can never again produce a conclusive
  execution, so its occurrences can't reach the case above. Reconciliation separately sweeps Active
  occurrences whose test is absent from the current manifest entirely (not just unselected in one
  invocation) and archives them with `close_reason = 'TEST_REMOVED'`.
- Row-level capture within one invocation commits per test as each test's failure evidence is
  collected, rather than deferring every test's write to one shared batch at the end. A collection
  error partway through an invocation only leaves the test being processed (and any after it in
  that invocation) at `pending`, instead of losing already-collected evidence for tests processed
  earlier in the same run. A `pending` execution is excluded from reconciliation and is picked up by
  a future successful invocation of the same test.
- Reconciliation processes every unprocessed conclusive execution in `captured_at, invocation_id`
  order and checkpoints only after a successful model write. Deterministic occurrence IDs make
  retries idempotent and preserve brief fail/pass episodes between package builds.

## Review application

- The local app reads the dbt profile selected by project, profiles directory, and target. It stores
  no credentials.
- Local SQLite contains failed-row snapshot data and field-level patches, uses owner-only filesystem
  permissions, and can be inspected or purged from the CLI. Reviewers may edit `workflow_status`,
  `call_to_action`, `ticket_url`, `notes`, and `poc_responsible`; `test_status` is a deprecated
  compatibility alias.
- Workflow status is selected from `NEW`, `TRIAGED`, `IN_PROGRESS`, `BLOCKED`, `RESOLVED`,
  `ACCEPTED_RISK`, and `FALSE_POSITIVE`. Historical custom values remain selectable during
  migration.
- Issues are rendered as record cards rather than single-line grid rows. Key and attribute fields
  are combined into one two-column table with a separately bordered `Column | Value` row for every
  field, keeping the whole failed record visible without horizontal scrolling.
- Cards prioritize workflow status, current-lifecycle creation time, last user annotation update,
  responsible person, and call to action. Technical hashes, tags, and test timestamps are collapsed.
- Sync overlays unapplied local patches on fresh warehouse data. Apply uses annotation versions,
  writes one audited bulk batch, refreshes the snapshot, and clears only the exact applied patch
  versions. If a pending patch's recorded base value no longer matches the freshly synced snapshot
  — meaning the warehouse value changed since the patch was staged — Apply is blocked until the
  reviewer discards the patch or explicitly rebases it for overwrite. A later concurrent write
  still returns a compare-and-set conflict.
- Issue cards are paginated (25/50/100/200 per page, configurable) rather than all rendered in one
  script run, so the record-card layout stays usable well beyond a few hundred issues.
- The full-text search box only re-filters on explicit submit (Enter or the Apply button), not on
  every keystroke, since it scans every column of every row. Every other filter (lifecycle, workflow
  status, owner, created date) still reacts immediately.
- One background-job process pool is shared across all review sessions for the life of the app
  process, rather than one per session.

## 0.2 additions

Reconciliation must be idempotent across retries and protect concurrent annotations. Each run freezes exact execution IDs, rejects late evidence, and applies occurrences, events, receipts and state atomically under a generation guard. Installation rejects recognizable legacy tables before writes; subsequent additive migrations preserve history. Schema-only runs have zero-row projections and no tracking-data changes.

Ownership may come from each failed row without expanding captured payloads. Conflicts use the static fallback and are measurable. Priority/criticality refresh Active issues and freeze on closure. Review verdicts are separate, audited annotations. History distinguishes previous occurrences, Passed recurrences and potential regressions, and persists payload changes independently of raw-log retention.

Known misses accept mapped or unmapped areas and stable retry IDs. Health publishes denominators and null undefined rates, explicitly separating review, closure and backlog populations, collection from processing coverage, and retention gaps from inactivity.
