# dbt v2 compatibility verification

Version 0.2.5 targets **dbt 2.0.11**, retaining v2 support from 2.0.9 and Core
1.11–1.12. Real 2.0.9 and 2.0.11 offline regression checks are required. Full live
2.0.11/Core 1.12.6 verification is required before publication. The published release
provides its reports as attachments; the [historical results](archive.md#historical-sections) cannot substitute for them.

## Release gate

Version 0.2.5 was validated with fresh local 34-case suites on dbt 2.0.11 and Core 1.12.6, the
serial restricted-permissions test, a serial cross-engine historical-recovery scenario, and
complete GitHub platform validation. Identity signature contracts were checked with both real
engines, including Unicode; migration 0008 was exercised on populated history. Those local results
were a one-time maintainer attestation and did not certify live WIF. See the
[current release checklist](release-checklist.md). Later releases must pass the credentialed
GitHub gate, which runs the full BigQuery set on dbt 2 and the smoke selection on Core; Core stays
fully covered on real Postgres and by offline BigQuery rendering, so a Core-only BigQuery adapter
bug outside the smoke selection can slip through. See [CI tiers](ci-measurements.md#bigquery-tiers).
Historical results cannot substitute for fresh evidence. Native Windows checks cover
unprivileged project projection and large literal payloads on real engines without warehouse access.

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
links the consumer sources and installed packages, using private copies when Windows denies
symlinks, and adds a root forwarding macro. Test
identities and selection stay stable, and original source/config files are never edited.
Artifacts and state paths remain absolute and resolve against the original project.
Internal SQL, result payloads and column maps are literal JSON in temporary project variables;
they do not consume command-line space or depend on a later compiled graph.
For v2 preflight compilation, a separate temporary view omits consumer and dependency global
hooks so compilation cannot execute them before validation and refresh confirmation.

The empty-mode guard runs in package-model pre-hooks. dbt v2 executes global hooks during
`compile`, including empty hooks; model pre-hooks preserve the guard without turning naming
discovery or output compilation into warehouse execution.

dbt 2 does not expose the metadata needed to establish whether dispatched maintenance
macros have been overridden. When maintenance shortcuts are enabled, dbt-dqm therefore
executes the maintenance steps normally on dbt 2; dbt 1 retains the shortcuts when safe.
