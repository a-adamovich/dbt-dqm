# Operate dbt-dqm

See [supported configurations](supported-configurations.md) for installation and
the [CLI reference](reference-cli.md) for command options.

## Upgrading

Follow the [upgrade and namespace migration guidance](legacy.md#upgrading).

## Retention and maintenance

Set positive `dbt_dqm_retention_days` to prune processed raw executions, observations,
and old run-ledger rows. Set `dbt_dqm_event_retention_days` to prune issue event
history. With the default `dbt_dqm_event_payloads: full` and no event retention,
captured before/after values can be kept forever and may contain personal data.
`changed_columns` retains column names and hashes, which are not anonymization;
`none` omits the values. Choose a policy appropriate for your data.

Maintenance runs at the end of `dbt-dqm run`, or on demand with
`dbt run-operation cleanup_dqm_logs --vars "{dqm_enabled: true}"`. A SQL error inside
a maintenance step does not fail the dbt run; outcomes are recorded in
`dqm_maintenance_health` and the app's Health tab. See the
[warehouse contract](warehouse-interfaces.md#optional-maintenance) for the guarantees.

The experimental BigQuery option
`dbt-dqm run --vars "{dbt_dqm_maintenance_shortcuts: true}"` probes default automatic
cleanup steps and skips separate jobs only when no eligible work exists. Unknown
eligibility and overridden cleanup implementations run normally; raw-retention cutoffs
still advance. It is off by default because a live performance benefit has not been
established by credentialed benchmarks.

## BigQuery dataset collation

Version 0.2.1 and later support datasets whose default collation is `und:ci`. DQM tables explicitly use
`DEFAULT COLLATE ''` and tracking comparisons remain case-sensitive. The package does not change
your dataset default or your test's own comparison rules. Identity normalization still lowercases
and trims granularity values; original payload and owner casing is retained.

Upgrade both packages and run `dbt deps`. Existing uncollated DQM
installations upgrade normally, without a schema migration or history reset.

If an earlier package created collated tracking columns, 0.2.1 and later refuse setup before tracking
writes. Changing a table's default does not repair existing columns, and BigQuery cannot change
their collation in place. For this affected installation only:

1. Export any history or annotations you need to retain, and stop its reviewer apps.
2. Upgrade both packages and run `dbt deps`.
3. Run `dbt-dqm run --full-refresh` with the same project, profile, target and naming `--vars`.
   Review the printed relation scope and confirm. This discards that installation's history and
   annotations; use `--yes` only after reviewing that scope for an unattended invocation.
4. Clear the installation's old reviewer workspaces and restart the apps. Old drafts are invalid.

The refresh preserves unrelated tables, dataset collation and existing table access. No automatic
reset or history-preserving conversion is performed. A separate tracking schema is another option
if you need to leave the old installation intact.
