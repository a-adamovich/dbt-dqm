# Design: warehouse-side issue browsing (0.3)

Status: **agreed 2026-10-11** (decisions recorded below). Implementation follows the delivery plan.

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

**Mode selection (decided):** `--browse auto` (the default once the evidence in "Acceptance" exists)
uses the snapshot sync when the backlog fits the snapshot limits and switches to warehouse paging
automatically when it doesn't (the count is already read before every sync). `--browse snapshot`
and `--browse warehouse` force a mode.

### 2. Query builder (`warehouse.py`)

- One builder produces **parameterized** SQL for both adapters against `dqm_all_issues`. User input
  is never interpolated: BigQuery query parameters, psycopg2 parameters.
- **Filters pushed down:** lifecycle (`record_status`), workflow status set, owner
  (`poc_responsible`, with "Unassigned" mapping to null or blank), created-date range on
  `first_seen_at`, priority (`test_priority`), tags (BigQuery
  `exists(select 1 from unnest(json_value_array(test_tags)) t where trim(t) in unnest(@tags))`;
  Postgres the same over `jsonb_array_elements_text`, so trimmed tags match as in snapshot mode),
  and "recurrences only" (`is_recurrence`). Missing values map as in snapshot mode: no workflow
  status is `NEW`, no priority is `unset`, a blank owner is `Unassigned`.
- **Keyset pagination in the snapshot order:** priority rank, workflow-status rank, `first_seen_at`,
  `occurrence_id` (the same order snapshot mode uses, minus its "pending edits first" step, which
  is local state; see section 6). The next page continues after the previous page's last key
  (a row comparison on Postgres, expanded to an `or` form on BigQuery). Previous pages come from
  a cursor stack in the session. No `offset`.
- **Two-phase read on BigQuery:** phase 1 selects only key and filter columns (`occurrence_id`,
  `first_seen_at`, status columns) to choose the page; phase 2 reads the full rows for at most
  `page_size` ids (`where occurrence_id in unnest(@ids)`). This avoids scanning payload columns for
  every page.

### 3. Consistency

- Every read carries the control generation and setup status **in the same statement** (the
  control row is left-joined to the query), so the rows and the generation come from one
  snapshot on both adapters. A BigQuery page's two reads must report the same generation (and
  the second must return every id from the first); otherwise the page is read again once, then
  reported as "data changed". (The first implementation bracketed each read with separate
  before/after checks; measurement showed that tripled BigQuery latency.)
- The page records its generation. If a later page sees a different generation, the app keeps
  the rows it has, shows "results changed since you started; restart from the first page", and
  offers a one-click restart. Mixing generations silently is never allowed.
- Edits keep their compare-and-set guarantee: each patch carries `base_annotation_version`, so a
  page that is out of date can never overwrite a newer annotation.

### 4. Metrics

- The header counts (Active, New, Blocked, Recurring) come from **one aggregate query** over the
  whole browsable population, not the filtered list, exactly as in snapshot mode (`countif` on
  BigQuery, `count(*) filter (where …)` on Postgres).
- A second query counts the issues matching the current filters ("Showing N of M"); with a search
  it is guarded like any other search. Both run under one verified generation.
- The "Pending edits" count stays local.
- Results are cached per `(filters, generation)` for the session.

### 5. Search

- Applied on submit, as today. Requires 3–200 characters and is parameterized.
- **Postgres:** `ilike` over a fixed set of text columns (test name, display identity, notes, call
  to action, owner, and the payload as text), combined with the other filters and the page limit.
  No index is required for 0.3; `pg_trgm` is documented as an optional speed-up.
- **BigQuery (decided):** `contains_substr` over the same columns, with a configurable ceiling
  (`--max-search-gib`, default **1 GiB**):
  - search runs only when the reviewer submits it, never on each keystroke;
  - a **dry run** estimates the bytes first; above the ceiling the search is refused;
  - the search query itself runs with **`maximum_bytes_billed`** set to the ceiling, so BigQuery
    enforces it even if the estimate was wrong;
  - a refusal keeps the current results on screen and suggests narrower filters (lifecycle, date
    range, status, owner, tags);
  - the docs state plainly that page reads and count queries also process (and bill) bytes; the
    ceiling applies to search, and per-page and count costs are measured and documented separately.

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
5. **Evidence, then `--browse auto` as the default:** scale runs at 250k and 1M occurrences, BigQuery bytes per
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

## Decisions (2026-10-11)

1. **Mode:** automatic. Snapshot sync while the backlog fits the snapshot limits; warehouse paging
   above them; `--browse` forces either.
2. **Search cost:** 1 GiB default, configurable; dry-run estimate plus `maximum_bytes_billed` on
   every search; submit-only; refusals keep current results and suggest narrower filters; page and
   count costs documented separately.
3. **Migration 0007:** included, with both the Postgres keyset indexes and the BigQuery clustering
   (the clustering step only if measurement shows a benefit).
