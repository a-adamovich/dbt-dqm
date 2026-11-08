# dbt v2 compatibility verification

The current candidate targets **dbt 2.0.11**, retaining v2 support from 2.0.9 and Core
1.11–1.12. Real 2.0.9 and 2.0.11 offline regression checks are required. Full live
2.0.11/Core 1.12.6 verification is required before publication. The published release
provides its reports as attachments; historical results below cannot substitute for them.

## Historical 2.0.9 verification

The 0.2.2 candidate uses an installed dbt executable and serialized artifacts instead of
in-memory manifest mutation. The package version constraint permits v2 for testing; it does
not replace the credentialed release gate below.

| Engine | Warehouse | Verification |
| --- | --- | --- |
| dbt Core 1.12.5 | Postgres 16 | Full local acceptance suite: 39 passed |
| dbt Core 1.11.0 | Postgres 16 | Full local acceptance suite: 39 passed, using an isolated engine |
| dbt Core 1.12.5 | BigQuery | Full credentialed local acceptance suite: 32 passed; exact-commit CI gate required |
| dbt 2.0.9 | BigQuery | Full credentialed local acceptance suite: 32 passed; exact-commit CI gate required |
| dbt 2.0.9 | Postgres | Outside this release; rejected before warehouse changes |

## Release gate

For the unpublished **0.2.2** candidate, the maintainer approved complete local BigQuery acceptance on dbt 2.0.11
and Core 1.12.6 using existing credentials, followed by the serial restricted runner/reviewer
check. The 33-case selections, reports, failures/reruns and identical source tree are recorded.
Publication requires explicit maintainer attestation and the protected PyPI approval. This does
not certify live WIF. Every GitHub full-validation job and current CI remain required.

See the [step-by-step release checklist](release-checklist.md#023-recovery-of-the-unpublished-022-candidate)
for checkpoints, receipt commands, verification-only mode and tag/release behavior. The original exception rejected every other version. The approved 0.2.3 recovery below
supersedes it only for publication recovery and is tracked for removal after shipping. The on-demand
WIF workflow stays inactive; no GCP setup is required for this exception.

## Offline and local checks

```bash
uv sync --locked --all-groups --all-extras
uv run --group dbt1 ruff check .
uv run --group dbt1 pytest -q
DBT_DQM_V2_EXECUTABLE=/path/to/dbt-2.0.11/bin/dbt uv run --group dbt1 pytest -q tests/test_dbt_v2.py
```

The v2 smoke tests exercise the actual executable without importing its Python internals.
Profile rendering and the base app work without either engine installed. A v1-only CLI shim
runs inside the selected engine's Python during offline compilation to avoid dbt-bigquery
opening an unused lazy connection on close. It does not inject manifests or register callbacks.

dbt requires a builtin test materialization override in the root project. For the dedicated
test phase, dbt-dqm creates a temporary project with the same name and relative source paths,
symlinks the consumer sources and installed packages, and adds a root forwarding macro. Test
identities and selection stay stable, and original source/config files are never edited.
Artifacts and state paths remain absolute and resolve against the original project.
For v2 preflight compilation, a separate temporary view omits consumer and dependency global
hooks so compilation cannot execute them before validation and refresh confirmation.

The empty-mode guard runs in package-model pre-hooks. dbt v2 executes global hooks during
`compile`, including empty hooks; model pre-hooks preserve the guard without turning naming
discovery or output compilation into warehouse execution.

dbt 2 does not expose the metadata needed to establish whether dispatched maintenance
macros have been overridden. When maintenance shortcuts are enabled, dbt-dqm therefore
executes the maintenance steps normally on dbt 2; dbt 1 retains the shortcuts when safe.

## Local results

The complete Postgres 16 acceptance suite passed with each v1 engine. After replacing dynamic
type macro lookups, three affected setup, empty-mode, and migration tests passed again on each.
The Python suite passed on both Python 3.11 and 3.14, and the base, BigQuery, Postgres, and
combined wheel installations passed without installing a dbt engine.

Credentialed BigQuery tests passed the dedicated limits, full-refresh, and complex-payload
scenario on Core 1.12.5 and dbt 2.0.9. The complete acceptance suite passed all 32 cases
locally on each engine. The exact-commit CI gate remains required.

The real 2.0.9 engine also ran a bounded DQM test against a disposable local DuckDB fixture with
temporary SQL adapter shims: three failing rows produced the configured two-row warning. This
checks materialization delegation, but does not replace the credentialed BigQuery lifecycle and
acceptance runs. The CI release gate remains mandatory for the exact candidate commit.

## 0.2.3 publication recovery

The 0.2.2 candidate passed 33 BigQuery cases on dbt 2.0.11, 33 on Core 1.12.6 and
one serial runner/reviewer permissions case. Its full GitHub platform matrix also passed.
Publication stopped before PyPI approval because draft evidence could not be retrieved by tag.

Version 0.2.3 changes release controls and version metadata only. A maintainer-approved,
one-release recovery compares source with tree `3e2d4837fa28007301709f41d03d39e8ffd9093d`,
checks exact historical reports and GitHub metadata, and compares wheel runtime payloads.
Reports retain their original 0.2.2 identities; a separate recovery record binds them to 0.2.3.
No new WIF verification is claimed. This exception does not apply to later versions.
