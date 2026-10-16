# Review issues in the local app

The Python `dbt-dqm` installation includes a single-user local browser app for the issues
captured by `dbt-dqm run`. Start it from the same dbt project and target used for the run:

```bash
dbt-dqm app
```

The app reads your dbt profile and synchronizes saved warehouse issues at the start of a
browser session. It does not execute tests or capture failures. Use its **Health** view to
inspect setup, tests, collections, and maintenance. If tracking relations are missing, run
`dbt-dqm run` first. Pass the same `--vars` when custom naming macros depend on them;
switching namespace in an open session requires a restart.

## Review and apply

Browse issue cards and filter by lifecycle, workflow status, owner, creation date, or text.
Cards show the captured record, owner, review status, timestamps, and action fields. You can
edit owner, workflow status, review verdict, call to action, ticket URL, and notes. Local
edits are pending until **Apply and sync** submits them to the warehouse.

The app compares annotation versions before applying changes and keeps an audit history.
A concurrent reviewer edit produces a conflict instead of a silent overwrite. Sync overlays
pending local edits on fresh saved values; a changed underlying value must be discarded or
explicitly rebased before Apply. A busy Postgres write waits up to `--lock-timeout` seconds
(default 5) before reporting the warehouse as busy.

### Bulk changes

Select issues individually or use **Select current page**. Selections persist across pages.
Choose bulk owner, workflow status, or review verdict; each field defaults to **No change**,
and owner also supports explicit unassignment. **Stage changes** creates local pending edits.
**Apply and sync** submits those edits using the same conflict checks and audit trail as
individual edits. Chosen fields replace existing drafts for those fields; notes, tickets,
and other pending fields are preserved.

Selection is bounded to 2,000 issues or 32 MiB and clears after staging, filter changes,
Sync, or successful Apply. Selecting every issue of a test automatically across pages is
not supported.

### CSV and Excel exports

Expand **Export saved issues** above the cards. **Prepare CSV** or **Prepare Excel** exports
all filtered saved issues across pages. Pending local edits are excluded. Archived issues
appear only when the current lifecycle filter and archive window include them. Turn off
**Pending edits only** before exporting.

The first columns are test name, owner, captured values, statuses, dates, call to action,
ticket URL, and notes, followed by the remaining public fields. CSV retains
`record_values_json` as JSON text. Excel expands its top-level keys into `record.*`
columns, with one sheet per test; configured granularity fields come first and additional
fields follow captured order. Nested values and other JSON columns remain text. Timestamp
headers label UTC, explicit offsets, or an unspecified historical timezone without
guessing. Captured JSON timestamps are unchanged.

CSV uses UTF-8 with BOM and standard quoting for spreadsheet compatibility. Formula-like
cell text is prefixed with an apostrophe to prevent spreadsheet execution; captured JSON
remains intact. Excel writes user-provided values as text, preserving leading zeros and
large numeric-looking identifiers, never as formulas or automatic hyperlinks. Values
incompatible with Excel's limits require CSV instead.

Exports are bounded to 50,000 issues, 128 MiB of serialized source data, and 128 MiB per
completed file. A limit failure offers no partial download. Warehouse exports use one
filtered data query with installation and generation checks. On BigQuery, the complete
export query respects `--max-search-gib`, even without text search. Prepared files are
temporary and are discarded after filter changes, Sync, or Apply.

## Local workspace and browsing modes

The app keeps a project-, target-, and resolved-installation-specific SQLite snapshot and
pending edits in an owner-only local workspace. It does not copy credentials there. Use
`dbt-dqm workspace info` to inspect it or `dbt-dqm workspace purge` to remove it; purge
discards that local snapshot and any pending edits. Supply the same project, target, and
naming variables as the app.

The default `--browse auto` mode uses a local snapshot while the review population fits
within 50,000 issues and 128 MiB of serialized issue data. The snapshot includes active
issues and up to 90 days of archived history by default; `--archive-cache-days 0` keeps
active issues only. `--max-cache-issues` and `--max-cache-mib` may lower the limits.
An oversized or inconsistent sync keeps the previous cache and pending edits.

For larger backlogs, the app browses the warehouse page by page. Filters, searches, counts,
and sorting then query the warehouse, while recently loaded pages and pending edits remain
local. If the warehouse becomes unavailable, cached pages can still be shown offline.
`--browse snapshot` or `--browse warehouse` forces a mode. Warehouse browsing incurs a
query for each page, count, and search; BigQuery refuses searches whose dry-run estimate
exceeds `--max-search-gib` (default 1). See [scale measurements](scale.md#warehouse-browsing-2026-10-11)
for observed cost and memory characteristics.

For explicit paths or a target, use:

```bash
dbt-dqm app --project-dir /path/to/project --profiles-dir /path/to/profiles --target dev
```

See [operations](operations.md#project-target-and-artifact-resolution) for defaults and
the matching `--target-path` option. Restart the app after changing the naming namespace;
the local workspaces remain isolated by resolved installation.
