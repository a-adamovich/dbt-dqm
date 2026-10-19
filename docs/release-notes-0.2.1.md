# dbt-dqm 0.2.1

Fix BigQuery initialization and case-sensitive tracking in datasets with a default collation
such as `und:ci`. Package tables explicitly opt out of dataset collation, including app tables,
full-refresh replacements and staging tables. String comparisons strip inherited collation;
case-only payload changes and distinct owners remain distinguishable.

Upgrade both components: install `dbt-dqm[bigquery]==0.2.1` (or
`dbt-dqm[postgres]==0.2.1`), set the dbt package revision to `v0.2.1`, then run `dbt deps`.
Existing uncollated installations retain their history without a migration or full refresh.

If an earlier run created collated tracking columns, setup stops before tracking writes.
BigQuery cannot change existing column collation in place. Follow the
[explicit recovery instructions](https://github.com/a-adamovich/dbt-dqm/blob/v0.2.1/docs/operations.md#bigquery-dataset-collation): a confirmed,
scoped `dbt-dqm run --full-refresh` discards that installation's history and annotations.
Clear its old reviewer workspaces afterward. Dataset collation and unrelated tables remain intact.
