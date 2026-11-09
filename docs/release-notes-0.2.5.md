# dbt-dqm 0.2.5

This corrective release addresses [#105](https://github.com/a-adamovich/dbt-dqm/issues/105):
switching between dbt Core and dbt 2 could falsely archive unchanged issues as
`IDENTITY_CHANGED`, leaving their reviewer state on the original occurrences.

## Identity and upgrade behavior

- Identity metadata now uses an explicit, length-prefixed representation independent of JSON
  object order and Unicode escaping. Actual issue hashes are unchanged.
- Migration `0008_canonical_identity_signature` converts historical signatures once on both
  BigQuery and Postgres, retaining annotations, history, record-field order and output sorting.
- Compact metadata JSON no longer removes spaces inside string values. Presentation-only
  field reordering does not emit a value-change event.
- Structural closures appear separately in the workflow summary and are not reported as passes.

An already-affected installation needs an explicit incident repair in addition to metadata
migration. The [recovery guide](identity-105-recovery.md) describes the bounded, preview-first
utility. It restores original occurrence IDs and review continuity and refuses later runs,
reviewer conflicts or incomplete evidence. Do not full-refresh an affected installation.

The exported `identity_scheme_signature` field changes from JSON to the documented text
contract. Consumers that parsed its old JSON must update. `record_values_json` retains its
ordered JSON-object contract. Old package writers refuse the migrated schema.

## Compatibility and release evidence

Python 3.11–3.14, dbt Core 1.11–1.12 on BigQuery/Postgres, and dbt 2.0.9+ on BigQuery remain
supported; current validation engines are Core 1.12.6 and dbt 2.0.11.

Publication requires fresh 34-case BigQuery suites on both engines, a separate serial historical
reproduction/migration/repair/engine-roundtrip case, the serial restricted-permissions case,
and complete GitHub platform validation with 42 Postgres cases per full matrix entry.
The release attachments contain outcomes and source identities. The cross-engine case pins
the historical 0.2.4 source; it does not reuse that release's warehouse results.

BigQuery execution for this release is **maintainer-attested local validation** through a
0.2.5-only exception. It is not live WIF certification. The protected PyPI approval and verified
publishing provenance remain required. Per-query byte ceilings are not a total spending budget;
known dbt 2 query-limit gaps and any unavailable usage measurements remain disclosed.

The isolated Core 1.11 compatibility-test engine retains only the previously accepted,
advisory-specific sqlparse exception. Installed wheel variants and the default engine must pass
the release security checks.
