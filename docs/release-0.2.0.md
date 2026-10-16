# 0.2.0 stable release verification

This release promotes the 0.2.0a8 behavior to stable packaging. The Python app, dbt package,
and lockfile all use `0.2.0`. No warehouse schema migration or history reset is required from a7
or a8. The [release checklist](release-checklist.md) applies to one frozen candidate.

The production candidate is `955e6dc7d06b20d93eeefc50dd1495ed6e5c90b4`. Only the
version, packaging classifier and release documentation differ from a8; package SQL and
application behavior are unchanged. A documentation-only commit records these results after
the candidate was frozen.

Candidate verification includes Python/UI tests, native Windows and macOS CI, clean locked
and fresh-installed dependency audits, an installed-wheel browser check in snapshot and
warehouse modes, and complete CSV and Excel exports at 50,000 saved issues. A separate
Postgres 16 check populated an a8 installation with seven issues and verified that an ordinary
0.2.0 rerun preserved their identities without a refresh. The browser and upgrade checks ran
against the exact candidate in [stable QA run 38847053731](https://github.com/a-adamovich/dbt-dqm/actions/runs/38847053731).
The [raw 50,000-issue result](scale-results/candidate-955e6dc-cache-boundary.json) is from
the same installed wheel.

The Git tag is the installation source until the separate dbt Hub registration is approved.
Full warehouse gate, approval, public artifact and Hub listing evidence is maintained on the
[v0.2.0 release page](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0).
