# Engine switches and issue continuity (#105)

An engine switch on dbt-dqm 0.2.4 or earlier can falsely close issues as `IDENTITY_CHANGED`.
Core and dbt 2 serialize identity JSON differently, including both object order and Unicode
escaping. The actual issue hash is unchanged, but reviewer state remains on the original
archived occurrence. Switching engines again can repeat the damage.

Until the corrective release and repair are verified, pause DQM execution and retention cleanup
on an affected installation. Preserve its tracking tables and existing local workspaces. Do not
use full refresh: it removes the history and annotations the repair needs.

## Permanent contract

The corrective package stores identity metadata as `dqm-id-v1|k6:source|k11:delivery_nm|`.
Each key is length-prefixed using Unicode code points; delimiter characters inside a key are
therefore unambiguous. Keys retain their configured order and existing case/trim normalization.
This changes the exported `identity_scheme_signature` representation, not issue hashing.

Migration `0008_canonical_identity_signature` decodes historical JSON signatures once and
updates both executions and occurrences transactionally. It accepts either engine's object
order and Unicode spelling and verifies agreement with the recorded granularity array. Unknown,
malformed or contradictory metadata stops the migration. Older packages refuse the new schema;
do not run old writers after upgrading.

The old compact JSON helper could also remove spaces inside strings, such as `a, b: c`. The
corrective helper preserves contents. Already-lost text cannot always be recovered from stored
metadata; retain original manifests/configuration and investigate contradictions rather than
inventing replacement column names.

Record fields retain SELECT order. CLI test sorting, issue sorting and pagination are unchanged.
Reordering the fields of an otherwise identical record does not create a value-change event.
Structural closures are reported separately from confirmed passes.

## Bounded incident repair

`scripts/repair_identity_105.py` is a version-controlled BigQuery incident utility, not a new
application command. It supports the reported case with no subsequent executions or reviewer
edits. Normal migration supports both BigQuery and Postgres.

**Use the utility only after the disposable BigQuery recovery check passes for the matching
release candidate. Inspect its release evidence; local planner/parser tests alone are insufficient.**

After installing matching corrective Python/dbt packages, run only setup to migrate metadata:

```sh
dbt run-operation ensure_capture_tables --vars '{dqm_enabled: true}' \
  --project-dir PROJECT --profiles-dir PROFILES --target TARGET
```

Keep the installation otherwise paused. From the matching source checkout, with the BigQuery
extra installed, preview the affected reconciliation:

```sh
python scripts/repair_identity_105.py --project-dir PROJECT --profiles-dir PROFILES \
  --target TARGET --run-id RECONCILIATION_ID --output /PRIVATE/identity-105.json
```

For naming overrides, provide the same `--vars` as the original workflow. Quote paths that
contain spaces. On Windows, use an absolute path inside a private user directory. The preview
contains original row data and annotations: do not attach it to a public issue or commit it.
POSIX files use owner-only permissions; Windows privacy depends on the directory's inherited ACL.

Review the manifest, original-occurrence count, synthetic-successor count and printed SHA256.
Only after separate authorization, repeat the command with `--apply SHA256`. This checks the
manifest digest, installation, generation and exact scoped rows again in one transaction. It
restores original IDs, creation times and reviewer state, corrects events, and retains original
records in the private manifest. A matching repair audit makes repeated application a no-op.

The per-query byte ceiling defaults to 1 GiB and may be set with `--maximum-bytes-billed`. It is
not a total budget. No quotas, IAM, keys or shared datasets are changed by this utility.

An edited successor, later execution, missing observation, limited capture or ambiguous history
stops the whole repair. Absence without a successor is not automatically a pass. Genuine new
failures and genuine granularity changes remain untouched. This utility does not handle multiple
engine-switch incidents or resolve annotation conflicts.

After a successful repair, synchronize all reviewer workspaces before resuming work. Keep the
private manifest and digest as the recovery record. A continued failure retains its original
occurrence; recurrence is reserved for a failure after a confirmed pass.
