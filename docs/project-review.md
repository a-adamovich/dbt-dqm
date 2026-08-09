# Project review: findings and roadmap (2026-08-09)

A holistic review of dbt-dqm covering the dbt package (`models/`, `macros/`), the local review app
(`src/dbt_dqm_app/`), and project meta (licensing, packaging, CI, repo hygiene). Findings are
organized by theme with severity and file:line references. The roadmap at the end sequences the
work directionally — this is not a committed schedule.

Two of the highest-severity claims below were independently verified by direct reads of the
referenced files (not just synthesized from exploration passes): the false-archival logic in
`models/dqm_reconcile.sql` and the former FSL-1.1-MIT license text.

**Status:** Phase 1 (Trust & correctness) and Phase 2 (Cost & scale hardening) are implemented and
verified against live BigQuery — see the Roadmap section at the end for what each phase covered and
what's still open. The findings below are left as originally written (a point-in-time record); they
are not edited in place as items get fixed.

## Licensing and monetization direction (decided)

The repo is now licensed Apache-2.0 in full — dbt package and review app alike — matching dbt-core's
own license. There is no code-level open/paid split in this repo. Monetization, if pursued, follows
the dbt Labs playbook: a separate hosted product built and sold independently, promoted from here
once it exists, never by gating functionality in this repo. Multi-warehouse support (see Portability
below) is intentionally kept fully open rather than paywalled, matching community norms (dbt-utils,
Elementary) and because credible dbt Hub distribution requires it.

## Correctness / data-integrity

These are the highest-priority findings regardless of timeline: a data-quality tool that silently
corrupts its own tracking data undermines the reason to trust it.

- **False-archival on identity/schema change** (`models/dqm_reconcile.sql:149-181`, verified by
  direct read). The `newly_archived` CTE archives any `active_existing` row whose `unique_id` no
  longer appears in `latest_observations` for that test (`existing.last_evaluated_invocation !=
  execution.invocation_id` joined against a left-joined `latest_observations` filtered to
  `observation.unique_id is null`), tagging it `close_reason = 'Passed'`. If
  `meta.dbt_dqm.granularity` changes, or the identity-hash algorithm changes (this has already
  happened once — see the "Unreleased" breaking change in `CHANGELOG.md`), every previously Active
  occurrence gets a new `unique_id` on the next run and is indistinguishable from a genuinely
  resolved issue. All of them get mass-archived as falsely "Passed," losing every annotation, and a
  fresh `NEW` occurrence is created in its place. No detection or warning mechanism exists.
- **Orphaned Active occurrences on test rename/removal** (`models/dqm_reconcile.sql:173-176`).
  `newly_archived` requires an `inner join latest_executions` on `test_unique_id`. If a test is
  renamed or removed, its `test_unique_id` never appears in `dqm_test_executions` again, so its
  Active occurrences can never be archived — they persist indefinitely with no reconciliation path
  short of manual SQL.
- **No transactional isolation in the capture hook** (`macros/capture_test_results.sql:8-225`). The
  `on-run-end` hook inserts all tracked tests as `'pending'` up front, then loops per-test issuing
  separate `run_query` calls, then does one bulk merge at the end. There's no try/except — a single
  transient BigQuery error mid-loop aborts the whole hook, leaving some tests permanently stuck at
  `collection_status = 'pending'` (excluded from reconciliation, so this doesn't cause a false
  lifecycle transition, but it does silently drop that invocation's failure evidence with no
  retry/backfill mechanism).
- **No enforced "at most one Active occurrence per identity" invariant.** Nothing in
  `models/schema.yml` enforces this. Combined with the deterministic-but-not-cross-invocation-safe
  `occurrence_id` construction (`sha256(test_unique_id|unique_id|invocation_id)` at
  `models/dqm_reconcile.sql:115-117`), two `dqm_reconcile` runs executing concurrently could each
  create a simultaneously Active occurrence for the same identity, and nothing would catch it.
- **NULL granularity values collapse distinct failures into one issue**
  (`macros/capture_test_results.sql:114-120`). The identity hash serializes a null granularity
  column as JSON `null`; if a configured granularity column is legitimately null across multiple
  logically distinct failing rows, they all hash identically and silently collapse into one issue.
  Undocumented as a caveat.
- **Identity hash is sensitive to numeric/whitespace formatting**
  (`macros/capture_test_results.sql:114-120`) — no `TRIM()`, and BigQuery's `CAST(... AS STRING)`
  can format numerically-equal values differently by declared type/scale, producing different
  `unique_id`s for what a reviewer would consider the same key.
- **Ad hoc, hand-maintained schema migrations** (`macros/helpers.sql:66-158`) hard-code a one-time
  migration path for specific legacy columns. Every future schema change needs a new hand-written,
  duplicated migration branch — this doesn't scale.
- **Zero automated tests of reconciliation semantics.** `models/schema.yml` only tests
  `occurrence_id`/`unique_id` not-null/unique and `record_status` accepted-values
  (`Active`/`Archived`) — nothing tests that Active rows have `archived_at IS NULL`, that
  `close_reason` is null iff Active, the at-most-one-Active invariant, or that occurrence numbering
  increments correctly on recurrence. There are no `unit_tests:` blocks anywhere in the repo.
  `integration_tests/demo_bigquery` is a manual, human-verified walkthrough (`README.md`'s
  seed→run→test→build sequence across `initial`/`passed`/`recurrence` scenarios) with no scripted
  assertions comparing expected vs. actual row counts or lifecycle transitions — nothing would catch
  a reconciliation regression automatically.
- **Docs vs. reality gaps.** `docs/warehouse-interfaces.md:20-21` and
  `docs/functional-requirements.md:20-22` describe archival as following "a conclusive pass," without
  mentioning that a granularity/identity-scheme change produces the identical observable effect. The
  NULL-collapsing behavior and the stuck-`pending`-row partial-failure scenario are similarly
  undocumented.
- **Wasted no-op MERGE on every build** (`models/dqm_annotation_changes.sql:1-20`) — the model
  produces zero rows (`where false`) but is still an incremental/merge model, issuing a MERGE DML
  against a potentially large audit table on every single invocation.

## BigQuery cost / scale

- **No partitioning or clustering anywhere** (`macros/helpers.sql:35-64` DDL for
  `dqm_test_executions`/`dqm_issue_observations`). `models/dqm_reconcile.sql`'s `latest_executions`
  (lines 15-28), `latest_observations` (31-37), and the incremental `existing` CTE (`select * from
  {{ this }}`, line 41) all perform unfiltered full-table scans on every single run. As invocation
  history accumulates, every `dbt build --select package:dbt_dqm` becomes progressively more
  expensive with no bound.
- **No retention/expiration policy** on the append-only `dqm_test_executions` and
  `dqm_issue_observations` log tables — they grow forever.

## Portability (BigQuery → other warehouses)

Relevant because multi-warehouse support is intentionally kept open/free — this is real product
work, not a licensing lever.

- **`relation_name` macro hard-codes BigQuery backtick 3-part naming**
  (`macros/helpers.sql:23-27`) — bypasses `adapter.quote()`/`api.Relation.create()` entirely; the
  primary blocker to any second-adapter support.
- **SHA-256 identity hashing uses BigQuery-only syntax** (`LOWER(TO_HEX(SHA256(...)))` in
  `macros/capture_test_results.sql:114` and `models/dqm_reconcile.sql:115-117`). Snowflake uses
  `SHA2(x, 256)`, Postgres needs the `pgcrypto` extension, Redshift has no native SHA-256, Databricks
  uses `sha2(x, 256)` — this needs adapter-dispatched macros.
- **BigQuery type literals instead of dbt's portable type macros**
  (`int64`/`float64`/`timestamp` in `macros/capture_test_results.sql:39,42` and
  `models/dqm_reconcile.sql:46-68`) — should use `{{ dbt.type_int() }}` etc.
- **`TO_JSON_STRING(STRUCT())` and `UNNEST([1])` idioms** are BigQuery-specific patterns used
  throughout for row construction and empty typed CTEs.
- **Hand-rolled `MERGE ... WHEN NOT MATCHED THEN INSERT ROW`** instead of dbt's
  `{{ get_merge_sql() }}` — bypasses dbt's cross-adapter merge abstraction.

Bottom line: supporting Snowflake/Postgres/Redshift/Databricks is a structural project (introducing
an `adapter.dispatch()` layer across `macros/helpers.sql` and `macros/capture_test_results.sql`,
plus per-adapter hash/type macros), not a config change. Sequence after the correctness fixes above
so bugs aren't multiplied across adapters.

## Python review app (`src/dbt_dqm_app/`)

- **Scale cliff: the entire issue set loads into memory with no pagination or virtualization**
  (`streamlit_app.py:134-356`). `workspace.rows()` loads all rows unbounded into a DataFrame; the
  render loop creates a bordered container with two selectboxes, two expanders, and an HTML table
  per issue, for every row. At the "thousands of issues" scale the product implies it should reach,
  this is a well-known Streamlit performance cliff (widget-state bookkeeping and DOM size both scale
  with issue count, with no "load more"/virtualization anywhere). This is the single biggest scale
  blocker in the app.
- **Full-DataFrame stringify-and-search on every keystroke, no debounce**
  (`streamlit_app.py:241-244`) — `visible.astype(str).apply(...)` runs across every column,
  including large JSON blob fields, on every rerun.
- **Every keystroke/selection triggers a full-script rerun** that reloads and re-filters the entire
  dataset, compounding the above.
- **`ProcessPoolExecutor` in `st.session_state` is never shut down**
  (`streamlit_app.py:56,65-66`) — leaks one background process per browser session.
- **Read-modify-write race in `store.py`'s `set_change`** (`store.py:96-131`) — reads the current
  pending version, increments, writes; two sessions/tabs against the same SQLite workspace editing
  the same field concurrently can lose an update to the version counter (last-write-wins on the
  value itself, but the audit trail's version numbering isn't safe). Latent because the app is
  documented single-user, but nothing technically prevents two tabs.
- **"Local-wins" overlay never warns on remote drift** (`store.py:84-94`) — a staged local patch
  silently overwrites the warehouse value even if that value changed since the patch was staged;
  the UI never surfaces the possibility.
- **No abstraction over the workspace store** — `store.py`'s `Workspace` is a concrete SQLite class
  called directly by `streamlit_app.py` (e.g. lines 36, 80-81, 87, 104, 134, 358); there's no
  interface a future backend could implement without touching every call site.
- **Business logic embedded directly in the Streamlit script** (`streamlit_app.py:134-356`) mixes
  data loading, filtering, sorting, dirty-tracking, and diff-staging inline — none of it is
  unit-testable without a full Streamlit runtime, unlike the cleanly factored `display.py`.
- **Thin test coverage.** Of roughly 998 lines across the six substantive modules, only
  `display.py` (176 lines) and `store.py` (149 lines) have meaningful direct coverage.
  `config.py` (0%, including an untested Jinja-rejection security guard at `config.py:33-34`) and
  `streamlit_app.py` (0%, the largest module at 363 lines) are completely untested.
  `warehouse.py`'s actual BigQuery I/O — `apply_patches`, the staging-table-plus-MERGE flow at
  `warehouse.py:101-177`, arguably the most business-critical code in the app — is untested; only
  its pure helper functions are.
- **Security posture is otherwise clean**: no SQL injection (parameterized queries throughout,
  `load_table_from_json` for warehouse writes), no shell injection (`subprocess.run` uses arg
  lists, never `shell=True`), credentials are never written to disk or logged. One low-severity
  note: `config._plain` (`config.py:31-35`) expands `$VAR`/`~` in arbitrary profile values, a minor
  indirect env-var substitution surface if `profiles.yml` were ever untrusted.
- **Single-user/local-only by construction, not convention** — a hardcoded local SQLite path via
  `platformdirs`, zero auth/session/tenant concept anywhere, one BigQuery profile per process. This
  is only relevant as a forward-looking seam: `display.py`'s pure formatting functions and the
  `Patch`/`EDITABLE_FIELDS` data model are already backend-agnostic and would carry over cleanly to
  a future hosted variant; the storage and credential layers would need a rewrite, not an extension,
  whenever that's actually pursued.

## Project hygiene

- **Zero git commits, no remote configured, no tags** — confirmed via `git log`/`git status`/`git
  remote -v`/`git tag -l`/`git branch -a`. This is the actual blocker to any distribution (GitHub
  visibility, dbt Hub, PyPI) independent of any code work.
- **No CI/CD** — no `.github/` directory, no pre-commit config, no Makefile. `ruff` and `pytest` are
  configured in `pyproject.toml` but never run automatically.
- **Packaging gap** — the built wheel packages only `src/dbt_dqm_app`
  (`[tool.hatch.build.targets.wheel] packages = ["src/dbt_dqm_app"]`), not the dbt package
  (`models/`, `macros/`, `dbt_project.yml`). `pip install dbt-dqm` delivers only the review-app CLI.
  The dbt package needs its own distribution path — a dbt Hub listing plus a git-ref example in
  `packages.yml` — which the README doesn't yet document for downstream consumers.
- **`pyproject.toml` missing PyPI-readiness metadata** — no `classifiers`, no `[project.urls]`
  (homepage/repository/issue tracker), no `keywords`.
- **No `CODE_OF_CONDUCT.md`, `SECURITY.md`, or issue/PR templates.**
- **Version `0.1.0` everywhere**, and `CHANGELOG.md` has a structurally odd "## 0.1.0 - Unreleased"
  heading — no version has ever actually been cut/released, consistent with the empty git history.
- **Narrow compatibility window** — `dbt-core==1.11.11` and `dbt-bigquery==1.11.3` are exact-pinned
  in `pyproject.toml`; `dbt_project.yml` requires `[">=1.11.0", "<1.12.0"]`. Worth loosening unless
  there's a specific reason for the exact pin.

## Roadmap (directional — themes, not deadlines)

1. **Trust & correctness — done.** Fixed false-archival-on-schema-change (`granularity_signature` +
   `IDENTITY_CHANGED` close reason) and orphaned-Active-on-rename (`TEST_REMOVED` sweep, both proven
   live against BigQuery via temporary fault injection). Made the capture hook commit per-test
   instead of batching to the end. Added the at-most-one-Active dbt test and normalized identity-hash
   whitespace. Turned the demo walkthrough into scripted assertions
   (`assert_scenario_outcomes`). Closed the docs/reality gaps. Replaced the ad hoc migration pattern
   with a generic `ensure_column` macro. NULL-collapsing was determined to be by-design (documented,
   not changed) rather than a bug.
2. **Cost & scale hardening — done.** Partitioned and clustered `dqm_test_executions`,
   `dqm_issue_observations`, and `dqm_issue_occurrences`; added opt-in `dbt_dqm_retention_days`
   (partition expiration) and `dbt_dqm_reconcile_lookback_days` (partition-pruning) cost levers,
   both unset/full-scan by default to preserve existing behavior. Paginated the Streamlit issue list,
   debounced full-text search behind an explicit submit, replaced the per-session
   `ProcessPoolExecutor` with one shared via `st.cache_resource` (verified only one worker process
   exists across multiple sessions), fixed `store.py`'s version-counter race with `BEGIN IMMEDIATE`,
   and added a drift warning when a pending patch's base value no longer matches the synced snapshot.
   The `dqm_annotation_changes` no-op-MERGE-on-every-build item was deliberately left as-is: every
   materialization approach considered either couldn't be verified to actually reduce cost or risked
   the audit table's external-write guarantee, and it was already flagged low severity.
3. **Portability** — introduce an adapter-dispatch layer, starting with Snowflake and/or Postgres,
   sequenced after (1)/(2) so correctness bugs aren't multiplied across warehouses.
4. **Go-to-market mechanics** — first git commit and push (done — see git log), a minimal CI
   workflow (ruff + pytest + a dbt build/test run against the demo), dbt Hub submission, PyPI
   metadata cleanup, fix the packaging gap, add `SECURITY.md`/`CODE_OF_CONDUCT.md`, cut an actual
   first tagged release.
5. **Future hosted-product seam** (not built now — just cheap to keep in mind) — introduce a `Store`
   interface behind `store.py`'s `Workspace` so a Postgres-backed multi-tenant version is an
   extension rather than a rewrite, if and when a hosted product is actually pursued. No auth/tenant
   work is needed until there's a real hosted product to build against.
