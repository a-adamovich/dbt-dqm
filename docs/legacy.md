# Legacy upgrade and recovery code

dbt-dqm 0.2.x carries code whose only job is to upgrade older installations or to repair one past
incident. It still runs and is still tested. It is kept for compatibility, so don't build anything
new on it. This page explains what the compatibility code does and how to upgrade a
populated installation.

The recovery macros for late evidence, stuck runs and leftover stages aren't legacy. They handle
normal runtime situations and are described in
[How a run works](how-it-works.md#when-something-goes-wrong).

## Upgrading

Upgrade the Python command and the dbt package together, run `dbt deps`, and restart the
review app. A normal run applies pending schema migrations. From 0.2.0a7 or a8, a normal
upgrade in the same namespace needs no full refresh. The 0.2.5 identity migration preserves
issue hashes, annotations, and display order. If your installation already encountered
[#105](#the-105-identity-repair), pause the workflow and follow the
[incident repair](identity-105-recovery.md).

Upgrades from 0.2.0a4–a6 preserve history when resolved names stay the same. Since a7,
alias prefixes also apply to state tables. When setup recognizes legacy shared tables in
the schema, it refuses to initialize a new prefixed installation in a normal run.

Before changing the namespace, apply pending edits or record them separately. Stop the old
review app. Run `dbt-dqm run --full-refresh` with the new target and naming variables and
verify the destinations before confirming. This starts isolated history for the resolved
installation. Shared history stays in its original tables. See the [reset and confirmation
rules](reference-cli.md#exit-codes-and-full-refresh).

For 0.1, point `dbt_dqm_schema` at a new schema to keep the old history available for
queries. To start over in the same schema, back up any history you need and explicitly confirm
`dbt-dqm run --full-refresh` for that installation. The reset discards its old DQM history
and starts a fresh installation. For older 0.2 alphas before a4, export annotations you need
before a full refresh. Update tests to the [current configuration](reference-configuration.md#tracked-data-tests),
remove obsolete settings, and set a [retention policy](operations.md#retention-and-maintenance).
After an intentional reset, purge the obsolete local workspace before restarting the app.
Annotation drafts from the previous install cannot be applied to the new one.

## Setup and migrations run on every install

Every run starts with setup (`initialize_dqm`, and again inside reconciliation). Setup checks the
installation, then applies any migration it hasn't recorded yet, in order:

| Migration | What it adds |
| --- | --- |
| `0001_initial` | The 0.2 tables. |
| `0002_event_payload_mode` | Event payload mode, changed columns and digests. |
| `0003_app_change_staging` | The BigQuery staging table for app edits. |
| `0004_app_staging_safety` | Upload IDs and a 24-hour lifetime for staged edits. |
| `0005_maintenance_log` | The maintenance outcome log. |
| `0006_maintenance_attribution` | Which invocation triggered each maintenance outcome. |
| `0007_browse_keyset_index` | An index for warehouse browsing (Postgres). |
| `0008_canonical_identity_signature` | Engine-independent identity metadata (see [#105](#the-105-identity-repair)). |

A **fresh** install also goes through this list, and some of its objects exist only because a
later migration creates them. `0001` creates the core tables. On BigQuery, `0003` creates the app
staging table. On both warehouses, `0005` creates the maintenance log and `0006` adds its
attribution columns. On Postgres, `0007` creates the browsing index. The other steps find their
columns already in place. So the upgrade path is
also today's install path. Once an installation is at the latest migration, setup takes a fast
path and does no DDL.

Each migration is applied, backfilled where needed, verified, then recorded in
`dqm_schema_migrations`. `dqm_reconciliation_control.schema_version` holds the latest migration.
Migrations also increase the generation, so a reconciliation frozen before them can't apply
afterwards. Postgres increases it as each migration starts and again as it finishes. BigQuery
increases it once when a run claims the migration lease and once when it releases the lease.

**Why BigQuery needs a migration lease.** BigQuery can't run schema changes inside a transaction.
A migration that stops halfway can't roll back, so the first run to start one claims a lease in the
control row, valid for `dbt_dqm_reconcile_timeout_minutes`. Every later step checks it still owns
the lease. Another run that arrives meanwhile gets "DQM migration is already running" and should
retry later. On Postgres, migrations run in one transaction under a lock, so an interrupted one
rolls back.

**Why an older package refuses a newer schema.** If the warehouse is at a later migration than the
installed package knows, setup stops with "DQM schema is newer than this package". Upgrade the
package. Don't run an older writer against an upgraded installation.

Source: `migrations` and `setup_sql` in [`schema_management.sql`](../macros/schema_management.sql),
and [`identity_migration.sql`](../macros/identity_migration.sql).

## Refusals that protect older data

Setup refuses to write in these cases. It never deletes or converts the old data:

- **A 0.1 schema.** Tracking tables without a 0.2 install marker. Point `dbt_dqm_schema` at a new
  schema to preserve the old history, or explicitly reset the existing installation with
  `dbt-dqm run --full-refresh`. The reset discards the old DQM history. See [Upgrading](#upgrading).
- **An incompatible marker.** `dqm_install` exists without `dqm-full-v1`.
- **BigQuery collation from before 0.2.1.** Collated tracking columns broke case-sensitive identity.
  See [BigQuery collation recovery](operations.md#bigquery-dataset-collation) for reinstall options.
- **A newly prefixed namespace next to legacy shared tables.** Since 0.2.0a7, a prefixing alias
  macro also prefixes the internal tables. If the old unprefixed tables still exist in that schema,
  an ordinary run won't start a second installation beside them. It asks for an explicit
  `--full-refresh` of the new, isolated installation.

## Namespaces and separate history

Changing `dbt_dqm_schema` or your naming macros points dbt-dqm at a different installation. The new
one starts with empty history. Nothing is copied, split or deleted, and the old tables stay where
they are. Local workspaces are separate too, because the workspace path includes the resolved
namespace. Before you switch, apply or discard pending edits in the old app.

## The #105 identity repair

On 0.2.4 and earlier, switching between dbt Core and dbt 2 could close open issues as
`IDENTITY_CHANGED`. The two engines wrote the identity metadata as JSON in different ways, so a
switch looked like a granularity change. The issue hash itself was unchanged.

Since 0.2.5, identity metadata uses a length-prefixed text format that doesn't depend on the
engine. The pieces that deal with the old format are:

- Migration `0008`, which decodes the old JSON signatures from either engine and rewrites them
  once, stopping on anything malformed or contradictory.
- The decoder in [`identity.py`](../src/dbt_dqm_app/identity.py).
- `scripts/repair_identity_105.py`, a one-time BigQuery repair that moves reviewer state back onto
  the original occurrences after an incident.
- A CI job (`cross-engine-upgrade`) that reproduces the incident from the 0.2.4 code, migrates,
  repairs and switches engines back and forth.

[Engine switches and issue continuity](identity-105-recovery.md) has the procedure. Don't use a full
refresh on an affected installation, because it removes the history the repair needs.

## Smaller compatibility paths

- The app upgrades older local workspaces in place (new columns, and old `test_status` edits renamed
  to `workflow_status`).
- `test_status` is a deprecated copy of `workflow_status`. Every workflow write also writes it.
- Obsolete test settings get a warning (`capture_mode`, `context_columns`,
  `dbt_dqm_capture_mode`, `dbt_dqm_reconcile_lookback_days`) or an error (`dbt_dqm_test_tag`,
  `poc_responsible`, `owner_column`, `dqm_granularity`).
- A newer app reports maintenance health as unavailable when the package predates migration
  `0005`, instead of failing the sync.
- On unprefixed Postgres installations, indexes keep the names they had before namespaces were
  isolated, so an upgrade doesn't create duplicates.
- `dqm_maintenance_health` treats log rows written before migration `0006` as unattributed and
  reports them as `unknown` until a newer outcome replaces them.
