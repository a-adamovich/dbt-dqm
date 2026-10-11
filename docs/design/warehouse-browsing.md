# Design: warehouse-side issue browsing (0.3)

Status: **proposal for review**. No code until this is agreed.

## Problem

The review app syncs every Active issue plus 90 days of archive into a local SQLite cache in one
pass (`warehouse.fetch_issues`), then filters, searches and counts in pandas
(`streamlit_app._render_issues`). That caps a project at **50,000 issues / 128 MiB**: a larger
backlog can't be reviewed at all, peak memory at the cap is about 435–585 MiB, and every sync
re-downloads everything (on BigQuery about 1.1 GB processed for 100k issues).

## Goals

- Review backlogs of any size, with memory bounded by the page size rather than the backlog.
- Filters, search and the header metrics run in the warehouse over the **whole** filtered
  population, not over what happens to be cached.
- Keep every existing contract: verified reads (the control generation and setup status around a
  read), compare-and-set annotation versions, the "busy" handling, owner-only local storage, no
  credentials in the cache.
- Pending edits stay visible and appliable even when their issue isn't on the current page.
- A bounded offline view of recently loaded pages when the warehouse is unreachable.
- Predictable cost: bytes per page on BigQuery and plans on Postgres are measured and budgeted.

## Non-goals

- Multi-user collaboration, a hosted service, or changing the warehouse lifecycle model.
- Full-text indexing inside the warehouse (search is bounded scanning; see Search).

## Design

### 1. Two read paths, one page model

A page is `(filters, sort, cursor, page_size)` → up to `page_size` issue rows plus the next cursor.

- **Browse mode (new default for large projects):** each page is read from the warehouse when
  requested.
- **Snapshot mode (today's behavior, kept):** the bounded full sync remains available with
  `--browse snapshot` for small projects and offline-first use, with its existing limits.

The CLI selects the mode (`--browse warehouse|snapshot`); the default flips to `warehouse` only
after the evidence in "Acceptance" exists.

### 2. Query builder (`warehouse.py`)

- One builder produces **parameterized** SQL for both adapters against `dqm_all_issues`. User input
  is never interpolated: BigQuery query parameters, psycopg2 parameters.
- **Filters pushed down:** lifecycle (`record_status`), workflow status set, owner
  (`poc_responsible`, with "Unassigned" mapping to null or blank), created-date range on
  `first_seen_at`, priority (`test_priority`), tags (BigQuery
  `exists(select 1 from unnest(json_value_array(test_tags)) t where t in unnest(@tags))`; Postgres
  `test_tags::jsonb ?| @tags`), and "recurrences only" (`is_recurrence`).
- **Keyset pagination:** `order by first_seen_at desc, occurrence_id`; the next page uses
  `(first_seen_at, occurrence_id) < (@t, @id)`, expanded to an `or` form on BigQuery. Previous
  pages come from a cursor stack in the session. No `offset`.
- **Two-phase read on BigQuery:** phase 1 selects only key and filter columns (`occurrence_id`,
  `first_seen_at`, status columns) to choose the page; phase 2 reads the full rows for at most
  `page_size` ids (`where occurrence_id in unnest(@ids)`). This avoids scanning payload columns for
  every page.

### 3. Consistency

- Every page read is bracketed by the existing `_snapshot_state` check (generation and setup
  status). A page read across a reconciliation is retried once, then reported as "data changed".
- The page records its generation. If a later page sees a different generation, the app keeps
  the rows it has, shows "results changed since you started; restart from the first page", and
  offers a one-click restart. Mixing generations silently is never allowed.
- Edits keep their compare-and-set guarantee: each patch carries `base_annotation_version`, so a
  page that is out of date can never overwrite a newer annotation.

### 4. Metrics

- The header counts (Active, New, Blocked, Recurring) come from **one aggregate query** with the
  same filters and no pagination (`countif` on BigQuery, `count(*) filter (where …)` on Postgres).
- The "Pending edits" count stays local.
- Results are cached per `(filters, generation)` for the session.

### 5. Search

- Applied on submit, as today. Requires 3–200 characters and is parameterized.
- **Postgres:** `ilike` over a fixed set of text columns (test name, display identity, notes, call
  to action, owner, and the payload as text), combined with the other filters and the page limit.
  No index is required for 0.3; `pg_trgm` is documented as an optional speed-up.
- **BigQuery:** `contains_substr` over the same columns. Before running, a **dry run** checks the
  bytes the query would process against a configurable ceiling (`--max-search-gib`, default 1).
  Above it, the app asks the reviewer to narrow the filters first.

### 6. Pending edits outside the page

- Patches already live in SQLite with their base value and version (`store.Workspace`), separate
  from the issue cache. The existing pending panel (100-issue local pages) remains the place to
  see and apply them, whatever page is on screen.
- An edited issue that appears on a newly loaded page shows its pending value, as today.
- If a pending patch's issue changed in the warehouse (drift), the existing discard or overwrite
  choice applies unchanged.

### 7. Bounded offline view

- The last loaded pages are kept in SQLite, up to 2,000 issues or 32 MiB, whichever comes first,
  reusing `limits.check_cache_size`. When the warehouse is unreachable, these show as "offline,
  as of <time>", with filters applied locally to that bounded set only, and labelled that way.

### 8. Warehouse support (migration 0007)

- **Postgres:** an index on `dqm_issue_occurrences (first_seen_at desc, occurrence_id)`, plus a
  partial one for `record_status = 'Active'`, so keyset pages are index range scans.
- **BigQuery:** clustering `dqm_issue_occurrences` by `record_status, first_seen_at` (metadata
  change via `alter table … set options`; existing rows recluster in the background), so phase 1
  of the two-phase read prunes blocks. If measurement shows no benefit, the clustering step is
  dropped and the cost is documented instead.
- Additive and idempotent like 0002–0006, with verify steps and interrupted-migration tests.

## Delivery, in PRs

1. **Query builder and metrics** behind `--browse warehouse` (off by default). Unit tests for SQL
   generation and parameterization, including hostile search strings.
2. **Paged UI:** cursor navigation, the generation-change banner, pending values on loaded pages.
3. **Offline bounded view.**
4. **Migration 0007** (Postgres indexes; BigQuery clustering if measurement supports it).
5. **Evidence, then the default flip:** scale runs at 250k and 1M occurrences, BigQuery bytes per
   page and per search, Postgres plans, memory, browser QA, and the full release checklist for
   0.3.

## Acceptance

- At 1M occurrences / 100k Active, both adapters:
  - first page and next-page latency, with Postgres p50 under 1 s for indexed filters;
  - BigQuery bytes per page under 100 MB with clustering, or a documented figure without it;
  - search under the byte ceiling with filters;
  - peak app memory independent of backlog size and well under the 50k snapshot figure.
- All existing app acceptance tests pass in both modes. New acceptance tests cover a generation
  change between pages, an edit to an issue not on the current page, and an offline view.
- Hostile search input (quotes, wildcards, very long strings) is parameterized and bounded.

## Questions for review

1. Default mode: flip to `warehouse` automatically when the backlog exceeds the snapshot limit, or
   always (after the evidence)?
2. BigQuery search cost guard: is a 1 GiB default ceiling per search right?
3. Is migration 0007 acceptable for 0.3? The BigQuery clustering changes table metadata on
   existing installations.
