# 0.2.1 release verification

This patch fixes BigQuery tracking in datasets with default collation. Python and dbt packages
use `0.2.1`. Uncollated installations need no schema migration; existing collated tables require
[explicit scoped recovery](operations.md#bigquery-dataset-collation).

The [release checklist](release-checklist.md) applies. Candidate and actual gate results will be
recorded here after verification; no publication or warehouse result is claimed by this preparation.
