# dbt-dqm documentation

## Get started

- [Disposable Postgres](quickstart-postgres.md) runs the local demo with Docker.
- [Disposable BigQuery](quickstart-bigquery.md) runs the demo in an authorized dataset.
- [Existing dbt project](quickstart-existing-project.md) walks through tracking and reviewing one test in a development target.

## Understand the system

- [Concepts](concepts.md) explains opt-in, issue identity, occurrences, and the lifecycle.
- [How a run works](how-it-works.md) follows evidence from dbt through reconciliation and review.
- [Warehouse tables](warehouse-tables.md) shows where data lives and how to query it.

## Configure and review

- [CLI reference](reference-cli.md) covers commands, flags, exit codes, and troubleshooting.
- [Configuration reference](reference-configuration.md) covers test metadata, project variables, naming, and authentication.
- [Warehouse interfaces](warehouse-interfaces.md) defines the relation, annotation, event, and recovery contracts.
- [Review app workflows](review-app.md) covers Sync, review edits, Health, exports, and missed issues.
- [Supported configurations](supported-configurations.md) lists engines, platforms, installation options, and limits.

## Operate and upgrade

- [Operations](operations.md) covers retention, maintenance, and BigQuery dataset collation.
- [Scale measurements](scale.md) records measured warehouse and app behavior.
- [Legacy upgrades](legacy.md) explains migrations, namespace changes, and protective refusals.
- [Identity incident #105 recovery](identity-105-recovery.md) is for installations affected by the historical identity bug.

## Contribute and maintain

- [Contributor source tour](contributor-tour.md) follows an issue through the code and tests.
- [CI tiers and validation](ci-measurements.md) describes automatic checks and manual validation.
- [Release checklist](release-checklist.md) defines publication gates.
- [On-demand BigQuery verification](bigquery-lifecycle.md) explains the maintainer cloud setup.

## Release history

- [Changelog](../CHANGELOG.md)
- [0.2.5 release notes](release-notes-0.2.5.md)
- [Archive](archive.md) of retired guides and historical evidence.
